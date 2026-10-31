"""Operator CLI: inspect quarantined rows and mark them ignored.

python -m donation_ledger.admin list-rejected
python -m donation_ledger.admin ignore-rejected <id>
python -m donation_ledger.admin reopen-rejected <id>
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from donation_ledger.config import ConfigError, Settings
from donation_ledger.db import make_engine
from donation_ledger.models import RejectedRow


def _printable(text: str) -> str:
    """Entry IDs come from sheet cells: neutralise control characters before printing."""
    return text.encode("unicode_escape").decode("ascii")


def list_rejected(engine: Engine, include_closed: bool = False) -> list[RejectedRow]:
    stmt = select(RejectedRow).order_by(RejectedRow.id)
    if not include_closed:
        stmt = stmt.where(RejectedRow.status == "open")
    with Session(engine) as session:
        return list(session.scalars(stmt))


def set_status(engine: Engine, rejected_id: int, status: str) -> bool:
    with Session(engine) as session, session.begin():
        result = session.execute(
            update(RejectedRow).where(RejectedRow.id == rejected_id).values(status=status)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="donation_ledger.admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-rejected").add_argument("--all", action="store_true")
    sub.add_parser("ignore-rejected").add_argument("id", type=int)
    sub.add_parser("reopen-rejected").add_argument("id", type=int)
    args = parser.parse_args(argv)
    try:
        engine = make_engine(Settings.from_env().database_url)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    if args.cmd == "list-rejected":
        for r in list_rejected(engine, include_closed=args.all):
            # No raw row here: it contains donor details. Query the table if you need it.
            print(f"{r.id}\t{r.status}\t{r.source}\t{_printable(r.external_id or '-')}\t{r.reason}")
        return 0
    if not 1 <= args.id <= 2**63 - 1:
        print("no such rejected row", file=sys.stderr)
        return 1
    status = "ignored" if args.cmd == "ignore-rejected" else "open"
    if not set_status(engine, args.id, status):
        print("no such rejected row", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
