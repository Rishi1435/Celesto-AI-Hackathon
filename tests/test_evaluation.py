import json
from types import SimpleNamespace

import httpx

from scripts.evaluate import allocation_deadline, evaluate_batch


def test_resuming_cohort_preserves_failed_rows_and_caps_in_flight_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.evaluate.ROOT", tmp_path)
    monkeypatch.setattr("scripts.evaluate.time.sleep", lambda seconds: None)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "evaluation_batch.json").write_text(
        json.dumps(
            {
                "started_at": "2026-10-24T00:00:00+00:00",
                "run_ids": ["old-failed", "old-success"],
            }
        )
    )
    rows = {
        key: {
            "run_id": key,
            "sandbox_name": "sbx-" + key.replace("-", ""),
            "details": {
                "finished_at": "done",
                "success": key != "old-failed",
                "allocation_confirmed": True,
                "cleanup_confirmed": True,
                "outcome": "failed" if key == "old-failed" else "succeeded",
            },
        }
        for key in ["old-failed", "old-success"]
    }
    active, seeds = set(), []
    peak = 0

    def handler(request):
        nonlocal peak
        if request.method == "POST":
            body = json.loads(request.content)
            seeds.append(body["seed"])
            run_id = f"new-{len(seeds)}"
            rows[run_id] = {
                "run_id": run_id,
                "sandbox_name": f"sbx-new{len(seeds):08}",
                "details": {
                    "finished_at": "done",
                    "success": True,
                    "allocation_confirmed": True,
                    "cleanup_confirmed": True,
                    "outcome": "succeeded",
                },
            }
            active.add(run_id)
            peak = max(peak, len(active))
            return httpx.Response(202, json=rows[run_id])
        run_id = request.url.path.rsplit("/", 1)[1]
        active.discard(run_id)
        return httpx.Response(200, json=rows[run_id])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        failed = evaluate_batch(
            client, SimpleNamespace(count=20, workers=5, resume=True, base_url="https://fixture.invalid")
        )
    batch = json.loads((reports / "evaluation_batch.json").read_text())
    assert failed is True
    assert batch["run_ids"][:2] == ["old-failed", "old-success"]
    assert len(batch["run_ids"]) == len(set(batch["run_ids"])) == 20
    assert seeds == list(range(3, 21))
    assert peak == 5


def test_server_retry_after_is_an_exact_allocation_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.evaluate.ROOT", tmp_path)
    directory = tmp_path / "reports" / "evidence"
    directory.mkdir(parents=True)
    (directory / "provider_errors.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-10-24T08:19:37+00:00",
                "http_status": 429,
                "retry_after": "2135",
            }
        )
        + "\n"
    )
    from datetime import datetime

    assert allocation_deadline() == datetime.fromisoformat("2026-10-24T08:55:12+00:00").timestamp()
