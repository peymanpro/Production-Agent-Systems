from __future__ import annotations

def test_end_to_end_runtime_demo_handles_lost_ack_once():
    from examples.effect_assurance_demo import run_demo

    report = run_demo()

    assert report["run_status"] == "completed"
    assert report["effect_status"] == "confirmed"
    assert report["committed_effect_count"] == 1
    assert report["duplicate_effect_detected"] is False
    assert report["ledger_event_count"] >= 4
