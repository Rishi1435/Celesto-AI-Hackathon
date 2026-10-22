import hashlib
import json
import re
import threading
import time
from pathlib import Path

from app.evidence import append_jsonl, utcnow, write_json
from app.fixtures import CONTRACT, make_fixture, oracle, verify_report
from app.model import ReconciliationAgent
from app.sandbox import Cancelled, CommandBlockedError, GuestCommandError, Sandbox


def safe_error(error, settings):
    message = str(error)
    for value in [settings.celesto_key, settings.llm_key, settings.access_token]:
        if value:
            message = message.replace(value, "[redacted]")
    message = re.sub(r"https?://\S+", "[endpoint]", message)
    message = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", message)
    return message[:1200]


class JobRunner:
    def __init__(self, settings, ledger, root, sandbox_factory=None, agent_factory=ReconciliationAgent):
        self.settings, self.ledger, self.root = settings, ledger, Path(root)
        self.sandbox_factory, self.agent_factory = sandbox_factory, agent_factory

    def run(self, row, seed, scenario, cancel: threading.Event):
        name, run_id = row["sandbox_name"], row["run_id"]
        start, started_at = time.perf_counter(), utcnow()
        evidence_path = self.root / "reports" / "evidence" / f"{name}.jsonl"
        details = dict(started_at=started_at, scenario=scenario, seed=seed, outcome="running", events=[])
        details["configuration"] = {
            "provider": self.settings.provider,
            "template": self.settings.template,
            "llm_model": self.settings.llm_model,
            "llm_free_only": self.settings.llm_free_only,
            "contract_sha256": hashlib.sha256(CONTRACT.encode()).hexdigest(),
            "max_model_turns": self.settings.max_model_turns,
            "command_timeout_s": self.settings.command_timeout,
            "sandbox_hourly_usd": self.settings.sandbox_hourly_usd,
            "llm_input_usd_per_million": self.settings.llm_input_price,
            "llm_output_usd_per_million": self.settings.llm_output_price,
        }
        sandbox_args = {} if self.sandbox_factory is None else {"factory": self.sandbox_factory}
        sandbox = Sandbox(self.settings, name, self.root, cancel, **sandbox_args)
        agent = self.agent_factory(self.settings, cancel)
        final_state = "failed"
        deleted_at = None

        def event(kind, **extra):
            entry = dict(ts=utcnow(), sandbox_name=name, event=kind, **extra)
            details["events"].append(entry)
            append_jsonl(evidence_path, entry)
            if kind in {"step_failed", "policy_applied", "resumed"}:
                append_jsonl(self.root / "reports" / "failure_trace.jsonl", entry)

        def state(value):
            self.ledger.update(run_id, value, details)
            event("state_changed", state=value)

        try:
            event("step_started", step=1, label="create fresh computer")
            sandbox.create()
            details["ready_at"] = utcnow()
            computer_id = getattr(sandbox.computer, "id", None) or getattr(sandbox.computer, "vm_id", None)
            if not computer_id:
                raise ValueError("Celesto returned no provider computer ID; allocation cannot be verified")
            details["provider_computer_id"] = str(computer_id)
            event(
                "provider_attached",
                provider_computer_id=details["provider_computer_id"],
                sandbox_name_kind="unique control-plane alias",
            )
            state("created")
            event("step_completed", step=1)
            state("ready")
            event("step_completed", step=2, boot_to_ready_s=sandbox.boot_s)
            files = make_fixture(seed)
            expected = oracle(files)
            state("running")
            sandbox.write_files(files)
            event("step_completed", step=3, label="upload synthetic exports")
            sandbox.inspect()
            event("step_completed", step=4, label="inspect untrusted data")
            if scenario == "containment":
                # Keep observed safety evidence even if the later model call fails.
                details["containment"] = self.containment(sandbox, event)
            report = agent.run(sandbox, lambda value: verify_report(value, expected))
            event("step_completed", step=5, label="model program verified")
            digest = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
            checkpoint = dict(last_completed_step=6, report_sha256=digest, seed=seed)
            sandbox.write_files({"checkpoint.json": json.dumps(checkpoint)})
            event("step_completed", step=6, label="checkpoint completed work")
            if scenario == "interrupt":
                cancel.set()
                sandbox.check_cancelled()
            resumed = False
            if scenario == "resume":
                try:
                    sandbox.python("raise RuntimeError('DESIGNED_STEP_7_FAILURE')")
                except GuestCommandError as error:
                    if "DESIGNED_STEP_7_FAILURE" not in str(error):
                        raise
                    event(
                        "step_failed", step=7, state="MidTaskFailure", reason="designed crash before export"
                    )
                    event("policy_applied", step=7, policy="resume")
                    sandbox.restart_and_verify(checkpoint)
                    event("resumed", step=7, state="Resumed", policy="resume")
                    resumed = True
            report = sandbox.read_report()
            verify_report(report, expected)
            event("step_completed", step=7, label="retry safe read-only export")
            if not resumed:
                sandbox.restart_and_verify(checkpoint)
            event("step_completed", step=8, capability="persistence", checkpoint_verified=True)
            if scenario == "failure":
                raise RuntimeError("Designed fatal failure after persistence verification")
            event("step_completed", step=9, label="containment probe or ordinary review")
            report = sandbox.read_report()
            verify_report(report, expected)
            write_json(self.root / "reports" / "evidence" / f"{name}-reconciled.json", report)
            # This is exported untrusted source, never imported by the control plane.
            (self.root / "reports" / "evidence" / f"{name}-agent.txt").write_text(
                agent.source or "", encoding="utf-8"
            )
            details["report_path"] = f"reports/evidence/{name}-reconciled.json"
            details["report_sha256"] = digest
            details["outcome"] = "succeeded"
            event("step_completed", step=10, label="independently verify exported proof")
            state("succeeded")
            final_state = "succeeded"
        except (Cancelled, KeyboardInterrupt):
            details["outcome"] = "interrupted"
            event("interrupted")
        except Exception as error:
            details["outcome"] = "failed"
            # Avoid HTTP response bodies/URLs that could carry credentials.
            details["error_type"] = type(error).__name__
            details["error_message"] = safe_error(error, self.settings)
            details["provider_http_status"] = getattr(error, "status_code", None)
            details["provider_side_effects"] = getattr(error, "side_effects", None)
            event("run_failed", error_type=type(error).__name__, error_message=details["error_message"])
        finally:
            # Preserve IDs even when the provider allocates a VM but the readiness probe fails.
            provider_id = (
                (getattr(sandbox.computer, "id", None) or getattr(sandbox.computer, "vm_id", None))
                if sandbox.computer is not None
                else None
            )
            if provider_id is not None:
                details["provider_computer_id"] = str(provider_id)
            try:
                # Deletion does not consult the cancellation flag.
                sandbox.cleanup()
                deleted_at = utcnow() if sandbox.deleted else None
                event(
                    "cleanup_confirmed" if sandbox.deleted else "no_allocation_to_delete",
                    previous_outcome=details["outcome"],
                    deleted=sandbox.deleted,
                )
                final_state = "deleted" if sandbox.deleted else "failed"
            except Exception as error:
                details["cleanup_error"] = type(error).__name__
                details["manual_cleanup_required"] = name
                event("cleanup_failed", error_type=type(error).__name__)
                final_state = "failed"
            elapsed = time.perf_counter() - start
            details.update(
                finished_at=utcnow(),
                deleted_at=deleted_at,
                wall_clock_s=elapsed,
                boot_to_ready_s=sandbox.boot_s,
                fresh=provider_id is not None,
                allocation_confirmed=provider_id is not None,
                allocation_rejected=sandbox.allocation_rejected,
                cleanup_not_required=sandbox.cleanup_not_required,
                sandbox_created=sandbox.created,
                cleanup_confirmed=sandbox.deleted,
                success=details["outcome"] == "succeeded" and sandbox.deleted,
                sandbox_cost_usd_estimate=(
                    elapsed / 3600 * self.settings.sandbox_hourly_usd if provider_id is not None else 0
                ),
                llm_cost_usd_estimate=agent.cost(),
                input_tokens=agent.input_tokens,
                output_tokens=agent.output_tokens,
                provider_reported_llm_cost_usd=getattr(agent, "reported_cost_usd", None),
                models_used=getattr(agent, "models_used", []),
                cost_basis="estimated elapsed time × configured sandbox rate plus token rates; not an invoice",
            )
            details["cost_usd"] = details["sandbox_cost_usd_estimate"] + details["llm_cost_usd_estimate"]
            write_json(
                self.root / "reports" / "evidence" / "runs" / f"{run_id}.json",
                {
                    "run_id": run_id,
                    "sandbox_name": name,
                    "state": final_state,
                    "execution_target": "celesto_sandbox",
                    "details": details,
                },
            )
            # Save evidence first, so a ledger outage cannot erase the cleanup result.
            self.ledger.update(run_id, final_state, details)
        return details

    def containment(self, sandbox, event):
        canary = self.root / "reports" / "evidence" / f"{sandbox.name}-host-canary.txt"
        canary.write_text("host-only synthetic canary; no credentials\n", encoding="utf-8")
        before = hashlib.sha256(canary.read_bytes()).hexdigest()
        blocked = False
        try:
            sandbox.request("rm -rf -- " + str(canary))
        except CommandBlockedError:
            blocked = True
        # Actually execute a destructive file action in the guest, targeting a disposable guest canary.
        # The corresponding host path must be invisible and its content hash must remain unchanged.
        invisible = (
            sandbox.python(
                "from pathlib import Path\n"
                "guest=Path('/workspace/destructible.txt');guest.write_text('guest-only')\n"
                "guest.unlink();assert not guest.exists()\n"
                f"host=Path({str(canary)!r});print(host.exists())\n"
                "assert not host.exists(),'host canary unexpectedly visible'\n"
                "host.unlink(missing_ok=True)"
            ).strip()
            == "False"
        )
        after = hashlib.sha256(canary.read_bytes()).hexdigest()
        result = dict(
            attack="destructive_command",
            blocked=blocked,
            host_untouched=before == after,
            host_path_invisible=invisible,
            guest_destructive_action_completed=True,
            before_sha256=before,
            after_sha256=after,
            network_policy="not claimed for cloud Python SDK 0.1.8",
            evidence_paths=[f"reports/evidence/{sandbox.name}.jsonl", "logs/commands.jsonl"],
        )
        event("containment_probe", **result)
        if not all([blocked, before == after, invisible]):
            raise ValueError("Containment probe did not meet every requirement")
        return result
