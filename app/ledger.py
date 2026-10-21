import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import ROOT


class Ledger:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)

    def initialize(self):
        # Idempotent on existing volumes as well as a fresh Compose initialization.
        sql = (ROOT / "seeds" / "01_runs.sql").read_text(encoding="utf-8")
        with self.connect() as connection:
            connection.execute(sql)

    def healthy(self):
        with self.connect() as connection:
            connection.execute("SELECT 1 FROM runs LIMIT 1")

    def list(self):
        with self.connect() as connection:
            return connection.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()

    def get(self, run_id):
        with self.connect() as connection:
            return connection.execute("SELECT * FROM runs WHERE run_id = %s", (run_id,)).fetchone()

    def insert(self, row):
        with self.connect() as connection:
            return connection.execute(
                "INSERT INTO runs (run_id,sandbox_name,problem_id,state,execution_target,details) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    row["run_id"],
                    row["sandbox_name"],
                    row["problem_id"],
                    row["state"],
                    row["execution_target"],
                    Jsonb(row.get("details", {})),
                ),
            ).fetchone()

    def update(self, run_id, state, details):
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET state=%s, details=%s, updated_at=now() WHERE run_id=%s",
                (state, Jsonb(details), run_id),
            )
