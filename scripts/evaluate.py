"""Drive real sandbox jobs through HTTP; never manufacture run measurements."""

import argparse
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import httpx

from app.config import ROOT, Settings
from app.evidence import utcnow, write_json
from app.reporting import build_reports


def wait_for_job(client, base_url, run_id):
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        response = client.get(base_url + "/api/runs/" + run_id)
        response.raise_for_status()
        row = response.json()
        if "finished_at" in row.get("details", {}):
            return row
        time.sleep(1)
    raise TimeoutError(f"Run {run_id} still active. Inspect the ledger; do not blindly replay it.")


def submit(client, base_url, scenario, seed):
    response = client.post(base_url + "/api/jobs", json=dict(scenario=scenario, seed=seed))
    response.raise_for_status()
    return response.json()


def allocation_deadline():
    path = ROOT / "reports" / "evidence" / "provider_errors.jsonl"
    if not path.exists():
        return 0
    deadline = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("http_status") != 429 or not str(event.get("retry_after", "")).isdigit():
            continue
        seconds = int(event["retry_after"])
        if 0 < seconds <= 86400:
            deadline = max(deadline, datetime.fromisoformat(event["ts"]).timestamp() + seconds)
    return deadline


def evaluate_batch(client, args):
    batch_path = ROOT / "reports" / "evaluation_batch.json"
    if args.count < 20 or not 1 <= args.workers <= 10:
        raise ValueError("Evaluation needs at least 20 runs and between 1 and 10 workers")
    if args.resume:
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
    else:
        if batch_path.exists():
            if not getattr(args, "new_cohort", False):
                raise ValueError("A cohort exists; use --resume or explicitly archive it with --new-cohort")
            archive = ROOT / "reports/cohorts" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            archive.mkdir(parents=True, exist_ok=False)
            for filename in ["evaluation_batch.json", "reliability.json", "postmortem.md"]:
                source = ROOT / "reports" / filename
                if source.exists():
                    shutil.copy2(source, archive / filename)
        batch = dict(started_at=utcnow(), run_ids=[])
    if getattr(args, "include_existing", False):
        records = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in (ROOT / "reports" / "evidence" / "runs").glob("*.json")
        ]
        for row in sorted(records, key=lambda value: value["details"]["started_at"]):
            if row["details"].get("allocation_confirmed") and row["run_id"] not in batch["run_ids"]:
                batch["run_ids"].append(row["run_id"])
        batch["cohort_policy"] = (
            "All confirmed end-to-end reconciliation computers, including the initial smoke and all failures; "
            "cohort allocation refusals are preserved separately. No allocated failures are omitted."
        )
    if "worker_history" not in batch and batch["run_ids"]:
        batch["worker_history"] = [dict(ts=batch["started_at"], workers=1, note="original serial dispatcher")]
    batch.setdefault("worker_history", []).append(dict(ts=utcnow(), workers=args.workers))
    batch["target_fresh_count"] = args.count
    batch.pop("finished_at", None)
    write_json(batch_path, batch)
    pending = set(batch["run_ids"])
    completed, confirmed, newly_submitted = set(), set(), set()
    results, cancel_requested = {}, set()
    cooldown_until = allocation_deadline()
    failed, stop_admission = False, False
    while pending or len(confirmed) < args.count and not stop_admission:
        for run_id in [value for value in batch["run_ids"] if value in pending]:
            response = client.get(args.base_url + "/api/runs/" + run_id)
            response.raise_for_status()
            row = response.json()
            details = row.get("details", {})
            if (
                getattr(args, "special_probes", False)
                and details.get("scenario") == "interrupt"
                and details.get("provider_computer_id")
                and run_id not in cancel_requested
                and "finished_at" not in details
            ):
                cancellation = client.post(args.base_url + "/api/jobs/" + run_id + "/cancel")
                if cancellation.status_code == 202:
                    cancel_requested.add(run_id)
            if "finished_at" not in details:
                continue
            pending.remove(run_id)
            completed.add(run_id)
            results[run_id] = details
            if details.get("allocation_confirmed"):
                confirmed.add(run_id)
            failed |= not details["success"]
            print(
                f"fresh={len(confirmed)}/{args.count} attempts={len(completed)} "
                f"{row['sandbox_name']} {details['outcome']} "
                f"cleanup={details['cleanup_confirmed']}",
                flush=True,
            )
            if not details["cleanup_confirmed"] and (
                not details.get("cleanup_not_required") or run_id in newly_submitted
            ):
                retry_at = allocation_deadline()
                if (
                    details.get("cleanup_not_required")
                    and details.get("provider_http_status") == 429
                    and retry_at > time.time()
                ):
                    cooldown_until = max(cooldown_until, retry_at)
                else:
                    stop_admission = True
                    print(
                        "Allocation/deletion needs inspection; draining accepted jobs without admitting more.",
                        file=sys.stderr,
                        flush=True,
                    )
        while (
            not stop_admission
            and time.time() >= cooldown_until
            and len(pending) < args.workers
            and len(confirmed) + len(pending) < args.count
        ):
            index = len(batch["run_ids"])
            scenario = "resume" if index % 5 == 0 else "normal"
            if getattr(args, "special_probes", False):
                if not any(
                    d.get("containment", {}).get("host_untouched") and d.get("cleanup_confirmed")
                    for d in results.values()
                ):
                    scenario = "containment"
                elif not any(
                    d.get("outcome") == "interrupted" and d.get("cleanup_confirmed") for d in results.values()
                ):
                    scenario = "interrupt"
            try:
                row = submit(client, args.base_url, scenario, index + 1)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 429:
                    raise
                break  # An explicit capacity rejection allocated nothing; retry after a pending run finishes.
            run_id = row["run_id"]
            pending.add(run_id)
            newly_submitted.add(run_id)
            batch["run_ids"].append(run_id)
            write_json(batch_path, batch)
        if pending:
            time.sleep(1)
        elif cooldown_until > time.time() and not stop_admission:
            batch["next_allocation_not_before"] = datetime.fromtimestamp(
                cooldown_until, timezone.utc
            ).isoformat()
            write_json(batch_path, batch)
            remaining = cooldown_until - time.time()
            print(f"Provider cooldown: {remaining:.0f}s remaining; no new sandbox is allocated.", flush=True)
            time.sleep(min(55, remaining))
        elif len(confirmed) < args.count and not stop_admission:
            raise RuntimeError(
                "API capacity is occupied by another batch; inspect the ledger before retrying"
            )
    batch["last_dispatch_finished_at"] = utcnow()
    batch["status"] = "measured" if len(confirmed) >= args.count else "incomplete"
    if len(confirmed) >= args.count:
        batch["finished_at"] = utcnow()
    write_json(batch_path, batch)
    return failed or len(confirmed) < args.count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=["smoke", "eval", "lifecycle", "containment", "concurrency", "reports"]
    )
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--include-existing", action="store_true")
    parser.add_argument("--special-probes", action="store_true")
    parser.add_argument("--new-cohort", action="store_true")
    args = parser.parse_args()
    if args.mode == "reports":
        print(json.dumps(build_reports(ROOT), indent=2))
        return 0
    settings = Settings.from_env()
    headers = {"Authorization": "Bearer " + settings.access_token} if settings.access_token else {}
    failed = False
    with httpx.Client(timeout=30, headers=headers) as client:
        client.get(args.base_url + "/health").raise_for_status()
        if args.mode == "eval":
            failed = evaluate_batch(client, args)
        elif args.mode == "concurrency":
            jobs, rejections, rows = [], [], []
            # Dispatch all five before waiting, to measure actual overlap, not sequential labels.
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [pool.submit(submit, client, args.base_url, "normal", 100 + i) for i in range(5)]
                for future in as_completed(futures):
                    try:
                        jobs.append(future.result())
                    except httpx.HTTPStatusError as error:
                        rejections.append(error.response.status_code)
            for job in jobs:
                rows.append(wait_for_job(client, args.base_url, job["run_id"]))
            completed = [r for r in rows if r["details"].get("allocation_confirmed")]
            intervals = [
                (r["details"]["ready_at"], r["details"]["deleted_at"])
                for r in completed
                if r["details"].get("ready_at") and r["details"].get("deleted_at")
            ]
            # Strict concurrency: at some instant all five must have been in flight.
            peak = max(
                (sum(start <= instant < end for start, end in intervals) for instant, _ in intervals),
                default=0,
            )
            report = dict(
                status="measured" if len(completed) >= 5 and peak >= 5 else "incomplete",
                jobs=[
                    dict(
                        sandbox_name=r["sandbox_name"],
                        started_at=r["details"]["started_at"],
                        finished_at=r["details"]["finished_at"],
                        success=r["details"]["success"],
                        provider_computer_id=r["details"]["provider_computer_id"],
                    )
                    for r in completed
                ],
                peak_retained_computers=peak,
                breakage_notes=(
                    f"Attempted five concurrent HTTP jobs; {len(completed)} allocated computers; "
                    f"peak retained ready computers {peak}; HTTP rejections {rejections}; "
                    f"outcomes {[r['details']['outcome'] for r in rows]}. "
                    "Watched admission limits, independent provider IDs, per-run evidence and deletion results."
                ),
            )
            write_json(ROOT / "reports" / "concurrency.json", report)
            failed = (
                len(completed) < 5 or peak < 5 or any(not r["details"]["cleanup_confirmed"] for r in rows)
            )
        else:
            scenarios = {
                "smoke": ["normal"],
                "lifecycle": ["normal", "failure", "interrupt"],
                "containment": ["containment"],
            }[args.mode]
            if args.mode == "eval" and args.count < 20:
                parser.error("eval requires --count >= 20; use smoke for one run")
            batch = dict(started_at=utcnow(), run_ids=[])
            for i, scenario in enumerate(scenarios):
                row = submit(client, args.base_url, scenario, i + 1)
                batch["run_ids"].append(row["run_id"])
                if args.mode == "eval":
                    write_json(ROOT / "reports" / "evaluation_batch.json", batch)
                result = wait_for_job(client, args.base_url, row["run_id"])
                d = result["details"]
                print(
                    f"{i + 1}/{len(scenarios)} {row['sandbox_name']} {d['outcome']} cleanup={d['cleanup_confirmed']}",
                    flush=True,
                )
                if not d["cleanup_confirmed"]:
                    if d.get("cleanup_not_required"):
                        print(
                            "Provider rejected allocation; no guest exists. Stopping this batch.",
                            file=sys.stderr,
                        )
                        failed = True
                        break
                    # Stop admitting work after an orphan; the trial allows only one retained computer.
                    print(
                        "Deletion unconfirmed; stopping this batch. Inspect the saved provider id.",
                        file=sys.stderr,
                    )
                    failed = True
                    break
                if scenario in {"normal", "resume", "containment"} and not d["success"]:
                    failed = True
    build_reports(ROOT)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
