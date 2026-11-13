"""A deliberately small, scriptable IMAP4rev1 server for local demos/tests.

It speaks enough of IMAP over plain TCP for ``imaplib`` (and therefore the
intake worker) to:

  * CAPABILITY / NOOP / LOGIN / LOGOUT
  * SELECT / EXAMINE / CLOSE / STATUS
  * UID SEARCH, UID FETCH (BODY.PEEK[], FLAGS, RFC822.SIZE), UID STORE
  * APPEND (used to seed new mail, e.g. after a rebuild)

It is *not* a general-purpose mail server and it is not hardened for
untrusted networks.  Its purpose is to reproduce the two awkward but real
behaviours the intake service must survive:

  * **Mailbox rebuild with UID reuse** -- ``POST /admin/rebuild`` wipes the
    mailbox (or creates a new UIDVALIDITY) and lets mail be appended at UID 1,
    so old local UID records become dangerous if replayed.
  * **UNSEEN STORE failure** -- ``POST /admin/fail-seen`` makes
    ``UID STORE +FLAGS (\\Seen)`` return ``NO`` a configurable number of times.

State is held in memory; the seed mail is read from a directory of ``.eml``
files on startup and re-read after a rebuild.  Admin control is exposed over
HTTP on a separate port.
"""

from __future__ import annotations

import base64
import email
import hashlib
import html
import json
import logging
import re
import socketserver
import threading
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger("mock_imap")

FLAG_SEEN = "\\Seen"
FLAG_FLAGGED = "\\Flagged"
FLAG_DELETED = "\\Deleted"
FLAG_ANSWERED = "\\Answered"
FLAG_DRAFT = "\\Draft"


def _normalise_flag(flag: str) -> str:
    flag = flag.strip()
    if not flag:
        return flag
    if flag.startswith("\\"):
        return "\\" + flag[1:2].upper() + flag[2:].lower()
    return flag


@dataclass
class StoredMessage:
    uid: int
    raw: bytes
    flags: set[str] = field(default_factory=set)

    @property
    def size(self) -> int:
        return len(self.raw)

    def header(self, name: str) -> str | None:
        msg = BytesParser(policy=policy.default).parsebytes(self.raw)
        value = msg.get(name)
        return str(value).strip() if value else None


@dataclass
class Mailbox:
    name: str = "INBOX"
    uid_validity: int = 1
    uid_next: int = 1
    messages: list[StoredMessage] = field(default_factory=list)

    def append(self, raw: bytes, flags: set[str] | None = None) -> int:
        uid = self.uid_next
        self.uid_next += 1
        self.messages.append(StoredMessage(uid=uid, raw=raw, flags=set(flags or set())))
        return uid

    def by_uid(self, uid: int) -> StoredMessage | None:
        for message in self.messages:
            if message.uid == uid:
                return message
        return None

    def search(self, criteria: list[str]) -> list[int]:
        text = " ".join(criteria).upper()
        predicate: list[Any] = []

        if "UNSEEN" in text:
            predicate.append(lambda m: FLAG_SEEN not in m.flags)
        elif "SEEN" in text:
            predicate.append(lambda m: FLAG_SEEN in m.flags)
        if "ALL" in text or not predicate:
            predicate.append(lambda m: True)

        def matches(message: StoredMessage) -> bool:
            return all(fn(message) for fn in predicate)

        return [m.uid for m in self.messages if matches(m)]


class ImapSession(socketserver.StreamRequestHandler):
    server: "MockImapServer"

    def setup(self) -> None:
        super().setup()
        self.state = "NONAUTH"
        self.selected: Mailbox | None = None
        self.selected_uid_validity: int | None = None
        self.readonly = False
        self.user: str | None = None

    # ------------------------------------------------------------- plumbing
    def handle(self) -> None:
        try:
            self.wfile.write(b"* OK [CAPABILITY IMAP4rev1] mock IMAP ready\r\n")
            self.wfile.flush()
            while True:
                line = self.rfile.readline()
                if not line:
                    return
                line = line.rstrip(b"\r\n")
                if not line:
                    continue
                if not self._dispatch(line):
                    return
        except (ConnectionError, BrokenPipeError, OSError):
            return
        finally:
            pass

    def _send(self, data: bytes) -> None:
        self.wfile.write(data)
        self.wfile.flush()

    def _ok(self, tag: str, text: str = "OK") -> None:
        self._send(f"{tag} OK {text}\r\n".encode())

    def _no(self, tag: str, text: str) -> None:
        self._send(f"{tag} NO {text}\r\n".encode())

    def _bad(self, tag: str, text: str) -> None:
        self._send(f"{tag} BAD {text}\r\n".encode())

    def _untagged(self, text: str) -> None:
        self._send(f"* {text}\r\n".encode())

    # ------------------------------------------------------------- dispatch
    def _dispatch(self, line: bytes) -> bool:
        text = line.decode("utf-8", "replace")
        parts = text.split(" ")
        if not parts:
            return True
        tag = parts[0]
        if len(parts) < 2:
            self._bad(tag, "missing command")
            return True
        command = parts[1].upper()
        args = parts[2:]
        try:
            if command == "CAPABILITY":
                self._untagged("CAPABILITY IMAP4rev1")
                self._ok(tag, "CAPABILITY completed")
            elif command == "NOOP":
                self._ok(tag, "NOOP completed")
            elif command == "LOGIN":
                self._handle_login(tag, args)
            elif command == "LOGOUT":
                self._untagged("BYE mock IMAP logging out")
                self._ok(tag, "LOGOUT completed")
                return False
            elif command in {"SELECT", "EXAMINE"}:
                self._handle_select(tag, args, readonly=command == "EXAMINE")
            elif command == "CLOSE":
                self.selected = None
                self.selected_uid_validity = None
                self.state = "AUTH"
                self._ok(tag, "CLOSE completed")
            elif command == "STATUS":
                self._handle_status(tag, args)
            elif command == "UID":
                if not args:
                    self._bad(tag, "UID requires a command")
                else:
                    self._handle_uid(tag, args[0].upper(), args[1:])
            elif command == "APPEND":
                self._handle_append(tag, text)
            else:
                self._bad(tag, f"command {command} not supported by mock server")
        except Exception as exc:  # noqa: BLE001 - always answer the client
            logger.exception("mock IMAP command failed: %s", text)
            self._bad(tag, f"internal error: {exc}")
        return True

    # --------------------------------------------------------------- commands
    def _handle_login(self, tag: str, args: list[str]) -> None:
        if len(args) < 2:
            self._bad(tag, "LOGIN requires user and password")
            return
        user = _unquote(args[0])
        password = _unquote(" ".join(args[1:]))
        if user != self.server.username or password != self.server.password:
            self._no(tag, "[AUTHENTICATIONFAILED] invalid credentials")
            return
        self.user = user
        self.state = "AUTH"
        self._ok(tag, "LOGIN completed")

    def _require_auth(self, tag: str) -> bool:
        if self.state not in {"AUTH", "SELECTED"}:
            self._bad(tag, "not authenticated")
            return False
        return True

    def _require_selected(self, tag: str) -> bool:
        if self.selected is None:
            self._bad(tag, "no mailbox selected")
            return False
        return True

    def _handle_select(self, tag: str, args: list[str], readonly: bool) -> None:
        if not self._require_auth(tag):
            return
        if not args:
            self._bad(tag, "SELECT requires a mailbox")
            return
        name = _unquote(args[0])
        if name.upper() != "INBOX":
            self._no(tag, f"mailbox {name} not found")
            return
        mailbox = self.server.mailbox
        self.selected = mailbox
        self.selected_uid_validity = mailbox.uid_validity
        self.readonly = readonly
        self.state = "SELECTED"
        exists = len(mailbox.messages)
        self._untagged(f"{exists} EXISTS")
        self._untagged(f"{sum(1 for m in mailbox.messages if FLAG_SEEN not in m.flags)} RECENT")
        self._untagged("FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
        self._untagged(f"OK [UIDVALIDITY {mailbox.uid_validity}] UIDs valid")
        self._untagged(f"OK [UIDNEXT {mailbox.uid_next}] Predicted next UID")
        mode = "READ-ONLY" if readonly else "READ-WRITE"
        self._ok(tag, f"[{mode}] SELECT completed")

    def _handle_status(self, tag: str, args: list[str]) -> None:
        if not self._require_auth(tag):
            return
        if len(args) < 2:
            self._bad(tag, "STATUS requires mailbox and items")
            return
        name = _unquote(args[0])
        if name.upper() != "INBOX":
            self._no(tag, f"mailbox {name} not found")
            return
        mailbox = self.server.mailbox
        items = " ".join(args[1:]).strip("()").upper().split()
        values: list[str] = []
        for item in items:
            if item == "UIDVALIDITY":
                values.append(f"UIDVALIDITY {mailbox.uid_validity}")
            elif item == "UIDNEXT":
                values.append(f"UIDNEXT {mailbox.uid_next}")
            elif item == "MESSAGES":
                values.append(f"MESSAGES {len(mailbox.messages)}")
            elif item == "UNSEEN":
                values.append(
                    f"UNSEEN {sum(1 for m in mailbox.messages if FLAG_SEEN not in m.flags)}"
                )
        self._untagged(f'STATUS INBOX ({" ".join(values)})')
        self._ok(tag, "STATUS completed")

    def _handle_uid(self, tag: str, subcommand: str, args: list[str]) -> None:
        if not self._require_selected(tag):
            return
        mailbox = self.selected
        assert mailbox is not None
        if self.selected_uid_validity != mailbox.uid_validity:
            # A real server invalidates the session's UID references when the
            # mailbox is rebuilt behind an open session.
            self._no(tag, "[UIDVALIDITY] mailbox was rebuilt; reselect")
            return
        if subcommand == "SEARCH":
            criteria = [a for a in args if a.upper() not in {"CHARSET", "UTF-8"}]
            if args and args[0].upper() == "CHARSET":
                criteria = args[2:]
            uids = mailbox.search(criteria)
            self._untagged("SEARCH " + " ".join(str(u) for u in uids))
            self._ok(tag, "UID SEARCH completed")
        elif subcommand == "FETCH":
            self._handle_uid_fetch(tag, mailbox, args)
        elif subcommand == "STORE":
            self._handle_uid_store(tag, mailbox, args)
        else:
            self._bad(tag, f"UID {subcommand} not supported")

    def _handle_uid_fetch(self, tag: str, mailbox: Mailbox, args: list[str]) -> None:
        if len(args) < 2:
            self._bad(tag, "UID FETCH requires set and items")
            return
        uid_set = args[0]
        items = " ".join(args[1:]).upper()
        uids: list[int] = []
        for token in re.split(r"[,\s]+", uid_set):
            if token.isdigit():
                uids.append(int(token))
            elif token in {"*", "1:*"}:
                uids = [m.uid for m in mailbox.messages] or [0]
        targets = sorted({uid for uid in uids if mailbox.by_uid(uid) is not None})

        for uid in targets:
            message = mailbox.by_uid(uid)
            if message is None:
                continue
            pieces: list[str] = [f"UID {message.uid}"]
            if "FLAGS" in items:
                pieces.append("FLAGS (" + " ".join(sorted(message.flags)) + ")")
            if "RFC822.SIZE" in items:
                pieces.append(f"RFC822.SIZE {message.size}")
            if "BODY.PEEK[]" in items or "BODY[]" in items or "RFC822" in items:
                self._send(
                    f"* {message.uid} FETCH (UID {message.uid} ".encode()
                    + b"RFC822.SIZE "
                    + str(message.size).encode()
                    + b" BODY[] {"
                    + str(message.size).encode()
                    + b"}\r\n"
                    + message.raw
                    + b")\r\n"
                )
                continue
            self._send(
                f"* {message.uid} FETCH ({' '.join(pieces)})\r\n".encode()
            )
        self._ok(tag, "UID FETCH completed")

    def _handle_uid_store(self, tag: str, mailbox: Mailbox, args: list[str]) -> None:
        if len(args) < 3:
            self._bad(tag, "UID STORE requires set, item and flags")
            return
        uid_set = args[0]
        item = args[1].upper()
        flags_raw = " ".join(args[2:]).strip("()")
        flags = {_normalise_flag(f) for f in flags_raw.split() if f}

        uids: list[int] = []
        for token in re.split(r"[,\s]+", uid_set):
            if token.isdigit():
                uids.append(int(token))
        messages = [mailbox.by_uid(uid) for uid in uids]
        if any(m is None for m in messages):
            self._no(tag, "UID STORE failed: message not found")
            return

        if FLAG_SEEN in flags:
            failure = self.server.consume_seen_failure()
            if failure:
                self._no(tag, f"[SERVERBUG] simulated \\Seen store failure ({failure})")
                return

        for message in messages:
            assert message is not None
            if item in {"+FLAGS", "+FLAGS.SILENT"}:
                message.flags |= flags
            elif item in {"-FLAGS", "-FLAGS.SILENT"}:
                message.flags -= flags
            elif item in {"FLAGS", "FLAGS.SILENT"}:
                message.flags = set(flags)
            if item.endswith(".SILENT"):
                continue
            self._untagged(
                f"{message.uid} FETCH (UID {message.uid} FLAGS ("
                + " ".join(sorted(message.flags))
                + "))"
            )
        self._ok(tag, "UID STORE completed")

    def _handle_append(self, tag: str, line: str) -> None:
        if not self._require_auth(tag):
            return
        match = re.search(r"\{(\d+)\}$", line)
        if not match:
            self._bad(tag, "APPEND requires a literal size")
            return
        size = int(match.group(1))
        self._send(b"+ Ready for literal data\r\n")
        raw = self.rfile.read(size)
        # Consume the trailing CRLF after the literal.
        self.rfile.readline()
        flags_match = re.search(r"\(([^)]*)\)", line)
        flags = set()
        if flags_match:
            flags = {_normalise_flag(f) for f in flags_match.group(1).split() if f}
        uid = self.server.mailbox.append(raw, flags)
        self._ok(tag, f"[APPENDUID {self.server.mailbox.uid_validity} {uid}] APPEND completed")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


# ------------------------------------------------------------------ server
class MockImapServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        username: str,
        password: str,
        seed_dir: Path | None = None,
        initial_uid_validity: int = 1000,
    ):
        super().__init__(address, ImapSession)
        self.username = username
        self.password = password
        self.seed_dir = seed_dir
        self.mailbox = Mailbox(name="INBOX", uid_validity=initial_uid_validity)
        self._lock = threading.Lock()
        self.seen_failures_remaining = 0
        self.seen_failure_forever = False
        self.seen_failure_count = 0
        self.append_failures_remaining = 0
        self.seed()

    # ------------------------------------------------------------- seeding
    def seed(self) -> None:
        if self.seed_dir is None or not self.seed_dir.is_dir():
            return
        for path in sorted(self.seed_dir.glob("*.eml")):
            raw = path.read_bytes()
            flags = set()
            if path.name.startswith("read_"):
                flags.add(FLAG_SEEN)
            self.mailbox.append(raw, flags)
            logger.info("seeded %s as UID %s", path.name, self.mailbox.uid_next - 1)

    # ------------------------------------------------------------- rebuild
    def rebuild(self, *, uid_validity: int | None = None, reseed: bool = True) -> dict:
        with self._lock:
            new_validity = (
                int(uid_validity)
                if uid_validity is not None
                else self.mailbox.uid_validity + 1
            )
            self.mailbox = Mailbox(name="INBOX", uid_validity=new_validity)
            if reseed:
                self.seed()
            return {
                "uid_validity": new_validity,
                "messages": len(self.mailbox.messages),
            }

    # --------------------------------------------------------- failure knobs
    def fail_seen(self, count: int, forever: bool = False) -> None:
        with self._lock:
            self.seen_failures_remaining = max(0, int(count))
            self.seen_failure_forever = bool(forever)

    def clear_fail_seen(self) -> None:
        with self._lock:
            self.seen_failures_remaining = 0
            self.seen_failure_forever = False

    def consume_seen_failure(self) -> str | None:
        with self._lock:
            if self.seen_failure_forever:
                self.seen_failure_count += 1
                return f"persistent failure #{self.seen_failure_count}"
            if self.seen_failures_remaining > 0:
                self.seen_failures_remaining -= 1
                self.seen_failure_count += 1
                return f"transient failure #{self.seen_failure_count}"
            return None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "uid_validity": self.mailbox.uid_validity,
                "uid_next": self.mailbox.uid_next,
                "messages": [
                    {
                        "uid": m.uid,
                        "flags": sorted(m.flags),
                        "size": m.size,
                        "message_id": m.header("Message-ID"),
                        "subject": m.header("Subject"),
                        "from": m.header("From"),
                        "sha256": hashlib.sha256(m.raw).hexdigest(),
                    }
                    for m in self.mailbox.messages
                ],
                "seen_failure_forever": self.seen_failure_forever,
                "seen_failures_remaining": self.seen_failures_remaining,
                "seen_failure_count": self.seen_failure_count,
            }

    def append_raw(self, raw: bytes) -> int:
        with self._lock:
            return self.mailbox.append(raw)


# --------------------------------------------------------------- admin HTTP
class AdminHandler(BaseHTTPRequestHandler):
    server: "AdminServer"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        logger.debug("admin: " + fmt, *args)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/admin/state":
            self._json(200, self.server.imap_server.snapshot())
        elif self.path.rstrip("/") == "/admin/health":
            self._json(200, {"status": "ok"})
        elif self.path.rstrip("/") == "/":
            body = _index_html().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        body = self._read_body()
        try:
            payload = json.loads(body) if body.strip() else {}
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
        except ValueError as exc:
            self._json(400, {"error": f"invalid JSON: {exc}"})
            return

        if path == "/admin/rebuild":
            uid_validity = payload.get("uid_validity")
            reseed = bool(payload.get("reseed", True))
            result = self.server.imap_server.rebuild(
                uid_validity=uid_validity, reseed=reseed
            )
            self._json(200, {"rebuilt": True, **result})
        elif path == "/admin/fail-seen":
            self.server.imap_server.fail_seen(
                int(payload.get("count", 1)), forever=bool(payload.get("forever", False))
            )
            self._json(200, {"ok": True, **self.server.imap_server.snapshot()})
        elif path == "/admin/clear-fail-seen":
            self.server.imap_server.clear_fail_seen()
            self._json(200, {"ok": True})
        elif path == "/admin/append":
            # Optional convenience: append a raw RFC822 message (base64).
            encoded = payload.get("raw_base64")
            if not encoded:
                self._json(400, {"error": "raw_base64 is required"})
                return
            raw = base64.b64decode(encoded)
            uid = self.server.imap_server.append_raw(raw)
            self._json(200, {"uid": uid, **self.server.imap_server.snapshot()})
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        self.do_POST()


class AdminServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, imap_server: MockImapServer):
        super().__init__(address, AdminHandler)
        self.imap_server = imap_server


def _index_html() -> str:
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>mock IMAP admin</title>
<style>body{font-family:system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem}
pre{background:#f4f4f4;padding:1rem;overflow:auto;border-radius:6px}
button{margin:.2rem;padding:.4rem .8rem}code{background:#eee;padding:.1rem .25rem}</style></head>
<body>
<h1>Mock IMAP mailbox</h1>
<p>Plain IMAP on port <code>1143</code>; this admin UI/API on port <code>8081</code>.</p>
<p>Intake API: <a href="http://localhost:8000/docs">http://localhost:8000/docs</a></p>
<button onclick="post('/admin/rebuild',{uid_validity:2001})">Rebuild mailbox (new UIDVALIDITY, reseed)</button>
<button onclick="post('/admin/rebuild',{uid_validity:2002,reseed:false})">Rebuild mailbox (empty)</button>
<button onclick="post('/admin/fail-seen',{count:2})">Fail next 2 \\Seen stores</button>
<button onclick="post('/admin/fail-seen',{forever:true})">Fail \\Seen stores until cleared</button>
<button onclick="post('/admin/clear-fail-seen',{})">Clear \\Seen failures</button>
<h2>State</h2><pre id="state">loading...</pre>
<script>
async function refresh(){document.getElementById('state').textContent=
  JSON.stringify(await (await fetch('/admin/state')).json(),null,2);}
async function post(path,body){await fetch(path,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});refresh();}
refresh();setInterval(refresh,3000);
</script>
</body></html>"""


# --------------------------------------------------------------- entrypoint
def serve(host: str, port: int, admin_host: str, admin_port: int, **kwargs: Any) -> None:
    imap_server = MockImapServer((host, port), **kwargs)
    admin_server = AdminServer((admin_host, admin_port), imap_server)
    threading.Thread(target=imap_server.serve_forever, daemon=True).start()
    logger.info("mock IMAP listening on %s:%s", host, port)
    logger.info("admin HTTP listening on %s:%s", admin_host, admin_port)
    try:
        admin_server.serve_forever()
    finally:
        admin_server.shutdown()
        imap_server.shutdown()
