import base64
import hashlib
import hmac
import html
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

import requests


REQUEST_TOKEN_URL = "https://www.tumblr.com/oauth/request_token"
AUTHORIZE_URL = "https://www.tumblr.com/oauth/authorize"
ACCESS_TOKEN_URL = "https://www.tumblr.com/oauth/access_token"
API_BASE = "https://api.tumblr.com/v2"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _pct(s: str) -> str:
    return quote(str(s), safe="~-._")


def _oauth1_header(
    *,
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    token: str = "",
    token_secret: str = "",
    callback: str | None = None,
    verifier: str | None = None,
) -> str:
    params: dict[str, str] = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": secrets.token_hex(12),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    if token:
        params["oauth_token"] = token
    if callback is not None:
        params["oauth_callback"] = callback
    if verifier is not None:
        params["oauth_verifier"] = verifier

    split = urlsplit(url)
    base_url = f"{split.scheme}://{split.netloc}{split.path}"

    sign_items: list[tuple[str, str]] = []
    for k, v in parse_qsl(split.query, keep_blank_values=True):
        sign_items.append((k, v))
    for k, v in params.items():
        sign_items.append((k, v))

    sign_items.sort(key=lambda x: (_pct(x[0]), _pct(x[1])))
    normalized = "&".join([f"{_pct(k)}={_pct(v)}" for k, v in sign_items])

    base_string = "&".join([_pct(method.upper()), _pct(base_url), _pct(normalized)])
    signing_key = f"{_pct(consumer_secret)}&{_pct(token_secret)}"
    digest = hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    signature = base64.b64encode(digest).decode("ascii")

    params["oauth_signature"] = signature
    return "OAuth " + ", ".join([f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(params.items())])


def tumblr_request_token(*, consumer_key: str, consumer_secret: str, callback_url: str) -> dict[str, str]:
    auth = _oauth1_header(
        method="POST",
        url=REQUEST_TOKEN_URL,
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        callback=callback_url,
    )
    r = requests.post(REQUEST_TOKEN_URL, headers={"Authorization": auth}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Tumblr request_token failed: {r.status_code} {r.text}")
    data = dict(parse_qsl((r.text or "").strip(), keep_blank_values=True))
    return {
        "oauth_token": (data.get("oauth_token") or "").strip(),
        "oauth_token_secret": (data.get("oauth_token_secret") or "").strip(),
    }


def tumblr_build_auth_url(oauth_token: str) -> str:
    return f"{AUTHORIZE_URL}?oauth_token={_pct(oauth_token)}"


def tumblr_exchange_access_token(*, consumer_key: str, consumer_secret: str, request_token: str, request_token_secret: str, oauth_verifier: str) -> dict[str, str]:
    auth = _oauth1_header(
        method="POST",
        url=ACCESS_TOKEN_URL,
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        token=request_token,
        token_secret=request_token_secret,
        verifier=oauth_verifier,
    )
    r = requests.post(ACCESS_TOKEN_URL, headers={"Authorization": auth}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Tumblr access_token failed: {r.status_code} {r.text}")
    data = dict(parse_qsl((r.text or "").strip(), keep_blank_values=True))
    return {
        "oauth_token": (data.get("oauth_token") or "").strip(),
        "oauth_token_secret": (data.get("oauth_token_secret") or "").strip(),
        "blog_hostname": (data.get("blog_hostname") or "").strip(),
    }


def db_get_tumblr(path: str) -> dict[str, Any] | None:
    with sqlite3.connect(path) as conn:
        r = conn.execute("SELECT oauth_token, oauth_token_secret, blog_hostname FROM tumblr_auth WHERE id=1").fetchone()
    if not r:
        return None
    return {
        "oauth_token": r[0],
        "oauth_token_secret": r[1],
        "blog_hostname": r[2],
    }


def db_set_tumblr(path: str, *, oauth_token: str, oauth_token_secret: str, blog_hostname: str | None) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO tumblr_auth (id, oauth_token, oauth_token_secret, blog_hostname)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              oauth_token=excluded.oauth_token,
              oauth_token_secret=excluded.oauth_token_secret,
              blog_hostname=excluded.blog_hostname
            """,
            (oauth_token, oauth_token_secret, blog_hostname),
        )
        conn.commit()


def db_clear_tumblr(path: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM tumblr_auth WHERE id=1")
        conn.commit()


def db_put_tumblr_temp(path: str, *, oauth_token: str, oauth_token_secret: str, state: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM tumblr_oauth_temp WHERE created_at < ?", (_utcnow_iso(),))
        conn.execute(
            """
            INSERT INTO tumblr_oauth_temp (oauth_token, oauth_token_secret, state, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(oauth_token) DO UPDATE SET
              oauth_token_secret=excluded.oauth_token_secret,
              state=excluded.state,
              created_at=excluded.created_at
            """,
            (oauth_token, oauth_token_secret, state, _utcnow_iso()),
        )
        conn.commit()


def db_pop_tumblr_temp(path: str, *, oauth_token: str) -> dict[str, Any] | None:
    with sqlite3.connect(path) as conn:
        r = conn.execute(
            "SELECT oauth_token_secret, state, created_at FROM tumblr_oauth_temp WHERE oauth_token=?",
            (oauth_token,),
        ).fetchone()
        conn.execute("DELETE FROM tumblr_oauth_temp WHERE oauth_token=?", (oauth_token,))
        conn.commit()
    if not r:
        return None
    return {
        "oauth_token_secret": r[0],
        "state": r[1],
        "created_at": r[2],
    }


def _strip_html_to_text(html_text: str) -> str:
    import re

    s = (html_text or "")
    s = re.sub(r"(?is)<script.*?</script>", " ", s)
    s = re.sub(r"(?is)<style.*?</style>", " ", s)
    s = re.sub(r"(?is)<br\\s*/?>", "\n", s)
    s = re.sub(r"(?is)</p>", "\n\n", s)
    s = re.sub(r"(?is)</h[1-6]>", "\n\n", s)
    s = re.sub(r"(?is)<li[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def _truncate(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    cut = t[:max_len]
    sp = cut.rfind(" ")
    if sp > int(max_len * 0.75):
        cut = cut[:sp]
    return cut.rstrip(" ,;:-")


def build_tumblr_post_html(*, title: str, description: str, content_html: str, url: str, include_link: bool = True) -> str:
    body = _strip_html_to_text(content_html)
    summary = _truncate(body, 1800)
    desc = (description or "").strip()

    out = []
    out.append(f"<p><strong>{html.escape(title or '')}</strong></p>")
    if desc:
        out.append(f"<p>{html.escape(desc)}</p>")
    if summary:
        for chunk in summary.split("\n\n"):
            chunk = chunk.strip()
            if chunk:
                out.append(f"<p>{html.escape(chunk)}</p>")
    if include_link and url:
        out.append(f"<p><a href=\"{html.escape(url)}\" target=\"_blank\" rel=\"noopener noreferrer\">Read full article</a></p>")
    return "\n".join(out)


def tumblr_publish_text_post(
    *,
    consumer_key: str,
    consumer_secret: str,
    oauth_token: str,
    oauth_token_secret: str,
    blog_hostname: str,
    title: str,
    body_html: str,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    host = (blog_hostname or "").strip()
    if not host:
        raise RuntimeError("Missing blog hostname")

    url = f"{API_BASE}/blog/{host}/post"
    auth = _oauth1_header(
        method="POST",
        url=url,
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        token=oauth_token,
        token_secret=oauth_token_secret,
    )

    clean_tags = [t.strip() for t in (tags or []) if isinstance(t, str) and t.strip()]
    payload = {
        "type": "text",
        "title": (title or "").strip()[:250],
        "body": body_html or "",
        "format": "html",
    }
    if clean_tags:
        payload["tags"] = ",".join(clean_tags[:10])

    r = requests.post(url, headers={"Authorization": auth}, data=payload, timeout=45)
    data: dict[str, Any] = {}
    try:
        data = r.json() if r.content else {}
    except Exception:
        data = {"raw": (r.text or "")}

    if r.status_code >= 400:
        raise RuntimeError(f"Tumblr post failed: {r.status_code} {json.dumps(data, ensure_ascii=False)}")

    post_id = str(((data.get("response") or {}).get("id") or "")).strip()
    post_url = f"https://{host}/post/{post_id}" if post_id else f"https://{host}/"
    return {
        "post_id": post_id,
        "post_url": post_url,
        "response": data,
    }
