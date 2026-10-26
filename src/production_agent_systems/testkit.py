from __future__ import annotations

import argparse
import json
import sys
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .effects import (
    EffectContract,
    EffectExecutor,
    EffectStatus,
    OutcomeProbeResult,
    ProbeStatus,
    SQLiteEffectLedger,
)


class FaultPoint(str, Enum):
    NONE = "none"
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT_BEFORE_ACK = "after_commit_before_ack"


class FaultInjectingPaymentProvider:
    """Small deterministic provider simulator used by examples and reliability tests."""

    def __init__(
        self,
        *,
        fault_point: FaultPoint = FaultPoint.NONE,
        supports_idempotency: bool = True,
    ) -> None:
        self.fault_point = fault_point
        self.supports_idempotency = supports_idempotency
        self.committed_effects: list[dict[str, Any]] = []
        self._by_key: dict[str, dict[str, Any]] = {}
        self._by_invoice: dict[str, dict[str, Any]] = {}
        self._fault_injected = False

    def capture(
        self, arguments: Mapping[str, Any], *, idempotency_key: str | None
    ) -> dict[str, Any]:
        invoice_id = str(arguments["invoice_id"])

        if self.fault_point is FaultPoint.BEFORE_COMMIT and not self._fault_injected:
            self._fault_injected = True
            raise TimeoutError("injected failure before the provider commits")

        if (
            self.supports_idempotency
            and idempotency_key is not None
            and idempotency_key in self._by_key
        ):
            result = self._by_key[idempotency_key]
        else:
            result = {
                "payment_id": f"pay-{len(self.committed_effects) + 1:06d}",
                "invoice_id": invoice_id,
                "status": "captured",
            }
            self.committed_effects.append(result)
            self._by_invoice[invoice_id] = result
            if self.supports_idempotency and idempotency_key is not None:
                self._by_key[idempotency_key] = result

        if (
            self.fault_point is FaultPoint.AFTER_COMMIT_BEFORE_ACK
            and not self._fault_injected
        ):
            self._fault_injected = True
            raise TimeoutError("injected lost acknowledgement after provider commit")

        return dict(result)

    def probe(
        self, arguments: Mapping[str, Any], *, idempotency_key: str | None
    ) -> OutcomeProbeResult:
        result = None
        if self.supports_idempotency and idempotency_key is not None:
            result = self._by_key.get(idempotency_key)
        if result is None:
            result = self._by_invoice.get(str(arguments["invoice_id"]))
        if result is not None:
            return OutcomeProbeResult(
                status=ProbeStatus.COMMITTED,
                output=dict(result),
                provider_operation_id=str(result["payment_id"]),
                evidence={"source": "fake_provider_ledger"},
            )
        # The fake provider processes requests synchronously, so this is definitive
        # evidence that the injected pre-commit failure did not produce an effect.
        return OutcomeProbeResult(
            status=ProbeStatus.NOT_COMMITTED,
            evidence={"source": "fake_provider_ledger", "terminal": True},
        )


def run_scenario(scenario: str) -> dict[str, Any]:
    try:
        fault_point = FaultPoint(scenario.replace("-", "_"))
    except ValueError as exc:
        valid = ", ".join(item.value.replace("_", "-") for item in FaultPoint)
        raise ValueError(f"unknown scenario {scenario!r}; choose one of: {valid}") from exc

    provider = FaultInjectingPaymentProvider(fault_point=fault_point)
    with tempfile.TemporaryDirectory(prefix="production-agent-systems-") as directory:
        ledger = SQLiteEffectLedger(Path(directory) / "effects.sqlite3")
        executor = EffectExecutor(ledger=ledger)
        contract = EffectContract(
            name="capture_payment",
            operation_id=lambda args: f"invoice:{args['invoice_id']}:capture:v1",
            provider_idempotency_key=lambda args: f"capture:{args['invoice_id']}",
            provider_idempotency_guaranteed=True,
            outcome_probe=provider.probe,
        )
        result = executor.execute(
            contract=contract,
            arguments={"invoice_id": "INV-DEMO-1"},
            handler=provider.capture,
        )
        events = ledger.events(result.operation_id)
        return {
            "scenario": scenario,
            "operation_id": result.operation_id,
            "status": result.status.value,
            "confirmed": result.status is EffectStatus.CONFIRMED,
            "attempts": result.attempts,
            "committed_effect_count": len(provider.committed_effects),
            "duplicate_effect_detected": len(provider.committed_effects) > 1,
            "provider_result": result.output,
            "event_types": [event["event_type"] for event in events],
            "passed": (
                result.status is EffectStatus.CONFIRMED
                and len(provider.committed_effects) == 1
                and not result.in_progress
            ),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic effect-reliability fault-injection scenarios."
    )
    parser.add_argument(
        "--scenario",
        choices=[item.value.replace("_", "-") for item in FaultPoint],
        default="after-commit-before-ack",
        help="Failure point to inject into the fake payment provider.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path to write the JSON report; stdout is always populated.",
    )
    args = parser.parse_args(argv)

    report = run_scenario(args.scenario)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
