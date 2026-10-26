from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .effects import EffectExecutor, EffectStatus, SQLiteEffectLedger


def _write_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str))


def _common_resolution_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", required=True, help="Authenticated operator identifier.")
    parser.add_argument("--reason", required=True, help="Why the outcome or retry was resolved.")
    parser.add_argument(
        "--evidence-reference",
        required=True,
        help="Ticket, provider event, or other independently verified evidence reference.",
    )


def _operation_summary(record) -> dict[str, Any]:
    # Avoid printing provider results, request payloads, or evidence by default.
    return {
        "operation_id": record.operation_id,
        "contract_name": record.contract_name,
        "contract_version": record.contract_version,
        "status": record.status.value,
        "attempts": record.attempts,
        "provider_operation_id": record.provider_operation_id,
        "error": record.error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-effect-ops",
        description=(
            "Inspect effect operations and resolve quarantined outcomes with an audit trail. "
            "The caller is responsible for authenticating and authorizing operators."
        ),
    )
    parser.add_argument(
        "--db",
        required=True,
        type=Path,
        help="Path to the existing SQLite effect ledger.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser("list", help="List operation summaries.")
    list_parser.add_argument(
        "--status",
        choices=[status.value for status in EffectStatus],
        help="Filter by a specific operation status.",
    )
    list_parser.add_argument("--limit", type=int, default=100)

    events_parser = commands.add_parser("events", help="Show one operation's ordered event history.")
    events_parser.add_argument("operation_id")

    confirm_parser = commands.add_parser(
        "confirm",
        help="Record an independently verified successful effect.",
    )
    confirm_parser.add_argument("operation_id")
    confirm_parser.add_argument(
        "--output-json",
        required=True,
        help="JSON result to store for future replay.",
    )
    confirm_parser.add_argument("--provider-operation-id")
    _common_resolution_arguments(confirm_parser)

    reject_parser = commands.add_parser(
        "reject",
        help="Record independently verified terminal non-execution/rejection.",
    )
    reject_parser.add_argument("operation_id")
    _common_resolution_arguments(reject_parser)

    retry_parser = commands.add_parser(
        "authorize-retry",
        help="Authorize a retry after independently verifying terminal non-execution.",
    )
    retry_parser.add_argument("operation_id")
    _common_resolution_arguments(retry_parser)

    args = parser.parse_args(argv)
    ledger = SQLiteEffectLedger(args.db)
    executor = EffectExecutor(ledger=ledger)

    if args.command == "list":
        status = EffectStatus(args.status) if args.status is not None else None
        records = ledger.list_operations(status=status, limit=args.limit)
        _write_json([_operation_summary(record) for record in records])
        return 0

    if args.command == "events":
        if ledger.get(args.operation_id) is None:
            parser.error(f"operation not found: {args.operation_id}")
        _write_json(ledger.events(args.operation_id))
        return 0

    if args.command == "confirm":
        try:
            output = json.loads(args.output_json)
        except json.JSONDecodeError as exc:
            parser.error(f"--output-json must be valid JSON: {exc}")
        result = executor.resolve_manually_confirmed(
            args.operation_id,
            output,
            provider_operation_id=args.provider_operation_id,
            actor=args.actor,
            reason=args.reason,
            evidence_reference=args.evidence_reference,
        )
        _write_json(
            {
                "operation_id": result.operation_id,
                "status": result.status.value,
                "resolved": result.status is EffectStatus.CONFIRMED,
                "provider_operation_id": result.provider_operation_id,
            }
        )
        return 0

    if args.command == "reject":
        result = executor.resolve_manually_rejected(
            args.operation_id,
            actor=args.actor,
            reason=args.reason,
            evidence_reference=args.evidence_reference,
        )
        _write_json(
            {
                "operation_id": result.operation_id,
                "status": result.status.value,
                "resolved": result.status is EffectStatus.REJECTED,
            }
        )
        return 0

    if args.command == "authorize-retry":
        record = executor.authorize_manual_retry(
            args.operation_id,
            actor=args.actor,
            reason=args.reason,
            evidence_reference=args.evidence_reference,
        )
        _write_json(
            {
                "operation_id": record.operation_id,
                "status": record.status.value,
                "retry_authorized": record.status is EffectStatus.PREPARED,
            }
        )
        return 0

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
