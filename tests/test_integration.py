import uuid

import httpx
import pytest


@pytest.mark.integration
def test_live_postgres_api(request):
    if not request.config.getoption("--live-api"):
        pytest.skip("Run with --live-api after docker compose up")
    with httpx.Client(base_url=request.config.getoption("--api-base-url"), timeout=10) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert any(r["sandbox_name"] == "sbx-seeddemo1" for r in client.get("/api/runs").json())
        row = dict(
            run_id="integration-" + uuid.uuid4().hex,
            sandbox_name="sbx-" + uuid.uuid4().hex,
            problem_id="api-contract-check",
            state="deleted",
            execution_target="celesto_sandbox",
        )
        response = client.post("/api/runs", json=row)
        assert response.status_code == 201
        assert all(response.json()[key] == value for key, value in row.items())
        for key, value in [("execution_target", "host"), ("state", "bad"), ("sandbox_name", "bad")]:
            assert client.post("/api/runs", json=dict(row, **{key: value})).status_code == 400
        assert client.post("/api/runs", json=row).status_code == 409
