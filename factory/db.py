import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db_init(path: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY,
              topic TEXT NOT NULL,
              slug TEXT,
              status TEXT NOT NULL,
              title TEXT,
              description TEXT,
              category TEXT,
              hero_image TEXT,
              draft_html TEXT,
              faq_json TEXT,
              error TEXT,
              sources_json TEXT,
              visibility TEXT NOT NULL DEFAULT 'hidden',
              published_url TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS jobs_status_created_idx ON jobs(status, created_at);"
        )

        # Lightweight migration for existing DBs.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs);").fetchall()]
        if "sources_json" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN sources_json TEXT;")
        if "visibility" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN visibility TEXT NOT NULL DEFAULT 'hidden';")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              ts TEXT NOT NULL,
              level TEXT NOT NULL,
              step TEXT NOT NULL,
              message TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS job_logs_job_ts_idx ON job_logs(job_id, ts);"
        )


@contextmanager
def db_connect(path: str):
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def log_event(db_path: str, job_id: str, step: str, message: str, level: str = "INFO") -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO job_logs (job_id, ts, level, step, message) VALUES (?, ?, ?, ?, ?)",
            (job_id, utcnow_iso(), level, step, message),
        )
        conn.commit()
