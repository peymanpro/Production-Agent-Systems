from __future__ import annotations

import json

from production_agent_systems.effect_cli import main
from production_agent_systems.effects import (
    EffectContract,
    EffectExecutor,
    EffectStatus,
    SQLiteEffectLedger,
)


def _quarantine_one_operation(database):
    contract = EffectContract(
        name="send_email",
        operation_id=lambda args: f"email:{args['message_id']}:send:v1",
    )
    ledger = SQLiteEffectLedger(database)
    result = EffectExecutor(ledger=ledger).execute(
        contract=contract,
        arguments={"message_id": "MSG-CLI-1"},
        handler=lambda arguments, *, idempotency_key: (
            _raise_timeout()
        ),
    )
    assert result.status is EffectStatus.QUARANTINED
    return result.operation_id


def _raise_timeout():
    raise TimeoutError("acknowledgement lost")


def test_operations_cli_lists_quarantine_without_exposing_payload(tmp_path, capsys):
    database = tmp_path / "effects.sqlite3"
    operation_id = _quarantine_one_operation(database)

    result_code = main(
        ["--db", str(database), "list", "--status", "quarantined"]
    )
    output = json.loads(capsys.readouterr().out)

    assert result_code == 0
    assert len(output) == 1
    assert output[0]["operation_id"] == operation_id
    assert output[0]["status"] == "quarantined"
    assert "request_fingerprint" not in output[0]
    assert "evidence" not in output[0]


def test_operations_cli_records_manual_confirmation_with_audit_event(tmp_path, capsys):
    database = tmp_path / "effects.sqlite3"
    operation_id = _quarantine_one_operation(database)

    result_code = main(
        [
            "--db",
            str(database),
            "confirm",
            operation_id,
            "--output-json",
            '{"message_id":"MSG-CLI-1","sent":true}',
            "--provider-operation-id",
            "mail-queue-42",
            "--actor",
            "on-call",
            "--reason",
            "Verified provider delivery event",
            "--evidence-reference",
            "INC-99/provider-event-42",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    ledger = SQLiteEffectLedger(database)

    assert result_code == 0
    assert output["status"] == "confirmed"
    assert output["resolved"] is True
    assert any(
        event["event_type"] == "manually_confirmed"
        for event in ledger.events(operation_id)
    )


def test_operations_cli_authorizes_retry_with_required_evidence(tmp_path, capsys):
    database = tmp_path / "effects.sqlite3"
    operation_id = _quarantine_one_operation(database)

    result_code = main(
        [
            "--db",
            str(database),
            "authorize-retry",
            operation_id,
            "--actor",
            "on-call",
            "--reason",
            "Provider confirms request was not committed and cannot commit later",
            "--evidence-reference",
            "INC-100/provider-terminal-query",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    record = SQLiteEffectLedger(database).get(operation_id)

    assert result_code == 0
    assert output["retry_authorized"] is True
    assert record is not None
    assert record.status is EffectStatus.PREPARED


def test_operations_cli_outputs_ordered_event_history(tmp_path, capsys):
    database = tmp_path / "effects.sqlite3"
    operation_id = _quarantine_one_operation(database)

    result_code = main(["--db", str(database), "events", operation_id])
    output = json.loads(capsys.readouterr().out)

    assert result_code == 0
    assert [event["event_type"] for event in output] == [
        "prepared",
        "dispatched",
        "outcome_unknown",
        "quarantined",
    ]
