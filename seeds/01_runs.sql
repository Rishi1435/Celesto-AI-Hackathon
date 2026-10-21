CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    sandbox_name TEXT NOT NULL UNIQUE CHECK (sandbox_name ~ '^sbx-[a-z0-9]{8,}$'),
    problem_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('created','ready','running','succeeded','failed','deleted')),
    execution_target TEXT NOT NULL CHECK (execution_target = 'celesto_sandbox'),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO runs (run_id, sandbox_name, problem_id, state, execution_target)
VALUES ('seed-demo-1', 'sbx-seeddemo1', 'csv-reconciliation', 'deleted', 'celesto_sandbox')
ON CONFLICT DO NOTHING;
