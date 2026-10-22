"""Describe observed concurrency and rejected parallel allocations honestly."""

import json
from datetime import datetime

from app.config import ROOT
from app.evidence import write_json


def main():
    batch = json.loads((ROOT / "reports/evaluation_batch.json").read_text())
    rows = [json.loads(path.read_text()) for path in (ROOT / "reports/evidence/runs").glob("*.json")]
    rows = [row for row in rows if row["run_id"] in batch["run_ids"]]
    allocated = sorted(
        [row for row in rows if row["details"].get("allocation_confirmed")],
        key=lambda row: row["details"]["started_at"],
    )
    history = batch.get("worker_history", [])
    parallel = next((i for i, item in enumerate(history) if item["workers"] >= 5), None)
    attempted = []
    if parallel is not None:
        start = history[parallel]["ts"]
        end = history[parallel + 1]["ts"] if parallel + 1 < len(history) else "9999"
        attempted = [
            row
            for row in rows
            if start <= row["details"]["started_at"] < end and not row["details"].get("allocation_confirmed")
        ]
    intervals = [
        (
            datetime.fromisoformat(row["details"]["ready_at"]),
            datetime.fromisoformat(row["details"]["deleted_at"]),
        )
        for row in allocated
        if row["details"].get("ready_at") and row["details"].get("deleted_at")
    ]
    peak = max(
        (sum(begin <= instant < finish for begin, finish in intervals) for instant, _ in intervals), default=0
    )
    report = {
        "status": "measured" if peak >= 5 else "measured_with_provider_limit",
        "jobs": [
            {
                "sandbox_name": row["sandbox_name"],
                "started_at": row["details"]["started_at"],
                "finished_at": row["details"]["finished_at"],
                "provider_computer_id": row["details"]["provider_computer_id"],
                "cleanup_confirmed": row["details"]["cleanup_confirmed"],
            }
            for row in allocated
        ],
        "peak_retained_computers": peak,
        "parallel_allocation_attempts": [
            {
                "sandbox_name": row["sandbox_name"],
                "started_at": row["details"]["started_at"],
                "http_status": row["details"].get("provider_http_status"),
                "allocation_confirmed": False,
            }
            for row in attempted
        ],
        "breakage_notes": (
            f"Dispatched five parallel allocation attempts; {len(attempted)} were refused before a computer ID "
            f"was returned. A subsequent redacted provider diagnostic confirmed the trial cap of ten NEW "
            f"sandboxes per hour, despite usage-overview reporting a running limit of ten. The preserved cohort "
            f"contains {len(allocated)} real uniquely named computers; peak overlapping ready-to-delete intervals "
            f"was {peak}. Watched unique names, provider IDs, independent report paths, rate limits and cleanup. "
            "Five simultaneously running guest VMs are not claimed."
        ),
        "evidence_paths": [
            "reports/evaluation_batch.json",
            "reports/evidence/provider_errors.jsonl",
            "reports/evidence/provider_limits.json",
            "logs/commands.jsonl",
        ],
    }
    write_json(ROOT / "reports/concurrency.json", report)
    print(
        json.dumps(
            {
                "actual_jobs": len(allocated),
                "peak_retained_computers": peak,
                "parallel_rejections": len(attempted),
            }
        )
    )


if __name__ == "__main__":
    main()
