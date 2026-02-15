import json
import os
import re
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import requests


LINKEDIN_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_API = "https://api.linkedin.com"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _parse_iso_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _strip_html_to_text(html: str) -> str:
    s = (html or "")
    s = re.sub(r"(?is)<script.*?</script>", " ", s)
    s = re.sub(r"(?is)<style.*?</style>", " ", s)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</p>", "\n\n", s)
    s = re.sub(r"(?is)</h[23]>", "\n\n", s)
    s = re.sub(r"(?is)<li[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _truncate_to(s: str, max_len: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    # Prefer word boundary.
    sp = cut.rfind(" ")
    if sp >= int(max_len * 0.75):
        cut = cut[:sp]
    return cut.rstrip(" ,;:-")


def linkedin_scopes(mode: str = 'member') -> str:
    # Keep scopes minimal so OAuth succeeds with default app permissions.
    # Add org scope only when explicitly requested.
    scopes = [
        'r_liteprofile',
                'w_member_social',
    ]
    if (mode or '').lower().strip() == 'org':
        scopes.append('w_organization_social')
    return ' '.join(scopes)


def linkedin_build_auth_url(*, client_id: str, redirect_uri: str, state: str, mode: str = 'member') -> str:
    q = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': linkedin_scopes(mode),
        'state': state,
    }
    return LINKEDIN_AUTH_URL + '?' + urllib.parse.urlencode(q)


def linkedin_exchange_code(*, code: str, redirect_uri: str, client_id: str, client_secret: str) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    r = requests.post(LINKEDIN_TOKEN_URL, data=data, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"LinkedIn token exchange failed: {r.status_code} {r.text}")
    return r.json()


def linkedin_refresh_token(*, refresh_token: str, client_id: str, client_secret: str) -> dict[str, Any]:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    r = requests.post(LINKEDIN_TOKEN_URL, data=data, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"LinkedIn token refresh failed: {r.status_code} {r.text}")
    return r.json()


def linkedin_get_member_id(*, access_token: str) -> str:
    # Returns the LinkedIn member id, used to build urn:li:person:<id>
    r = requests.get(
        LINKEDIN_API + "/v2/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"LinkedIn /v2/me failed: {r.status_code} {r.text}")
    data = r.json()
    mid = (data.get("id") or "").strip()
    if not mid:
        raise RuntimeError(f"LinkedIn /v2/me did not return id: {data}")
    return mid


def linkedin_register_image_upload(*, access_token: str, owner_urn: str) -> tuple[str, str]:
    payload = {
        "registerUploadRequest": {
            "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
            "owner": owner_urn,
            "serviceRelationships": [
                {"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}
            ],
        }
    }

    r = requests.post(
        LINKEDIN_API + "/v2/assets?action=registerUpload",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload),
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"LinkedIn registerUpload failed: {r.status_code} {r.text}")

    data = r.json()
    value = data.get("value") or {}
    asset = (value.get("asset") or "").strip()

    upload_url = ""
    mech = (value.get("uploadMechanism") or {}).get("com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest") or {}
    if isinstance(mech, dict):
        upload_url = (mech.get("uploadUrl") or "").strip()

    if not asset or not upload_url:
        raise RuntimeError(f"LinkedIn registerUpload missing asset/uploadUrl: {data}")
    return asset, upload_url


def linkedin_upload_image(*, upload_url: str, image_bytes: bytes, mime: str) -> None:
    r = requests.put(
        upload_url,
        headers={"Content-Type": mime},
        data=image_bytes,
        timeout=120,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"LinkedIn image upload failed: {r.status_code} {r.text}")


def linkedin_create_image_post(*, access_token: str, author_urn: str, text: str, asset_urn: str, title: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "author": author_urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "IMAGE",
                "media": [
                    {
                        "status": "READY",
                        "media": asset_urn,
                        "title": {"text": title[:200]},
                    }
                ],
            }
        },
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }

    r = requests.post(
        LINKEDIN_API + "/v2/ugcPosts",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        },
        data=json.dumps(payload),
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"LinkedIn ugcPosts failed: {r.status_code} {r.text}")
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}


def _guess_mime_from_filename(name: str) -> str:
    n = (name or "").lower().strip()
    if n.endswith(".png"):
        return "image/png"
    if n.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def build_linkedin_summary(*, title: str, description: str, content_html: str, url: str) -> str:
    """Create a LinkedIn post <= 3000 chars. Uses Gemini if configured; else deterministic fallback."""

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    # Keep the input small and high-signal.
    body = _strip_html_to_text(content_html)
    body = _truncate_to(body, 2500)

    # We'll reserve space for the link and a couple newlines.
    MAX = 3000
    RESERVE = min(MAX, len(url) + 10)
    TARGET = MAX - RESERVE

    if api_key:
        sys = (
            "You are a senior B2B growth writer. "
            "Write a LinkedIn post that promotes a blog article. "
            "No markdown. No emojis. Plain text only. "
            "Structure: hook (1 line), value bullets (3-6 short bullets), "
            "one concise CTA line. "
            "Add 3-6 relevant hashtags at the end. "
            f"Hard limit: {TARGET} characters for the post text (excluding the URL we will append)."
        )
        user = (
            f"TITLE: {title}\n"
            f"META DESCRIPTION: {description}\n"
            f"ARTICLE EXCERPT (clean text): {body}\n"
            "Return ONLY the post text."
        )

        # Use the same REST pattern as generate.py (no grounding needed).
        gen_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": sys + "\n\n" + user}]}],
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": 800},
        }
        r = requests.post(gen_url, json=payload, timeout=60)
        if r.status_code < 400:
            data = r.json()
            try:
                text = data["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                text = ""
            text = (text or "").strip()
            if text:
                text = _truncate_to(text, TARGET)
                return text + "\n\n" + url

    # Deterministic fallback.
    base = _truncate_to((description or title or "").strip(), 180)
    post = (
        f"{title}\n\n"
        f"{base}\n\n"
        "Key takeaways:\n"
        "- What changed in 2026\n"
        "- What to measure and test\n"
        "- A simple execution checklist\n\n"
        "Read the full guide:\n"
    )
    post = _truncate_to(post, TARGET)
    return post + "\n" + url


# --- SQLite persistence (single-tenant) ---

def db_init_linkedin(path: str) -> None:
    with sqlite3.connect(path) as conn:
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


def db_get_linkedin(path: str) -> dict[str, Any] | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT access_token, refresh_token, expires_at, member_urn, org_urn FROM linkedin_auth WHERE id=1"
        ).fetchone()

    if not row:
        return None

    return {
        "access_token": row[0],
        "refresh_token": row[1],
        "expires_at": row[2],
        "member_urn": row[3],
        "org_urn": row[4],
    }


def db_set_linkedin(
    path: str,
    *,
    access_token: str | None,
    refresh_token: str | None,
    expires_at: str | None,
    member_urn: str | None,
    org_urn: str | None,
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO linkedin_auth (id, access_token, refresh_token, expires_at, member_urn, org_urn) VALUES (1, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET access_token=excluded.access_token, refresh_token=excluded.refresh_token, "
            "expires_at=excluded.expires_at, member_urn=excluded.member_urn, org_urn=excluded.org_urn",
            (access_token, refresh_token, expires_at, member_urn, org_urn),
        )
        conn.commit()


def db_clear_linkedin(path: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM linkedin_auth WHERE id=1")
        conn.commit()


def db_create_state(path: str, *, provider: str, state: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO oauth_states (provider, state, created_at) VALUES (?, ?, ?)",
            (provider, state, _utcnow_iso()),
        )
        conn.commit()


def db_consume_state(path: str, *, provider: str, state: str, max_age_min: int = 15) -> bool:
    now = _utcnow()
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT rowid, created_at FROM oauth_states WHERE provider=? AND state=? ORDER BY created_at DESC LIMIT 1",
            (provider, state),
        ).fetchall()

        if not rows:
            return False

        rowid, created_at = rows[0]
        ts = _parse_iso_dt(created_at)
        ok = bool(ts and ts >= (now - timedelta(minutes=max_age_min)))

        # Consume regardless to avoid replay.
        conn.execute("DELETE FROM oauth_states WHERE rowid=?", (rowid,))
        conn.commit()

    return ok


def linkedin_load_or_refresh_token(*, db_path: str, client_id: str, client_secret: str) -> dict[str, Any]:
    auth = db_get_linkedin(db_path) or {}
    access_token = (auth.get("access_token") or "").strip()
    refresh_token = (auth.get("refresh_token") or "").strip()
    expires_at = _parse_iso_dt(auth.get("expires_at"))

    # If token exists and not expiring soon, keep.
    if access_token and expires_at and expires_at > (_utcnow() + timedelta(minutes=2)):
        return auth

    if refresh_token:
        data = linkedin_refresh_token(refresh_token=refresh_token, client_id=client_id, client_secret=client_secret)
        new_access = data.get("access_token")
        new_refresh = data.get("refresh_token") or refresh_token
        exp = int(data.get("expires_in") or 0)
        new_expires_at = (_utcnow() + timedelta(seconds=exp)).isoformat() if exp else None

        auth["access_token"] = new_access
        auth["refresh_token"] = new_refresh
        auth["expires_at"] = new_expires_at

        db_set_linkedin(
            db_path,
            access_token=new_access,
            refresh_token=new_refresh,
            expires_at=new_expires_at,
            member_urn=auth.get("member_urn"),
            org_urn=auth.get("org_urn"),
        )
        return auth

    return auth


def post_job_to_linkedin(
    *,
    db_path: str,
    client_id: str,
    client_secret: str,
    author_mode: str,
    member_urn: str,
    org_urn: str | None,
    title: str,
    description: str,
    content_html: str,
    url: str,
    hero_abs_path: str,
    hero_filename: str,
) -> dict[str, Any]:
    auth = linkedin_load_or_refresh_token(db_path=db_path, client_id=client_id, client_secret=client_secret)
    access_token = (auth.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("LinkedIn not connected")

    if author_mode == "org":
        if not org_urn:
            raise RuntimeError("Missing LinkedIn org URN")
        author_urn = org_urn
    else:
        author_urn = member_urn

    text = build_linkedin_summary(title=title, description=description, content_html=content_html, url=url)
    if len(text) > 3000:
        text = _truncate_to(text, 3000)

    with open(hero_abs_path, "rb") as f:
        img_bytes = f.read()

    mime = _guess_mime_from_filename(hero_filename)

    asset, upload_url = linkedin_register_image_upload(access_token=access_token, owner_urn=author_urn)
    linkedin_upload_image(upload_url=upload_url, image_bytes=img_bytes, mime=mime)

    return linkedin_create_image_post(access_token=access_token, author_urn=author_urn, text=text, asset_urn=asset, title=title)
