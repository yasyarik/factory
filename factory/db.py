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
              product_mode INTEGER NOT NULL DEFAULT 0,
              engagement_mode INTEGER NOT NULL DEFAULT 0,
              lead_magnet_mode INTEGER NOT NULL DEFAULT 0,
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
        if "product_mode" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN product_mode INTEGER NOT NULL DEFAULT 0;")
        if "engagement_mode" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN engagement_mode INTEGER NOT NULL DEFAULT 0;")
        if "lead_magnet_mode" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN lead_magnet_mode INTEGER NOT NULL DEFAULT 0;")

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

        if 'tumblr_status' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN tumblr_status TEXT;")
        if 'tumblr_post_url' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN tumblr_post_url TEXT;")
        if 'tumblr_posted_at' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN tumblr_posted_at TEXT;")
        if 'tumblr_error' not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN tumblr_error TEXT;")

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
            """
            CREATE TABLE IF NOT EXISTS tumblr_auth (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              oauth_token TEXT,
              oauth_token_secret TEXT,
              blog_hostname TEXT
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tumblr_oauth_temp (
              oauth_token TEXT PRIMARY KEY,
              oauth_token_secret TEXT NOT NULL,
              state TEXT,
              created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS oauth_states_provider_created_idx ON oauth_states(provider, created_at);"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS autopublish_settings (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              enabled INTEGER NOT NULL DEFAULT 0,
              times_per_day INTEGER NOT NULL DEFAULT 3,
              channels_json TEXT NOT NULL DEFAULT '["linkedin","telegram","twitter","tumblr"]',
              timezone TEXT NOT NULL DEFAULT 'UTC',
              start_hour INTEGER NOT NULL DEFAULT 9,
              end_hour INTEGER NOT NULL DEFAULT 21,
              linkedin_include_link INTEGER NOT NULL DEFAULT 0,
              telegram_include_link INTEGER NOT NULL DEFAULT 0,
              tumblr_include_link INTEGER NOT NULL DEFAULT 0,
              last_slot_key TEXT,
              last_run_at TEXT,
              updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO autopublish_settings (id, enabled, times_per_day, channels_json, timezone, start_hour, end_hour, updated_at)
            SELECT 1, 0, 3, '["linkedin","telegram","twitter","tumblr"]', 'UTC', 9, 21, ?
            WHERE NOT EXISTS (SELECT 1 FROM autopublish_settings WHERE id = 1);
            """,
            (utcnow_iso(),),
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS autopublish_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              trigger TEXT NOT NULL,
              job_id TEXT,
              status TEXT NOT NULL,
              result_json TEXT
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS autopublish_runs_started_idx ON autopublish_runs(started_at);"
        )

        ap_cols = [r[1] for r in conn.execute("PRAGMA table_info(autopublish_settings);").fetchall()]
        if "linkedin_include_link" not in ap_cols:
            conn.execute("ALTER TABLE autopublish_settings ADD COLUMN linkedin_include_link INTEGER NOT NULL DEFAULT 0;")
        if "telegram_include_link" not in ap_cols:
            conn.execute("ALTER TABLE autopublish_settings ADD COLUMN telegram_include_link INTEGER NOT NULL DEFAULT 0;")
        if "tumblr_include_link" not in ap_cols:
            conn.execute("ALTER TABLE autopublish_settings ADD COLUMN tumblr_include_link INTEGER NOT NULL DEFAULT 0;")


        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS social_posts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              channel TEXT NOT NULL,
              content_text TEXT,
              content_json TEXT,
              remote_url TEXT,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS social_posts_job_channel_idx ON social_posts(job_id, channel, created_at);"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_discovery_settings (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              enabled INTEGER NOT NULL DEFAULT 0,
              timezone TEXT NOT NULL DEFAULT 'UTC',
              run_hour INTEGER NOT NULL DEFAULT 6,
              direction TEXT,
              category_hint TEXT,
              per_run_limit INTEGER NOT NULL DEFAULT 15,
              min_score REAL NOT NULL DEFAULT 55.0,
              top_n INTEGER NOT NULL DEFAULT 3,
              last_run_key TEXT,
              last_run_at TEXT,
              updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO topic_discovery_settings (id, enabled, timezone, run_hour, per_run_limit, min_score, top_n, updated_at)
            SELECT 1, 0, 'UTC', 6, 15, 55.0, 3, ?
            WHERE NOT EXISTS (SELECT 1 FROM topic_discovery_settings WHERE id = 1);
            """,
            (utcnow_iso(),),
        )

        td_cols = [r[1] for r in conn.execute("PRAGMA table_info(topic_discovery_settings);").fetchall()]
        if "enabled" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0;")
        if "timezone" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC';")
        if "run_hour" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN run_hour INTEGER NOT NULL DEFAULT 6;")
        if "direction" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN direction TEXT;")
        if "category_hint" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN category_hint TEXT;")
        if "per_run_limit" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN per_run_limit INTEGER NOT NULL DEFAULT 15;")
        if "min_score" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN min_score REAL NOT NULL DEFAULT 55.0;")
        if "top_n" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN top_n INTEGER NOT NULL DEFAULT 3;")
        if "product_mode" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN product_mode INTEGER NOT NULL DEFAULT 0;")
        if "engagement_mode" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN engagement_mode INTEGER NOT NULL DEFAULT 0;")
        if "lead_magnet_mode" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN lead_magnet_mode INTEGER NOT NULL DEFAULT 0;")
        if "last_run_key" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN last_run_key TEXT;")
        if "last_run_at" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN last_run_at TEXT;")
        if "updated_at" not in td_cols:
            conn.execute("ALTER TABLE topic_discovery_settings ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_discovery_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              trigger TEXT NOT NULL,
              direction TEXT,
              status TEXT NOT NULL,
              found_count INTEGER NOT NULL DEFAULT 0,
              queued_count INTEGER NOT NULL DEFAULT 0,
              result_json TEXT
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS topic_discovery_runs_started_idx ON topic_discovery_runs(started_at);"
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
