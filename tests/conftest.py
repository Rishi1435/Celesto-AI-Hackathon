import copy
import threading

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--require-evidence", action="store_true", help="Fail if live submission evidence is pending"
    )
    parser.addoption("--live-api", action="store_true", help="Run integration checks against localhost:8080")
    parser.addoption("--api-base-url", default="http://localhost:8080", help="URL for the live API check")


class MemoryLedger:
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()
        self.insert(
            dict(
                run_id="seed-demo-1",
                sandbox_name="sbx-seeddemo1",
                problem_id="csv-reconciliation",
                state="deleted",
                execution_target="celesto_sandbox",
            )
        )

    def initialize(self):
        pass

    def healthy(self):
        pass

    def list(self):
        with self.lock:
            return copy.deepcopy(list(self.rows.values()))

    def get(self, run_id):
        with self.lock:
            return copy.deepcopy(self.rows.get(run_id))

    def insert(self, row):
        with self.lock:
            self.rows[row["run_id"]] = dict(row, details=row.get("details", {}))
            return copy.deepcopy(self.rows[row["run_id"]])

    def update(self, run_id, state, details):
        with self.lock:
            self.rows[run_id].update(state=state, details=copy.deepcopy(details))


@pytest.fixture
def ledger():
    return MemoryLedger()
