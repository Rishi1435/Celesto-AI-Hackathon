"""Lifecycle tests use SDK doubles. No guest or model program executes on the host."""

import ast
import base64
import json
import shlex
import threading
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.fixtures import oracle
from app.runner import JobRunner, safe_error
from app.sandbox import CommandBlockedError, Sandbox, inspection_metadata


class FakeComputer:
    id = "test-double-not-a-real-computer"

    def __init__(self, fail_delete=False):
        self.deleted = self.stops = self.resumes = self.starts = 0
        self.files = {}
        self.commands = []
        self.fail_delete = fail_delete

    def start(self):
        self.starts += 1

    def run(self, command, timeout):
        self.commands.append(command)
        wrapper = shlex.split(command)[2]
        # Parse the fixed wrapper, decode bytes, but never eval or exec anything.
        tree = ast.parse(wrapper)
        call = tree.body[1].value.args[0]
        source = base64.b64decode(ast.literal_eval(call.args[0])).decode()
        if "DESIGNED_STEP_7_FAILURE" in source:
            return SimpleNamespace(exit_code=1, stdout="", stderr="DESIGNED_STEP_7_FAILURE")
        if "files=" in source:
            for node in ast.parse(source).body:
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "files"
                ):
                    self.files.update(ast.literal_eval(node.value))
        if "print(Path('/workspace/checkpoint.json').read_text())" in source:
            output = self.files["checkpoint.json"]
        elif "csv.DictReader" in source:
            output = json.dumps(
                {
                    "sales.csv": {"columns": ["order_id", "amount", "notes"], "row_count": 10},
                    "payments.csv": {"columns": ["payment_id", "order_id", "amount"], "row_count": 8},
                }
            )
        elif "reconciled.json" in source:
            output = json.dumps(oracle(self.files))
        elif "host.unlink" in source:
            output = "False"
        else:
            output = "ready"
        return SimpleNamespace(exit_code=0, stdout=output, stderr="")

    def stop(self):
        self.stops += 1

    def resume(self):
        self.resumes += 1

    def delete(self):
        if self.fail_delete:
            raise RuntimeError("simulated deletion outage")
        self.deleted += 1


class FakeAgent:
    source = "# test double; never executed"
    input_tokens = output_tokens = 0

    def __init__(self, settings, cancel):
        pass

    def run(self, sandbox, validate):
        result = oracle(sandbox.computer.files)
        validate(result)
        return result

    def cost(self):
        return 0


@pytest.mark.parametrize(
    "scenario,outcome",
    [
        ("normal", "succeeded"),
        ("resume", "succeeded"),
        ("failure", "failed"),
        ("interrupt", "interrupted"),
        ("containment", "succeeded"),
    ],
)
def test_all_paths_delete(tmp_path, ledger, scenario, outcome):
    computer = FakeComputer()
    row = dict(
        run_id=scenario,
        sandbox_name="sbx-test123456",
        problem_id="csv-reconciliation",
        state="created",
        execution_target="celesto_sandbox",
    )
    ledger.insert(row)
    runner = JobRunner(Settings(), ledger, tmp_path, lambda settings, name: computer, FakeAgent)
    details = runner.run(row, 1, scenario, threading.Event())
    assert details["outcome"] == outcome
    assert details["cleanup_confirmed"] is True
    assert computer.deleted == 1
    assert ledger.get(scenario)["state"] == "deleted"
    if scenario == "resume":
        assert computer.stops == computer.resumes == 1
        trace = [
            json.loads(line) for line in (tmp_path / "reports/failure_trace.jsonl").read_text().splitlines()
        ]
        assert any(e["event"] == "policy_applied" and e["policy"] == "resume" for e in trace)
        assert [e["state"] for e in trace if "state" in e] == ["MidTaskFailure", "Resumed"]


def test_errors_do_not_leak_configured_credentials():
    settings = Settings(celesto_key="secret-celesto", llm_key="secret-llm", access_token="secret-local")
    message = safe_error(
        RuntimeError("secret-celesto secret-llm secret-local https://private.invalid?key=x Bearer token"),
        settings,
    )
    assert "secret-" not in message and "private.invalid" not in message and "Bearer token" not in message


def test_inspection_cannot_forward_csv_cells_or_unknown_columns():
    metadata = inspection_metadata(
        json.dumps(
            {
                "sales.csv": {
                    "columns": ["order_id", "amount", "private-value"],
                    "row_count": 2,
                    "rows": ["private-csv-cell"],
                },
                "payments.csv": {"columns": ["payment_id"], "row_count": 3},
                "extra": "private-other-data",
            }
        )
    )
    assert "private" not in metadata
    assert json.loads(metadata)["sales.csv"]["columns"] == ["amount", "order_id"]


def test_cleanup_outage_is_not_reported_as_deleted(tmp_path, ledger, monkeypatch):
    monkeypatch.setattr("app.sandbox.time.sleep", lambda value: None)
    row = dict(
        run_id="cleanup-fail",
        sandbox_name="sbx-test123456",
        problem_id="test",
        state="created",
        execution_target="celesto_sandbox",
    )
    ledger.insert(row)
    runner = JobRunner(
        Settings(), ledger, tmp_path, lambda settings, name: FakeComputer(fail_delete=True), FakeAgent
    )
    details = runner.run(row, 1, "normal", threading.Event())
    assert details["success"] is False
    assert details["cleanup_confirmed"] is False
    assert details["manual_cleanup_required"] == row["sandbox_name"]
    assert ledger.get(row["run_id"])["state"] == "failed"


def test_blocked_command_is_audited_and_never_reaches_sdk(tmp_path):
    computer = FakeComputer()
    sandbox = Sandbox(Settings(), "sbx-block12345", tmp_path, threading.Event(), lambda s, n: computer)
    sandbox.create()
    count = len(computer.commands)
    with pytest.raises(CommandBlockedError):
        sandbox.request("rm -rf /workspace")
    assert len(computer.commands) == count
    last = json.loads((tmp_path / "logs/commands.jsonl").read_text().splitlines()[-1])
    assert last["decision"] == "block"
    assert last["execution_target"] == "celesto_sandbox"
    sandbox.cleanup()


def test_boot_failure_still_deletes_handle(tmp_path, ledger):
    class BrokenComputer(FakeComputer):
        def run(self, command, timeout):
            raise TimeoutError("boot probe uncertain")

    computer = BrokenComputer()
    row = dict(
        run_id="boot-fail",
        sandbox_name="sbx-test123456",
        problem_id="test",
        state="created",
        execution_target="celesto_sandbox",
    )
    ledger.insert(row)
    details = JobRunner(Settings(), ledger, tmp_path, lambda s, n: computer, FakeAgent).run(
        row, 1, "normal", threading.Event()
    )
    assert details["success"] is False
    assert computer.deleted == 1


def test_unknown_allocation_cannot_be_marked_deleted(tmp_path, ledger, monkeypatch):
    class UnknownAllocation(FakeComputer):
        id = None

        def start(self):
            raise TimeoutError("create response lost")

    computer = UnknownAllocation()
    row = dict(
        run_id="unknown-allocation",
        sandbox_name="sbx-test123456",
        problem_id="test",
        state="created",
        execution_target="celesto_sandbox",
    )
    ledger.insert(row)
    details = JobRunner(Settings(), ledger, tmp_path, lambda s, n: computer, FakeAgent).run(
        row, 1, "normal", threading.Event()
    )
    assert details["fresh"] is False
    assert details["allocation_confirmed"] is False
    assert details["cleanup_confirmed"] is False
    assert details["manual_cleanup_required"] == row["sandbox_name"]
    assert ledger.get(row["run_id"])["state"] == "failed"


def test_explicit_credit_rejection_is_not_an_orphan(tmp_path, ledger):
    class CreditRejection(RuntimeError):
        operation = "create"
        side_effects = "none"
        status_code = 402

    class RejectedComputer(FakeComputer):
        id = None

        def start(self):
            raise CreditRejection("Start the free trial")

    row = dict(
        run_id="credit-rejected",
        sandbox_name="sbx-test123456",
        problem_id="test",
        state="created",
        execution_target="celesto_sandbox",
    )
    ledger.insert(row)
    details = JobRunner(Settings(), ledger, tmp_path, lambda s, n: RejectedComputer(), FakeAgent).run(
        row, 1, "normal", threading.Event()
    )
    assert details["allocation_rejected"] is True
    assert details["cleanup_not_required"] is True
    assert details["cleanup_confirmed"] is False  # No deletion of a real guest occurred.
    assert "manual_cleanup_required" not in details
    assert details["sandbox_cost_usd_estimate"] == 0


def test_containment_evidence_survives_a_later_model_failure(tmp_path, ledger):
    class FailingAgent(FakeAgent):
        def run(self, sandbox, validate):
            raise RuntimeError("Model unavailable after the safety probe")

    row = dict(
        run_id="probe-then-fail",
        sandbox_name="sbx-test123456",
        problem_id="test",
        state="created",
        execution_target="celesto_sandbox",
    )
    ledger.insert(row)
    details = JobRunner(Settings(), ledger, tmp_path, lambda s, n: FakeComputer(), FailingAgent).run(
        row, 1, "containment", threading.Event()
    )
    assert details["success"] is False
    assert details["containment"]["blocked"] is True
    assert details["containment"]["host_untouched"] is True
    assert details["cleanup_confirmed"] is True
