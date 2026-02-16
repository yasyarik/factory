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
              visibility TEXT NOT NULL DEFAULT 'public',
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
            conn.execute("ALTER TABLE jobs ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public';")

        if 'linkedin_status' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN linkedin_status TEXT;")
        if 'linkedin_post_url' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN linkedin_post_url TEXT;")
        if 'linkedin_posted_at' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN linkedin_posted_at TEXT;")
        if 'linkedin_error' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN linkedin_error TEXT;")

        if 'telegram_status' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN telegram_status TEXT;")
        if 'telegram_post_url' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN telegram_post_url TEXT;")
        if 'telegram_posted_at' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN telegram_posted_at TEXT;")
        if 'telegram_error' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN telegram_error TEXT;")

        if 'twitter_status' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN twitter_status TEXT;")
        if 'twitter_post_url' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN twitter_post_url TEXT;")
        if 'twitter_posted_at' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN twitter_posted_at TEXT;")
        if 'twitter_error' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN twitter_error TEXT;")

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

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS linkedin_auth (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              access_token TEXT,
              refresh_token TEXT,
              expires_at TEXT,
              member_urn TEXT,
              org_urn TEXT
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_states (
              provider TEXT NOT NULL,
              state TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS oauth_states_provider_created_idx ON oauth_states(provider, created_at);"
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
