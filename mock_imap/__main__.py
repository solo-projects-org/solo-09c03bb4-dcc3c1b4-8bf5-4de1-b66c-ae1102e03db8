"""Run the mock IMAP server (and its admin UI) as a standalone container."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Local mock IMAP mailbox")
    parser.add_argument("--host", default=os.getenv("MOCK_IMAP_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("MOCK_IMAP_PORT", "1143"))
    )
    parser.add_argument(
        "--admin-host", default=os.getenv("MOCK_ADMIN_HOST", "0.0.0.0")
    )
    parser.add_argument(
        "--admin-port", type=int, default=int(os.getenv("MOCK_ADMIN_PORT", "8081"))
    )
    parser.add_argument(
        "--seed-dir", default=os.getenv("MOCK_SEED_DIR", "/app/mails")
    )
    parser.add_argument(
        "--user", default=os.getenv("MOCK_IMAP_USER", "hydro")
    )
    parser.add_argument(
        "--password", default=os.getenv("MOCK_IMAP_PASSWORD", "hydropass")
    )
    parser.add_argument(
        "--uid-validity", type=int, default=int(os.getenv("MOCK_UID_VALIDITY", "1000"))
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    seed = Path(args.seed_dir) if args.seed_dir else None
    serve(
        args.host,
        args.port,
        args.admin_host,
        args.admin_port,
        username=args.user,
        password=args.password,
        seed_dir=seed,
        initial_uid_validity=args.uid_validity,
    )


if __name__ == "__main__":
    main()
