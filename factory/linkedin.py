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

def _is_author_access_denied(err: Exception) -> bool:
    msg = str(err or '')
    if 'ugcPosts failed' not in msg and 'ACCESS_DENIED' not in msg and '403' not in msg:
        return False
    return ('/author' in msg) or ('fields [/author]' in msg)



def _tokenize_for_tags(text: str) -> list[str]:
    src = (text or '').lower()
    src = re.sub(r"[^a-z0-9а-яё\s-]", " ", src)
    src = src.replace('-', ' ')
    out: list[str] = []
    for t in src.split():
        if len(t) < 3:
            continue
        out.append(t)
    return out


def _build_context_hashtags(*, title: str, description: str, body: str, max_tags: int = 6) -> list[str]:
    text = f"{title} {description} {body}"
    tokens = _tokenize_for_tags(text)

    stop = {
        'the','and','for','with','from','that','this','your','you','our','are','but','not','how','why','what','when','into','using',
        'choose','choosing','ultimate','complete','practical','tips','guide','mastering','learn','insights',
        'about','more','than','can','will','its','their','they','have','has','had','one','two','new','best','guide','2025','2026',
        'это','как','что','для','или','при','про','без','под','над','его','ее','это','эти','этом','этих','когда','почему','чтобы',
        'есть','быть','вам','ваш','ваша','ваши','мы','они','она','оно','также','только','очень','через','после','перед','где','который'
    }

    tags: list[str] = []
    seen: set[str] = set()

    def add(tag: str):
        key = tag.lower()
        if key in seen:
            return
        clean = re.sub(r'[^A-Za-z0-9_]', '', tag)
        if not clean:
            return
        seen.add(key)
        tags.append('#' + clean)

    freq: dict[str, int] = {}
    for t in tokens:
        if t in stop:
            continue
        if t.isdigit():
            continue
        freq[t] = freq.get(t, 0) + 1

    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    for t, _ in ranked:
        if len(t) < 4:
            continue
        if len(t) > 24:
            continue
        add(t.capitalize())
        if len(tags) >= max_tags:
            break

    # Last-resort dynamic fallback from title tokens only (no static tag set).
    if not tags:
        for t in _tokenize_for_tags(title or ''):
            if len(t) < 4 or t in stop:
                continue
            add(t.capitalize())
            if len(tags) >= max_tags:
                break

    return tags[:max_tags]



def _strip_hashtag_tail(text: str) -> str:
    t = (text or '').strip()
    t = re.sub(r'(?im)^hashtags\s*:\s*.+$', '', t)
    t = re.sub(r'(?im)^хэштеги\s*:\s*.+$', '', t)
    lines = [ln for ln in t.splitlines()]
    while lines:
        ln = lines[-1].strip()
        if ln and re.fullmatch(r'(?:#[A-Za-z0-9_]+\s*){2,}', ln):
            lines.pop()
            continue
        break
    t = '\n'.join(lines)
    t = re.sub(r'\n{3,}', '\n\n', t).strip()
    return t


def _fit_with_hashtags(text: str, tags: list[str], target: int) -> str:
    tail = ' '.join(tags).strip()
    if not tail:
        return _truncate_to(text, target)
    reserve = len(tail) + 2
    core_max = max(200, target - reserve)
    core = _truncate_to(_strip_hashtag_tail(text), core_max)
    out = (core + '\n\n' + tail).strip()
    if len(out) > target:
        over = len(out) - target
        core = _truncate_to(core, max(120, len(core) - over - 2))
        out = (core + '\n\n' + tail).strip()
    return out


def _ensure_lead_title(text: str, title: str) -> str:
    t = (text or "").strip()
    ttl = (title or "").strip()
    if not ttl:
        return t

    first = ""
    for ln in t.splitlines():
        if ln.strip():
            first = ln.strip()
            break

    def _norm(v: str) -> list[str]:
        v = re.sub(r"[^a-zа-яё0-9 ]+", " ", (v or "").lower())
        return [x for x in v.split() if x]

    first_words = set(_norm(first))
    title_words = set(_norm(ttl))
    overlap = len(first_words & title_words)

    if (not first) or len(first) > 140 or overlap < 2:
        return (ttl + "\n\n" + t).strip()
    return t


def _has_section_headers(text: str, min_headers: int = 2) -> bool:
    t = _strip_hashtag_tail(text or "")
    count = 0
    for ln in t.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            continue
        if len(s) > 80:
            continue
        if s.endswith(":") and not s.startswith("-"):
            count += 1
    return count >= min_headers


def linkedin_scopes(mode: str = 'member') -> str:
    # Keep scopes minimal to avoid app-permission issues.
    # For posting as a member we need w_member_social.
    # For posting as an organization we need w_organization_social (and you must have page admin rights).
    scopes = ['w_member_social']
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


def build_linkedin_post(
    *,
    title: str,
    description: str,
    content_html: str,
    author_bio: str,
    include_link: bool,
    url: str,
) -> str:
    """Create a LinkedIn post <= 3000 chars.

    If include_link=False, generate a full self-contained post (no URL).
    """

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    body = _strip_html_to_text(content_html)
    body = _truncate_to(body, 2600)
    context_tags = _build_context_hashtags(title=title, description=description, body=body, max_tags=6)

    max_total = 3000
    suffix = ("\n\n" + url) if (include_link and url) else ""
    target = max_total - len(suffix)

    # Prompt asks for richer output, validator is more tolerant.
    prompt_min_len = 1650
    min_len = 1259

    def _normalize_text(txt: str) -> str:
        txt = (txt or "")
        txt = txt.replace("\r\n", "\n").replace("\r", "\n")
        # Keep line breaks for LinkedIn readability; normalize spaces per line.
        lines = []
        for ln in txt.split("\n"):
            line = re.sub(r"[\t ]+", " ", ln).strip()
            lines.append(line)
        txt = "\n".join(lines)
        # Collapse excessive blank lines (max one empty line between blocks).
        txt = re.sub(r"\n{3,}", "\n\n", txt)
        txt = txt.replace("My conclusion: teams win when the feedback loop is fast, measurable, and brutally practical.", "")
        txt = re.sub(r"\n{3,}", "\n\n", txt)
        return txt.strip()

    if api_key:
        sys = (
            "You are a senior B2B founder ghostwriter. "
            "Write a LinkedIn post in FIRST PERSON as the author described below. "
            "Preserve the core meaning and key factual claims from SOURCE; do not invent facts. "
            "Frame the narrative as: I learned this, tested this, and I share this because it is useful. "
            "In the voice, mention only the most relevant role(s) for this topic (1-2 max), not the full list every time. Plain text only. No markdown. No emojis. "
            "Tone: practical, credible, useful, and specific. No hype. "
            "Structure:\n"
            "- Use clear paragraph breaks and readable spacing for LinkedIn feed.\n"
            "- Add 2-4 short section headers ending with a colon (for example: What changed:, What worked:, My takeaway:).\n"
            "- Keep one empty line between sections and before hashtags.\n"
            "- 6-10 short bullets (1 line each).\n"
            "- 1 short paragraph with a clear conclusion.\n"
            "- 3-6 hashtags.\n"
            "Hard constraints:\n"
            "- Preserve all important factual points from SOURCE in your own wording.\n"
            f"- Length must be between {prompt_min_len} and {target} characters.\n"
            "- Do NOT reference the words 'blog', 'article', or 'link' unless include_link=true.\n"
            "- Return only final post text."
        )

        user = (
            f"AUTHOR BIO:\n{author_bio}\n\n"
            f"TITLE: {title}\n"
            f"META DESCRIPTION: {description}\n\n"
            f"SOURCE (clean text excerpt):\n{body}\n\n"
            f"include_link={str(bool(include_link)).lower()}\n"
            "Return ONLY the post text."
        )

        gen_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

        def _call_llm(prompt_text: str, max_tokens: int = 2200) -> str:
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
                "generationConfig": {"temperature": 0.65, "maxOutputTokens": max_tokens},
            }
            r = requests.post(gen_url, json=payload, timeout=60)
            if r.status_code >= 400:
                return ""
            data = r.json()
            try:
                t = data["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                t = ""
            return _normalize_text(t)

        last_error = "empty model response"
        for attempt in range(1, 5):
            attempt_user = user + f"\n\nATTEMPT: {attempt}/4"
            text = _call_llm(sys + "\n\n" + attempt_user)

            if not text:
                last_error = "empty model response"
                continue

            # If too short, request explicit expansion passes (up to 3).
            if len(text) < min_len:
                for expand_attempt in range(1, 4):
                    expand = (
                        f"Rewrite and expand to {min_len}-{target} chars, preserving all key facts from SOURCE. "
                        "Use first-person founder voice, clear section headers, clean paragraph breaks, and practical bullets. "
                        "Plain text only. No markdown. No emojis. No filler. Do not mention blog/article.\n\n"
                        f"AUTHOR BIO:\n{author_bio}\n\n"
                        f"SOURCE:\n{body}\n\n"
                        f"CURRENT DRAFT:\n{text}\n\n"
                        f"EXPAND ATTEMPT: {expand_attempt}/3"
                    )
                    expanded = _call_llm(expand, max_tokens=2600)
                    if expanded:
                        text = expanded
                    if len(text) >= min_len:
                        break

            text = _ensure_lead_title(text, title)
            text = _fit_with_hashtags(text, context_tags, target)
            if len(text) >= min_len and not _has_section_headers(text, min_headers=2):
                headers_fix = (
                    f"Rewrite this LinkedIn post to keep the same meaning and length {min_len}-{target} chars, "
                    "but make structure explicit with at least 3 short section headers ending with ':' on separate lines. "
                    "Keep bullets and paragraph breaks. Plain text only.\n\n"
                    f"POST:\n{text}"
                )
                fixed = _call_llm(headers_fix, max_tokens=2600)
                if fixed:
                    text = _ensure_lead_title(fixed, title)
                    text = _fit_with_hashtags(text, context_tags, target)

            if len(text) >= min_len and _has_section_headers(text, min_headers=2):
                return text + suffix

            if len(text) < min_len:
                last_error = f"too short after attempt {attempt}: {len(text)} < {min_len}"
            else:
                last_error = f"missing section headers after attempt {attempt}"

    raise RuntimeError(f"LinkedIn post generation failed (fallback disabled): {last_error}")



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
    author_bio: str,
    include_link: bool,
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

    fallback_to_member = (author_mode == 'org')

    text = build_linkedin_post(title=title, description=description, content_html=content_html, author_bio=author_bio, include_link=include_link, url=url)
    if len(text) > 3000:
        text = _truncate_to(text, 3000)

    with open(hero_abs_path, "rb") as f:
        img_bytes = f.read()

    mime = _guess_mime_from_filename(hero_filename)
    try:
        asset, upload_url = linkedin_register_image_upload(access_token=access_token, owner_urn=author_urn)
        linkedin_upload_image(upload_url=upload_url, image_bytes=img_bytes, mime=mime)

        api_response = linkedin_create_image_post(access_token=access_token, author_urn=author_urn, text=text, asset_urn=asset, title=title)
        return {
            "api_response": api_response,
            "sent_text": text,
            "author_urn": author_urn,
        }
    except Exception as e:
        # If posting-as-org is not permitted, LinkedIn returns ACCESS_DENIED for /author.
        if fallback_to_member and _is_author_access_denied(e):
            author_urn = member_urn
            asset, upload_url = linkedin_register_image_upload(access_token=access_token, owner_urn=author_urn)
            linkedin_upload_image(upload_url=upload_url, image_bytes=img_bytes, mime=mime)
            api_response = linkedin_create_image_post(access_token=access_token, author_urn=author_urn, text=text, asset_urn=asset, title=title)
            return {
                'api_response': api_response,
                'sent_text': text,
                'author_urn': author_urn,
                'fallback': 'member',
            }
        raise
