import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.config import ROOT, Settings
from app.ledger import Ledger
from app.runner import JobRunner


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str = Field(min_length=1, max_length=100)
    sandbox_name: str = Field(pattern=r"^sbx-[a-z0-9]{8,}$", max_length=80)
    problem_id: str = Field(min_length=1, max_length=100)
    state: Literal["created", "ready", "running", "succeeded", "failed", "deleted"]
    execution_target: Literal["celesto_sandbox"]


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    seed: int = Field(default=1, ge=0, le=2**31 - 1)
    scenario: Literal["normal", "resume", "failure", "interrupt", "containment"] = "normal"


def create_app(settings=None, ledger=None, runner=None, initialize=True):
    settings = settings or Settings.from_env()
    ledger = ledger or Ledger(settings.database_url)
    runner = runner or JobRunner(settings, ledger, ROOT)
    controls = {}
    lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=max(1, settings.max_concurrent), thread_name_prefix="celesto-job")

    @asynccontextmanager
    async def lifespan(app):
        if initialize:
            ledger.initialize()
        yield
        with lock:
            for control in controls.values():
                control.set()
        # An in-flight command is bounded by COMMAND_TIMEOUT_S, then finally deletes the guest.
        pool.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(title="Celesto Reconcile — run ledger", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse(status_code=400, content={"detail": "Invalid request fields or values"})

    def authorize(authorization: str | None = Header(default=None)):
        if settings.access_token and settings.access_token.startswith("replace-"):
            raise HTTPException(503, "Configure API_ACCESS_TOKEN before starting jobs")
        if settings.access_token and not secrets.compare_digest(
            authorization or "", "Bearer " + settings.access_token
        ):
            raise HTTPException(401, "Invalid bearer token")

    @app.get("/health")
    def health():
        try:
            ledger.healthy()
        except psycopg.Error:
            raise HTTPException(503, "Ledger unavailable")
        return {"status": "ok"}

    @app.get("/api/runs")
    def list_runs():
        return ledger.list()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        row = ledger.get(run_id)
        if row is None:
            raise HTTPException(404, "Run not found")
        return row

    @app.post("/api/runs", status_code=201)
    def post_run(body: RunInput):
        try:
            return ledger.insert(body.model_dump())
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "Run id or sandbox name already exists")

    @app.post("/api/jobs", status_code=202, dependencies=[Depends(authorize)])
    def post_job(body: JobInput):
        try:
            settings.validate_agent()
        except ValueError as error:
            raise HTTPException(503, str(error))
        with lock:
            if len(controls) >= settings.max_concurrent:
                raise HTTPException(429, "All job slots busy; no sandbox was created")
            run_id = uuid.uuid4().hex
            row = dict(
                run_id=run_id,
                sandbox_name="sbx-" + uuid.uuid4().hex,
                problem_id="csv-reconciliation",
                state="created",
                execution_target="celesto_sandbox",
            )
            ledger.insert(row)
            control = threading.Event()
            controls[run_id] = control

            def work():
                try:
                    runner.run(row, body.seed, body.scenario, control)
                finally:
                    with lock:
                        controls.pop(run_id, None)

            pool.submit(work)
        return row

    @app.post("/api/jobs/{run_id}/cancel", status_code=202, dependencies=[Depends(authorize)])
    def cancel_job(run_id: str):
        with lock:
            control = controls.get(run_id)
            if control is None:
                raise HTTPException(404, "No active job with this id")
            control.set()
        return {"run_id": run_id, "cancellation_requested": True}

    app.mount("/reports", StaticFiles(directory=ROOT / "reports"), name="reports")
    return app


app = create_app()
