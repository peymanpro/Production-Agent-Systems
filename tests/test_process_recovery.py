from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from production_agent_systems.effects import (
    EffectContract,
    EffectExecutor,
    EffectStatus,
    OutcomeProbeResult,
    ProbeStatus,
    SQLiteEffectLedger,
)


def test_subprocess_crash_after_external_commit_recovers_without_reexecution(tmp_path):
    database = tmp_path / "crash-recovery.sqlite3"
    provider_record = tmp_path / "provider-record.json"
    script = r"""
import os
import sys
from pathlib import Path
from production_agent_systems.effects import EffectContract, EffectExecutor, SQLiteEffectLedger

database, provider_record = sys.argv[1], sys.argv[2]
contract = EffectContract(
    name="provision_resource",
    operation_id=lambda args: f"resource:{args['resource_id']}:create",
)

def handler(arguments, *, idempotency_key):
    # Simulate an external provider commit, then kill the worker before its
    # response can be persisted in the local effect ledger.
    Path(provider_record).write_text(
        '{"resource_id":"R-CRASH","status":"created"}',
        encoding="utf-8",
    )
    os._exit(73)

EffectExecutor(
    ledger=SQLiteEffectLedger(database),
    lease_seconds=0.05,
).execute(
    contract=contract,
    arguments={"resource_id": "R-CRASH"},
    handler=handler,
)
os._exit(74)
"""
    worker = subprocess.run(
        [sys.executable, "-c", script, str(database), str(provider_record)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert worker.returncode == 73
    assert provider_record.exists()

    # The worker is dead, but its durable dispatch claim remains. Wait for the
    # lease to expire before a separate process context reconciles the provider.
    time.sleep(0.12)
    provider_result = json.loads(provider_record.read_text(encoding="utf-8"))
    probe_calls: list[int] = []
    handler_calls: list[int] = []

    def probe(arguments, *, idempotency_key):
        probe_calls.append(1)
        if provider_record.exists():
            return OutcomeProbeResult(
                status=ProbeStatus.COMMITTED,
                output=provider_result,
                provider_operation_id="provider-resource-R-CRASH",
                evidence={"source": "durable fake provider record"},
            )
        return OutcomeProbeResult(
            status=ProbeStatus.NOT_COMMITTED,
            evidence={"source": "durable fake provider record", "terminal": True},
        )

    contract = EffectContract(
        name="provision_resource",
        operation_id=lambda args: f"resource:{args['resource_id']}:create",
        outcome_probe=probe,
    )
    ledger = SQLiteEffectLedger(database)
    executor = EffectExecutor(ledger=ledger, lease_seconds=5)

    recovered = executor.execute(
        contract=contract,
        arguments={"resource_id": "R-CRASH"},
        handler=lambda arguments, *, idempotency_key: handler_calls.append(1),
    )
    final_record = ledger.get(recovered.operation_id)

    assert recovered.status is EffectStatus.CONFIRMED
    assert recovered.output == {
        "resource_id": "R-CRASH",
        "status": "created",
    }
    assert final_record is not None
    assert final_record.attempts == 1
    assert final_record.status is EffectStatus.CONFIRMED
    assert handler_calls == []
    assert len(probe_calls) == 1
    assert [
        event["event_type"] for event in ledger.events(recovered.operation_id)
    ] == ["prepared", "dispatched", "outcome_unknown", "confirmed"]
