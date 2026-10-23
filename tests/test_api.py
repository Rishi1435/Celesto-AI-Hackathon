import threading

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings


@pytest.fixture
def client(ledger):
    with TestClient(create_app(settings=Settings(), ledger=ledger)) as test_client:
        yield test_client


def test_health_and_seed(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health").status_code == 200
    assert any(r["sandbox_name"] == "sbx-seeddemo1" for r in client.get("/api/runs").json())


def test_run_roundtrip(client):
    row = dict(
        run_id="run-1",
        sandbox_name="sbx-test12345",
        problem_id="reconciliation",
        state="created",
        execution_target="celesto_sandbox",
    )
    response = client.post("/api/runs", json=row)
    assert response.status_code == 201
    assert all(response.json()[key] == value for key, value in row.items())
    assert client.get("/api/runs/run-1").json()["sandbox_name"] == row["sandbox_name"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_target", "host"),
        ("state", "resumed"),
        ("sandbox_name", "sbx-short"),
        ("sandbox_name", "sbx-UPPERCASE"),
        ("run_id", ""),
        ("extra", "x"),
    ],
)
def test_invalid_row_is_400(client, field, value):
    row = dict(
        run_id="run-1",
        sandbox_name="sbx-test12345",
        problem_id="reconciliation",
        state="created",
        execution_target="celesto_sandbox",
    )
    row[field] = value
    assert client.post("/api/runs", json=row).status_code == 400


def test_missing_keys_dont_break_health(client):
    assert client.post("/api/jobs", json={}).status_code == 503
    assert client.get("/health").status_code == 200


def test_auth_and_admission_limit(ledger):
    entered, finished = threading.Event(), threading.Event()

    class WaitingRunner:
        def run(self, row, seed, scenario, cancel):
            entered.set()
            cancel.wait(timeout=5)
            finished.set()

    settings = Settings(
        celesto_key="test-key",
        llm_key="test-key",
        llm_model="test-model",
        llm_base_url="http://test-model/v1",
        access_token="test-token",
    )
    with TestClient(create_app(settings, ledger, WaitingRunner())) as client:
        assert client.post("/api/jobs", json={}).status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        result = client.post("/api/jobs", json={}, headers=headers)
        assert result.status_code == 202
        assert entered.wait(timeout=2)
        assert client.post("/api/jobs", json={}, headers=headers).status_code == 429
        run_id = result.json()["run_id"]
        assert client.post(f"/api/jobs/{run_id}/cancel", headers=headers).status_code == 202
        assert finished.wait(timeout=2)


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/unknown").status_code == 404
