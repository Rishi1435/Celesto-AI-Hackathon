"""Derive submission evidence from preserved provider-backed run records."""

import json
import math
import statistics
from pathlib import Path

from app.evidence import write_json


def reliability(rows):
    runs = []
    for row in rows:
        d = row["details"]
        runs.append(
            {
                "sandbox_name": row["sandbox_name"],
                "fresh": d["fresh"],
                "success": d["success"],
                **{k: d[k] for k in ["wall_clock_s", "boot_to_ready_s", "cost_usd"]},
                "provider_computer_id": d.get("provider_computer_id"),
                "outcome": d["outcome"],
                "scenario": d["scenario"],
            }
        )
    walls = sorted(r["wall_clock_s"] for r in runs)
    return dict(
        status="measured" if len(runs) >= 20 else "incomplete",
        run_count=len(runs),
        runs=runs,
        success_rate=sum(r["success"] for r in runs) / len(runs) if runs else None,
        median_wall_clock_s=statistics.median(walls) if runs else None,
        p90_wall_clock_s=walls[math.ceil(0.9 * len(walls)) - 1] if runs else None,
        median_boot_to_ready_s=statistics.median(r["boot_to_ready_s"] for r in runs) if runs else None,
        total_cost_usd=sum(r["cost_usd"] for r in runs),
        cost_basis="Estimate from measured elapsed time and configured token prices; not provider billing",
        postmortem_path="reports/postmortem.md",
    )


def build_reports(root: Path):
    rows = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((root / "reports" / "evidence" / "runs").glob("*.json"))
    ]
    batch_path = root / "reports" / "evaluation_batch.json"
    if batch_path.exists():
        ids = json.loads(batch_path.read_text(encoding="utf-8"))["run_ids"]
        indexed = {row["run_id"]: row for row in rows}
        cohort = [indexed[run_id] for run_id in ids if run_id in indexed]
    else:
        cohort = rows
    unallocated = [row for row in cohort if not row["details"].get("allocation_confirmed", False)]
    allocated = [row for row in cohort if row["details"].get("allocation_confirmed", False)]
    report = reliability(allocated)
    if batch_path.exists():
        manifest = json.loads(batch_path.read_text(encoding="utf-8"))
        report["cohort_policy"] = manifest.get("cohort_policy", "Preserved evaluation cohort")
        report["worker_history"] = manifest.get("worker_history", [])
    report["allocation_failures"] = [
        dict(
            run_id=r["run_id"],
            sandbox_name=r["sandbox_name"],
            error_type=r["details"].get("error_type"),
            error_message=r["details"].get("error_message"),
        )
        for r in unallocated
    ]
    write_json(root / "reports" / "reliability.json", report)
    successful_cleanup = {
        r["details"]["outcome"]
        for r in rows
        if r["details"]["cleanup_confirmed"] and r["details"].get("allocation_confirmed")
    }
    lifecycle = dict(
        cleanup_on_success="succeeded" in successful_cleanup,
        cleanup_on_failure="failed" in successful_cleanup,
        cleanup_on_interrupt="interrupted" in successful_cleanup,
        sandbox_names=[r["sandbox_name"] for r in rows if r["details"].get("allocation_confirmed")],
        status="measured" if {"succeeded", "failed", "interrupted"} <= successful_cleanup else "pending",
    )
    write_json(root / "reports" / "lifecycle.json", lifecycle)
    containment = [
        r
        for r in rows
        if r["details"].get("containment", {}).get("host_untouched")
        and r["details"].get("containment", {}).get("blocked")
        and r["details"].get("allocation_confirmed")
        and r["details"].get("cleanup_confirmed")
    ]
    if containment:
        write_json(
            root / "reports" / "containment.json",
            dict(status="measured", **containment[-1]["details"]["containment"]),
        )
    persistence = [
        row
        for row in rows
        if row["details"].get("allocation_confirmed")
        and any(
            event.get("capability") == "persistence" and event.get("checkpoint_verified") is True
            for event in row["details"].get("events", [])
        )
    ]
    capabilities_path = root / "reports" / "capabilities.json"
    if capabilities_path.exists():
        capabilities = json.loads(capabilities_path.read_text(encoding="utf-8"))
        observed = {"persistence": persistence, "safety_hooks": containment}
        for capability in capabilities["capabilities"]:
            matched = observed.get(capability["id"], [])
            if matched:
                capability["implementation_path"] = "app/sandbox.py"
                capability["evidence_path"] = f"reports/evidence/{matched[-1]['sandbox_name']}.jsonl"
                capability["observed"] = True
        capabilities["status"] = "measured" if persistence and containment else "live_evidence_incomplete"
        write_json(capabilities_path, capabilities)
    failed = [r for r in cohort if not r["details"]["success"]]
    if rows:
        body = (
            "# Eval post-mortem\n\n"
            f"The preserved evaluation cohort contains {len(allocated)} confirmed fresh computers, "
            f"{len(unallocated)} allocation failures, and {len(failed)} "
            "unsuccessful outcomes. Failed rows remain in reliability.json. Success requires an exact match with "
            "the independent integer-cent oracle, a matching checkpoint after stop/resume, and confirmed deletion. "
            "A model response alone never establishes success.\n\n"
            "The cohort includes the first end-to-end smoke run and all subsequently confirmed reconciliation "
            "computers, including intentional interruption and failed model attempts. No allocated failed row "
            "is removed. Pre-allocation refusals are kept in allocation_failures and cannot count toward twenty "
            "fresh computers. This is an engineering cohort, not a fixed-version model comparison.\n\n"
            "Elapsed time includes boot, model calls, persistence verification, and cleanup. Cost is an estimate "
            "from configured runtime and token rates. Zero model prices can mean verified free models or "
            "unconfigured prices; each run records its free-only policy and provider-reported model cost when available. "
            "Compare against provider invoices before reporting billed cost.\n\n"
        )
        for row in failed:
            d = row["details"]
            body += (
                f"- {row['sandbox_name']}: outcome={d['outcome']}, error={d.get('error_type', 'none')}, "
                f"cleanup_confirmed={d['cleanup_confirmed']}. {d.get('error_message', '')} "
                f"Evidence: reports/evidence/{row['sandbox_name']}.jsonl\n"
            )
        if not failed:
            body += "No failures were observed in this cohort. This does not establish robustness outside these synthetic fixtures.\n"
        body += (
            "\nObserved failure analysis: two early generated programs required whole rupees instead of integral "
            "cents, rejecting valid values such as 12.34. Their repair attempts also produced syntax errors. "
            "The original contract and a failed program are archived under reports/evidence/. The clarified "
            "contract explicitly distinguishes 12.34 (valid) from 12.345 (invalid); later runs record its SHA-256. "
            "Earlier failures remain scored. A separate early attempt received HTTP 400 from the model provider; "
            "its original response body was unavailable, so its precise cause is not claimed. New diagnostics "
            "retain a bounded, credential-redacted provider error message.\n"
            "\nThe later free Nemotron phase had a two-call budget. Several attempts exhausted that budget "
            "without an accepted Python execution request, and a provider HTTP 502 was observed. These were "
            "not relabeled as successes. The free North model produced the seven verified outputs; the local "
            "default was restored to it. Contracts, models and call budgets changed between documented phases, "
            "so this cohort cannot establish an isolated model ranking or production readiness. "
            "Timing summaries include fast failures and the deliberate interruption.\n"
            "\nKnown limits: synthetic exports use one schema and small files. Stop/resume tests disk persistence, "
            "not full memory snapshots. SIGKILL and host power loss require manual orphan reconciliation. "
            "The pre-execution hook restricts shell entry points, but generated Python still has guest permissions. "
            "The pinned cloud SDK does not expose an outbound network policy; no network containment is claimed.\n"
        )
        (root / "reports" / "postmortem.md").write_text(body, encoding="utf-8")
    return report
