import json

from app.evidence import write_json
from app.reporting import build_reports


def test_unallocated_attempts_cannot_satisfy_fresh_run_count(tmp_path):
    for i in range(20):
        write_json(
            tmp_path / "reports" / "evidence" / "runs" / f"{i}.json",
            {
                "run_id": str(i),
                "sandbox_name": f"sbx-attempt{i:08}",
                "details": {
                    "allocation_confirmed": False,
                    "cleanup_confirmed": False,
                    "outcome": "failed",
                    "success": False,
                    "error_type": "TimeoutError",
                    "error_message": "create outcome unknown",
                },
            },
        )
    report = build_reports(tmp_path)
    assert report["run_count"] == 0
    assert report["status"] == "incomplete"
    assert len(report["allocation_failures"]) == 20
    assert len(list((tmp_path / "reports" / "evidence" / "runs").glob("*.json"))) == 20
    lifecycle = json.loads((tmp_path / "reports" / "lifecycle.json").read_text())
    assert lifecycle["cleanup_on_failure"] is False


def test_extra_probes_dont_change_frozen_evaluation_cohort(tmp_path):
    for run_id, success in [("eval-failed", False), ("extra-success", True)]:
        write_json(
            tmp_path / "reports" / "evidence" / "runs" / f"{run_id}.json",
            {
                "run_id": run_id,
                "sandbox_name": "sbx-" + run_id.replace("-", ""),
                "details": {
                    "allocation_confirmed": True,
                    "cleanup_confirmed": True,
                    "fresh": True,
                    "outcome": "succeeded" if success else "failed",
                    "success": success,
                    "wall_clock_s": 1,
                    "boot_to_ready_s": 0.1,
                    "cost_usd": 0.01,
                    "scenario": "normal",
                },
            },
        )
    write_json(tmp_path / "reports" / "evaluation_batch.json", {"run_ids": ["eval-failed"]})
    report = build_reports(tmp_path)
    assert report["run_count"] == 1
    assert report["success_rate"] == 0
    assert report["runs"][0]["sandbox_name"] == "sbx-evalfailed"


def test_capabilities_point_at_observed_guest_evidence(tmp_path):
    write_json(
        tmp_path / "reports" / "capabilities.json",
        {
            "capabilities": [
                {"id": "persistence", "evidence_path": "app/sandbox.py"},
                {"id": "safety_hooks", "evidence_path": "app/sandbox.py"},
            ]
        },
    )
    write_json(
        tmp_path / "reports" / "evidence" / "runs" / "test.json",
        {
            "run_id": "test",
            "sandbox_name": "sbx-observed123",
            "details": {
                "allocation_confirmed": True,
                "cleanup_confirmed": True,
                "fresh": True,
                "outcome": "succeeded",
                "success": True,
                "wall_clock_s": 1,
                "boot_to_ready_s": 0.1,
                "cost_usd": 0.01,
                "scenario": "containment",
                "events": [{"capability": "persistence", "checkpoint_verified": True}],
                "containment": {
                    "attack": "destructive_command",
                    "blocked": True,
                    "host_untouched": True,
                    "evidence_paths": ["reports/evidence/sbx-observed123.jsonl"],
                },
            },
        },
    )
    build_reports(tmp_path)
    capabilities = json.loads((tmp_path / "reports" / "capabilities.json").read_text())
    assert capabilities["status"] == "measured"
    assert all(
        c["evidence_path"] == "reports/evidence/sbx-observed123.jsonl" for c in capabilities["capabilities"]
    )
