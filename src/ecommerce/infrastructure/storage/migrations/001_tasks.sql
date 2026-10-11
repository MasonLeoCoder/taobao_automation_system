CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','cancelled','needs_attention')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tasks_started_at ON tasks(started_at DESC);
