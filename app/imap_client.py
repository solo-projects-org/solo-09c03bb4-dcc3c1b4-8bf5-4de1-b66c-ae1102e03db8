"""IMAP reading: connect, verify UIDVALIDITY, fetch unread messages, mark seen.

Design notes
------------
* Every connection starts from a fresh ``SELECT`` of the configured mailbox,
  and the ``UIDVALIDITY`` returned by the server is compared with the value
  persisted from the previous connection.  When it differs the mailbox has
  been rebuilt: all UIDs are invalid, so retry records for the old
  UIDVALIDITY are discarded (never replayed against the new one) and the
  unread scan starts over.  UIDs recorded under the new UIDVALIDITY are safe
  to reuse only while it stays the same.
* The unique CSV attachment is located by walking the MIME tree.  Attachments
  with a ``.csv`` filename, ``text/csv`` content type, or an octet-stream
  content type that is really UTF-8 text with comma-separated rows are all
  candidates.  "Unique" means exactly one candidate across the whole message;
  zero or several means the mail cannot be ingested.
* Fetches use ``BODY.PEEK[]`` so reading a message never changes its ``\\Seen``
  flag.  ``\\Seen`` is set explicitly with ``UID STORE`` and only after the
  local transaction committed.
"""

from __future__ import annotations

import email
import imaplib
import re
import socket
import ssl
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.utils import parseaddr

PLAIN_UID_RE = re.compile(r"UIDVALIDITY\s+(\d+)", re.IGNORECASE)

CSV_CONTENT_TYPES = {
    "text/csv",
    "text/comma-separated-values",
    "application/csv",
    "application/vnd.ms-excel",
}
CANDIDATE_FALLBACK_TYPES = {"application/octet-stream", "binary/octet-stream", ""}


class ImapError(RuntimeError):
    pass


class UidValidityError(ImapError):
    pass


class AttachmentError(ValueError):
    """The message does not contain exactly one unambiguous CSV attachment."""


@dataclass(frozen=True)
class FetchedMessage:
    uid: int
    uid_validity: int
    message_id: str | None
    subject: str | None
    from_addr: str | None
    filename: str | None
    content_type: str | None
    attachment: bytes
    raw_size: int


def _to_text(item: bytes | str | None) -> str:
    if item is None:
        return ""
    if isinstance(item, (bytes, bytearray)):
        return bytes(item).decode("ascii", "ignore")
    return str(item)


def _uid_validity_from_lines(lines: list[bytes]) -> int | None:
    """Extract UIDVALIDITY from SELECT/STATUS untagged response lines."""
    for line in lines:
        match = PLAIN_UID_RE.search(_to_text(line))
        if match:
            return int(match.group(1))
    return None


def _parse_select_response(data: list[bytes | None] | None) -> int | None:
    if not data:
        return None
    for item in data:
        for line in _to_text(item).splitlines():
            match = re.match(r"\s*UIDVALIDITY\s+(\d+)\s*$", line, re.IGNORECASE)
            if match:
                return int(match.group(1))
    return None


def _uidvalidity_from_uidfetch_meta(meta: bytes | None) -> int | None:
    """UID FETCH response metadata looks like ``1 (UID 5 UIDVALIDITY 42 ...)``."""
    if not meta:
        return None
    match = re.search(rb"UIDVALIDITY\s+(\d+)", meta, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _parse_uid_fetch(
    payload: tuple[bytes, bytes], uid_hint: int, expected_uid_validity: int
) -> tuple[int, int, bytes] | None:
    """Split ``(meta, literal)`` into (uid, uidvalidity, body)."""
    meta, body = payload
    meta = bytes(meta)
    uid_match = re.search(rb"UID\s+(\d+)", meta)
    uid = int(uid_match.group(1)) if uid_match else uid_hint
    uid_validity = _uidvalidity_from_uidfetch_meta(meta) or expected_uid_validity
    return uid, uid_validity, body


class ImapClient:
    """Thin, explicit wrapper over ``imaplib``.

    The wrapper deliberately keeps a single connection at a time; the caller
    decides when to reconnect.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int = 143,
        user: str,
        password: str,
        use_ssl: bool = False,
        ssl_verify: bool = True,
        timeout: float = 30.0,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.use_ssl = use_ssl
        self.ssl_verify = ssl_verify
        self.timeout = timeout
        self._conn: imaplib.IMAP4 | None = None
        self.mailbox = "INBOX"
        self.uid_validity: int | None = None

    # ---------------------------------------------------------- lifecycle
    def connect(self) -> None:
        self.close()
        if self.use_ssl:
            context = ssl.create_default_context()
            if not self.ssl_verify:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout, ssl_context=context)
        else:
            conn = imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
        try:
            conn.login(self.user, self.password)
        except Exception:
            try:
                conn.logout()
            except Exception:
                pass
            raise
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    @property
    def conn(self) -> imaplib.IMAP4:
        if self._conn is None:
            raise ImapError("IMAP client is not connected")
        return self._conn

    def select(self, mailbox: str) -> int:
        """SELECT the mailbox and return the server's UIDVALIDITY.

        The mailbox is selected read-write so ``\\Seen`` can be stored later.
        """
        typ, data = self.conn.select(mailbox, readonly=False)
        if typ != "OK":
            raise ImapError(f"cannot select mailbox {mailbox!r}: {typ} {data!r}")
        uid_validity = _uid_validity_from_lines(
            self.conn.untagged_responses.get("UIDVALIDITY", [])
        )
        if uid_validity is None:
            uid_validity = _parse_select_response(data)
        if uid_validity is None:
            # Last resort: an explicit STATUS-independent check.  Refusing to
            # continue without a UIDVALIDITY is safer than guessing.
            uid_validity = self._uid_validity_via_status(mailbox)
        if uid_validity is None:
            raise UidValidityError(
                f"mailbox {mailbox!r} did not report UIDVALIDITY; refusing to use UIDs"
            )
        self.mailbox = mailbox
        self.uid_validity = uid_validity
        return uid_validity

    def _uid_validity_via_status(self, mailbox: str) -> int | None:
        typ, data = self.conn.status(mailbox, "(UIDVALIDITY)")
        if typ != "OK" or not data:
            return None
        for item in data:
            if item is None:
                continue
            match = PLAIN_UID_RE.search(_to_text(item))
            if match:
                return int(match.group(1))
        return None

    # ------------------------------------------------------------- reading
    def search_unread(self) -> list[int]:
        typ, data = self.conn.uid("SEARCH", "UNSEEN")
        if typ != "OK":
            raise ImapError(f"UNSEEN search failed: {typ} {data!r}")
        return _flatten_uid_search(data)

    def fetch_message(self, uid: int) -> FetchedMessage:
        if self.uid_validity is None:
            raise UidValidityError("fetch_message requires a selected mailbox")
        typ, data = self.conn.uid("FETCH", str(uid), "(UID RFC822.SIZE BODY.PEEK[])")
        if typ != "OK":
            raise ImapError(f"UID FETCH {uid} failed: {typ} {data!r}")
        parsed = None
        for item in data or []:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], (bytes, bytearray)):
                parsed = _parse_uid_fetch(item, uid, self.uid_validity)
                break
        if parsed is None:
            raise ImapError(f"UID FETCH {uid} returned no message body")
        fetched_uid, fetched_uid_validity, raw = parsed
        if int(fetched_uid) != int(uid):
            raise UidValidityError(
                f"asked for UID {uid} but server returned UID {fetched_uid}"
            )
        if int(fetched_uid_validity) != int(self.uid_validity):
            # Never trust bytes fetched under a different epoch.
            self.uid_validity = int(fetched_uid_validity)
            raise UidValidityError(
                "UIDVALIDITY changed while fetching; discarding the fetched message"
            )
        return extract_message(uid, int(fetched_uid_validity), raw)

    def mark_seen(self, uid: int, uid_validity: int) -> None:
        if self.uid_validity != uid_validity:
            raise UidValidityError(
                f"refusing to mark UID {uid}: connection UIDVALIDITY "
                f"{self.uid_validity} does not match record UIDVALIDITY {uid_validity}"
            )
        typ, data = self.conn.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
        if typ != "OK":
            raise ImapError(f"UID STORE {uid} +FLAGS \\Seen failed: {typ} {data!r}")
        # Verify the flag actually took effect; some servers silently no-op.
        typ, data = self.conn.uid("FETCH", str(uid), "(FLAGS)")
        if typ != "OK":
            raise ImapError(f"UID FETCH {uid} FLAGS failed: {typ} {data!r}")
        flags = b" ".join(
            bytes(item[0]) if isinstance(item, tuple) else bytes(item)
            for item in (data or [])
            if item is not None
        )
        if b"\\seen" not in flags.lower():
            raise ImapError(f"server did not persist \\Seen on UID {uid}")

    def fetch_flags(self, uid: int) -> bytes:
        typ, data = self.conn.uid("FETCH", str(uid), "(FLAGS)")
        if typ != "OK":
            raise ImapError(f"UID FETCH {uid} FLAGS failed: {typ} {data!r}")
        return b" ".join(
            bytes(item[0]) if isinstance(item, tuple) else bytes(item)
            for item in (data or [])
            if item is not None
        )

    def ping(self) -> None:
        typ, data = self.conn.noop()
        if typ != "OK":
            raise ImapError(f"NOOP failed: {typ} {data!r}")


def _flatten_uid_search(data: list[bytes | None] | None) -> list[int]:
    if not data:
        return []
    chunks: list[str] = []
    for item in data:
        if item is None:
            continue
        if isinstance(item, tuple):
            item = item[0]
        chunks.append(bytes(item).decode("ascii", "ignore"))
    text = " ".join(chunks)
    uids: list[int] = []
    for token in text.split():
        if token.isdigit():
            uid = int(token)
            if uid not in uids:
                uids.append(uid)
    return sorted(uids)


# ------------------------------------------------------------- MIME parsing
def _filename(message: Message) -> str | None:
    filename = message.get_filename()
    if filename:
        return str(filename).strip() or None
    return None


def _is_csv_ish(message: Message) -> bool:
    content_type = message.get_content_type().lower()
    if content_type in CSV_CONTENT_TYPES:
        return True
    filename = (_filename(message) or "").lower()
    if filename.endswith(".csv"):
        return True
    return False


def _decode_part(message: Message) -> bytes:
    payload = message.get_payload(decode=True)
    if payload is None:
        # message/rfc822 or otherwise non-decodable part
        raise AttachmentError("attachment part has no decodable payload")
    return bytes(payload)


def _looks_like_csv(body: bytes) -> bool:
    if not body or b"\x00" in body:
        return False
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    comma_rows = sum(1 for line in lines[:5] if "," in line)
    return comma_rows >= min(len(lines), 3) or len(lines) == 1 and "," in lines[0]


def extract_message(uid: int, uid_validity: int, raw: bytes) -> FetchedMessage:
    try:
        message = email.message_from_bytes(raw, policy=policy.default)
    except Exception as exc:  # pragma: no cover - policy.default is lenient
        raise AttachmentError(f"cannot parse MIME message: {exc}") from exc

    message_id = str(message.get("Message-ID") or "").strip() or None
    subject = str(message.get("Subject") or "").strip() or None
    from_header = str(message.get("From") or "").strip() or None
    from_addr = None
    if from_header:
        addr = parseaddr(from_header)[1]
        from_addr = addr.strip() or None

    candidates = _csv_candidates(message)
    if not candidates:
        raise AttachmentError("message has no CSV attachment")
    if len(candidates) > 1:
        names = ", ".join(repr(item[0]) for item in candidates)
        raise AttachmentError(
            f"message has {len(candidates)} candidate CSV attachments; exactly one is required ({names})"
        )
    filename, content_type, attachment = candidates[0]
    if not attachment.strip():
        raise AttachmentError("CSV attachment is empty")
    return FetchedMessage(
        uid=uid,
        uid_validity=uid_validity,
        message_id=message_id,
        subject=subject,
        from_addr=from_addr,
        filename=filename,
        content_type=content_type,
        attachment=attachment,
        raw_size=len(raw),
    )


def _csv_candidates(message: Message) -> list[tuple[str | None, str, bytes]]:
    """Return candidates, but let a real .csv/*csv part win over a generic one.

    A PDF named ``notes.pdf`` sent as octet-stream data that merely looks
    comma-separated is still a candidate, but a mail with exactly one
    ``text/csv`` part and one ambiguous octet-stream part has a single
    unambiguous CSV attachment.
    """
    raw_candidates = _collect_candidates(message)
    known = [c for c in raw_candidates if _is_csv_ish_name(c[0], c[1])]
    ambiguous = [c for c in raw_candidates if not _is_csv_ish_name(c[0], c[1])]
    return known if known else ambiguous


def _is_csv_ish_name(filename: str | None, content_type: str) -> bool:
    content_type = (content_type or "").lower()
    return content_type in CSV_CONTENT_TYPES or (
        filename is not None and filename.lower().endswith(".csv")
    )


def _collect_candidates(message: Message) -> list[tuple[str | None, str, bytes]]:
    candidates: list[tuple[str | None, str, bytes]] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type().lower()
        disposition = (part.get("Content-Disposition") or "").lower()
        filename = _filename(part)
        try:
            if _is_csv_ish(part):
                candidates.append((filename, content_type, _decode_part(part)))
                continue
            if content_type in CANDIDATE_FALLBACK_TYPES and (
                "attachment" in disposition or filename is not None
            ):
                body = _decode_part(part)
                if _looks_like_csv(body):
                    candidates.append((filename, content_type, body))
        except AttachmentError:
            continue
    return candidates
