import os
import json
import time
import difflib
import sqlite3
import secrets
import re
import threading
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from factory.db import db_init, db_connect, log_event
from factory.discovery import discover_topics
from factory.landing import (
    list_existing_posts,
    render_post_html,
    upsert_blog_index_card,
    remove_blog_index_card,
    upsert_sitemap_url,
    remove_sitemap_url,
    git_commit_push,
    git_commit_push_with_remove,
)
from factory.generate import generate_draft
from factory.validate import validate_draft
from factory.images import ensure_hero_and_inline_images
from factory.meta import fit_meta_description
from factory.linkedin import (
    linkedin_build_auth_url,
    linkedin_exchange_code,
    linkedin_get_member_id,
    db_get_linkedin,
    db_set_linkedin,
    db_clear_linkedin,
    db_create_state,
    db_consume_state,
    post_job_to_linkedin,
)
from factory.telegram import (
    build_telegram_post_ru,
    telegram_send,
    telegram_message_url,
)
from factory.twitter import (
    build_twitter_thread_ru,
    twitter_post_thread,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(APP_DIR, "templates")
DB_PATH = os.path.join(APP_DIR, "factory.sqlite")
ENV_PATH = os.path.join(APP_DIR, ".env")

LANDING_DIR = os.environ.get("LANDING_DIR", "/var/www/landing")
BLOG_DIR = os.path.join(LANDING_DIR, "blog")
SITEMAP_PATH = os.path.join(LANDING_DIR, "sitemap.xml")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

app = FastAPI()

_AUTOPUBLISH_LOCK = threading.Lock()
_AUTOPUBLISH_THREAD = None


SOCIAL_ENV_KEYS = {
    "LINKEDIN_CLIENT_ID",
    "LINKEDIN_CLIENT_SECRET",
    "LINKEDIN_REDIRECT_URI",
    "LINKEDIN_PERSON_URN",
    "LINKEDIN_ORG_URN",
    "LINKEDIN_AUTHOR_BIO",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TWITTER_BEARER_TOKEN",
}

SOCIAL_SECRET_KEYS = {
    "LINKEDIN_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TWITTER_BEARER_TOKEN",
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()



# Lightweight .env loader (so PM2 does not need env wiring).
# Lines: KEY=VALUE, supports comments (#) and quoted values.
def _load_dotenv(dotenv_path: str) -> None:
    if not dotenv_path or not os.path.exists(dotenv_path):
        return
    try:
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and (v[0] == v[-1]) and (v[0] in ("\"", "'")):
                    v = v[1:-1]
                if k and (k not in os.environ):
                    os.environ[k] = v
    except Exception:
        # Never fail startup on env parsing.
        return

# Keep AI rewrite source clean: the template already adds nav/share/cta blocks.
def _env_decode_line(raw: str) -> tuple[str, str] | None:
    line = (raw or "").strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    k, v = line.split("=", 1)
    k = k.strip()
    v = v.strip()
    if len(v) >= 2 and (v[0] == v[-1]) and (v[0] in ('"', "'")):
        v = v[1:-1]
    if not k:
        return None
    return k, v


def _env_encode_value(v: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@-]+", v or ""):
        return v
    return json.dumps(v or "")


def _env_file_values(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                kv = _env_decode_line(raw)
                if kv:
                    out[kv[0]] = kv[1]
    except Exception:
        return out
    return out


def _env_write_updates(path: str, updates: dict[str, str], clears: set[str]) -> None:
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    updates_left = dict(updates)
    out_lines: list[str] = []

    for raw in lines:
        kv = _env_decode_line(raw)
        if not kv:
            out_lines.append(raw)
            continue

        key = kv[0]
        if key in clears:
            continue
        if key in updates_left:
            out_lines.append(f"{key}={_env_encode_value(updates_left.pop(key))}\n")
            continue
        out_lines.append(raw)

    for key, value in updates_left.items():
        out_lines.append(f"{key}={_env_encode_value(value)}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out_lines)


def _social_settings_snapshot() -> dict[str, Any]:
    values = _env_file_values(ENV_PATH)

    def pick(key: str, *fallbacks: str) -> str:
        for k in (key, *fallbacks):
            v = (values.get(k) or os.environ.get(k) or "").strip()
            if v:
                return v
        return ""

    out: dict[str, Any] = {}
    out["LINKEDIN_CLIENT_ID"] = pick("LINKEDIN_CLIENT_ID", "LI_CLIENT_ID")
    out["LINKEDIN_CLIENT_SECRET"] = pick("LINKEDIN_CLIENT_SECRET", "LI_CLIENT_SECRET")
    out["LINKEDIN_REDIRECT_URI"] = pick("LINKEDIN_REDIRECT_URI") or "https://myugc.studio/factory/linkedin/callback"
    out["LINKEDIN_PERSON_URN"] = pick("LINKEDIN_PERSON_URN", "LI_PERSON_URN")
    out["LINKEDIN_ORG_URN"] = pick("LINKEDIN_ORG_URN")
    out["LINKEDIN_AUTHOR_BIO"] = pick("LINKEDIN_AUTHOR_BIO", "LI_AUTHOR_BIO")
    out["TELEGRAM_BOT_TOKEN"] = pick("TELEGRAM_BOT_TOKEN")
    out["TELEGRAM_CHAT_ID"] = pick("TELEGRAM_CHAT_ID")
    out["TWITTER_BEARER_TOKEN"] = pick("TWITTER_BEARER_TOKEN", "X_BEARER_TOKEN")

    masked: dict[str, Any] = {}
    for k in SOCIAL_ENV_KEYS:
        v = (out.get(k) or "").strip()
        if k in SOCIAL_SECRET_KEYS:
            if not v:
                masked[k] = {"value": "", "hasValue": False}
            elif len(v) <= 8:
                masked[k] = {"value": "*" * len(v), "hasValue": True}
            else:
                masked[k] = {"value": v[:4] + "..." + v[-2:], "hasValue": True}
        else:
            masked[k] = {"value": v, "hasValue": bool(v)}

    return {"values": out, "masked": masked}


def _sanitize_source_html(html: str | None) -> str | None:
    if not html:
        return None
    out = html
    out = re.sub(r"(?is)<nav[^>]+class=\"breadcrumbs\".*?</nav>", "", out)
    out = re.sub(r"(?is)<aside[^>]+class=\"toc-box\".*?</aside>", "", out)
    out = re.sub(r"(?is)<div[^>]+class=\"share-section\".*?</div>", "", out)
    out = re.sub(r"(?is)<div[^>]+class=\"cta-box\".*", "", out)
    out = re.sub(r"(?is)<script[^>]*>.*?</script>", "", out)
    return out.strip() or None


@app.on_event("startup")
def _startup() -> None:
    _load_dotenv(os.path.join(APP_DIR, '.env'))
    db_init(DB_PATH)
    _autopublish_start_scheduler()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "build": utcnow_iso(),
        },
    )


@app.get("/api/jobs")
def list_jobs():
    with db_connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, topic, slug, status, title, description, category, hero_image,
                   draft_html, faq_json, error, sources_json, visibility, created_at, updated_at, published_url,
                   linkedin_status, linkedin_post_url, linkedin_posted_at, linkedin_error,
                   telegram_status, telegram_post_url, telegram_posted_at, telegram_error,
                   twitter_status, twitter_post_url, twitter_posted_at, twitter_error
            FROM jobs
            ORDER BY created_at DESC
            LIMIT 500
            """
        ).fetchall()

    jobs = []
    for r in rows:
        parsed_sources = json.loads(r[11]) if r[11] else None
        sources = (parsed_sources.get("sources") if isinstance(parsed_sources, dict) else parsed_sources)
        queries = parsed_sources.get("queries") if isinstance(parsed_sources, dict) else None

        jobs.append(
            {
                "id": r[0],
                "topic": r[1],
                "slug": r[2],
                "status": r[3],
                "title": r[4],
                "description": r[5],
                "category": r[6],
                "heroImage": r[7],
                "draftHtml": r[8],
                "faq": json.loads(r[9]) if r[9] else None,
                "error": r[10],
                "sources": sources,
                "queries": queries,
                "sourcesCount": len(sources or []),
                "visibility": r[12],
                "createdAt": r[13],
                "updatedAt": r[14],
                "publishedUrl": r[15],
                "linkedinStatus": r[16],
                "linkedinPostUrl": r[17],
                "linkedinPostedAt": r[18],
                "linkedinError": r[19],
                "telegramStatus": r[20],
                "telegramPostUrl": r[21],
                "telegramPostedAt": r[22],
                "telegramError": r[23],
                "twitterStatus": r[24],
                "twitterPostUrl": r[25],
                "twitterPostedAt": r[26],
                "twitterError": r[27],
            }
        )

    return {"success": True, "jobs": jobs}


@app.post("/api/jobs")
async def create_job(request: Request):
    body = await request.json()
    topic = (body.get("topic") or "").strip()
    if not topic:
        raise HTTPException(status_code=400, detail="Missing topic")

    # Optional overrides
    category = (body.get("category") or "").strip() or None
    hero_image = (body.get("heroImage") or "").strip() or None
    visibility = (body.get("visibility") or "public").strip().lower()
    if visibility not in ("public", "hidden"):
        raise HTTPException(status_code=400, detail="visibility must be public|hidden")

    # slug can be empty; generate later
    slug = (body.get("slug") or "").strip() or None

    job_id = secrets.token_hex(12)
    now = utcnow_iso()

    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, topic, slug, status, category, hero_image, visibility, created_at, updated_at)
            VALUES (?, ?, ?, 'NEW', ?, ?, ?, ?, ?)
            """,
            (job_id, topic, slug, category, hero_image, visibility, now, now),
        )

    log_event(DB_PATH, job_id, "NEW", "Job created")
    return {"success": True, "id": job_id}




@app.post("/api/topics/discover")
async def api_topics_discover(request: Request):
    body = await request.json()
    direction = (body.get("direction") or body.get("topic") or "").strip()
    if len(direction) < 3:
        raise HTTPException(status_code=400, detail="Direction must be at least 3 characters")

    category_hint = (body.get("categoryHint") or body.get("category") or "").strip() or None

    try:
        limit = int(body.get("limit") or 20)
    except Exception:
        limit = 20
    limit = max(5, min(30, limit))

    try:
        data = discover_topics(direction=direction, limit=limit, category_hint=category_hint)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Topic discovery failed: {e}")

    return {"success": True, **data}


@app.get("/api/posts")
def list_posts():
    posts = list_existing_posts(BLOG_DIR)
    # Keep payload small
    return {"success": True, "posts": [{"slug": p.get("slug"), "title": p.get("title"), "url": p.get("url"), "category": p.get("category")} for p in posts]}


@app.post("/api/import")
async def import_existing_post(request: Request):
    body = await request.json()
    slug_or_url = (body.get("slugOrUrl") or body.get("slug") or "").strip()
    if not slug_or_url:
        raise HTTPException(status_code=400, detail="Missing slugOrUrl")

    raw = slug_or_url
    if "://" in raw:
        try:
            raw = urlparse(raw).path or raw
        except Exception:
            pass

    raw = raw.split("?", 1)[0].split("#", 1)[0].strip()
    raw = raw.rstrip("/")
    if raw.endswith(".html"):
        raw = raw[:-5]
    slug = raw.split("/")[-1].strip()
    if not slug:
        raise HTTPException(status_code=400, detail="Invalid slug")

    src_path = os.path.join(BLOG_DIR, f"{slug}.html")
    if not os.path.exists(src_path):
        posts = list_existing_posts(BLOG_DIR)
        slugs = [p.get('slug') for p in (posts or []) if p.get('slug')]
        # Prefer substring matches, then fuzzy matches.
        subs = [x for x in slugs if slug in x][:5]
        fuzzy = difflib.get_close_matches(slug, slugs, n=5, cutoff=0.35)
        sugg = []
        for x in subs + fuzzy:
            if x and x not in sugg:
                sugg.append(x)
        hint = (" Did you mean: " + ", ".join(sugg)) if sugg else ""
        raise HTTPException(status_code=404, detail=f"Not found: /blog/{slug}.html.{hint}")

    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()

    def strip_tags(html: str) -> str:
        return re.sub(r"<[^>]+>", "", html or "").strip()

    m_title = re.search(r"<h1[^>]*>(.*?)</h1>", src, flags=re.IGNORECASE | re.DOTALL)
    title = strip_tags(m_title.group(1)) if m_title else slug
    m_desc = re.search(r'<meta\s+name="description"\s+content="(.*?)"', src, flags=re.IGNORECASE | re.DOTALL)
    desc = (m_desc.group(1) or "").strip() if m_desc else ""
    m_cat = re.search(r'class="post-category"[^>]*>(.*?)</', src, flags=re.IGNORECASE | re.DOTALL)
    cat = strip_tags(m_cat.group(1)) if m_cat else ""

    hero = None
    m_og = re.search(r'<meta\s+property="og:image"\s+content="(.*?)"', src, flags=re.IGNORECASE | re.DOTALL)
    if m_og:
        og = (m_og.group(1) or "").strip()
        og = og.split("?", 1)[0]
        if og.startswith("https://myugc.studio/"):
            og = og[len("https://myugc.studio/"):]
        hero = os.path.basename(og) or None

    if not hero:
        m_bg = re.search(r'(?is)class="post-hero"[^>]*style="[^\"]*background-image:\s*url\((.*?)\)', src)
        if m_bg:
            bg = (m_bg.group(1) or "").strip().strip("\"'")
            hero = os.path.basename(bg) or None
    hero = hero or "logo.png"
    # Extract only the inner .post-content (exclude share/CTA blocks that the template adds).
    m_content = re.search(r'(?is)<div\s+class="post-content"[^>]*>(.*?)</div>\s*<div\s+class="share-section"', src)
    if not m_content:
        m_content = re.search(r'(?is)<div\s+class="post-content"[^>]*>(.*?)</div>\s*<div\s+class="cta-box"', src)
    if not m_content:
        m_content = re.search(r'(?is)<div\s+class="post-content"[^>]*>(.*?)</div>', src)
    content_html = (m_content.group(1) or "").strip() if m_content else ""
    if not content_html:
        raise HTTPException(status_code=400, detail="Could not extract .post-content")

    # Remove any factory-injected navigation blocks if present.
    content_html = re.sub(r'(?is)<nav[^>]+class="breadcrumbs".*?</nav>', "", content_html).strip()
    content_html = re.sub(r'(?is)<aside[^>]+class="toc-box".*?</aside>', "", content_html).strip()

    now = utcnow_iso()

    with db_connect(DB_PATH) as conn:
        ex = conn.execute("SELECT id FROM jobs WHERE slug=? ORDER BY created_at DESC LIMIT 1", (slug,)).fetchone()
        if ex:
            job_id = ex[0]
            conn.execute(
                """
                UPDATE jobs
                SET topic=?, status='READY', title=?, description=?, category=?, hero_image=?,
                    draft_html=?, faq_json=NULL, sources_json=NULL, error=NULL,
                    visibility=COALESCE(visibility,'public'), updated_at=?
                WHERE id=?
                """,
                (title or slug, title or slug, desc, cat, hero, content_html, now, job_id),
            )
        else:
            job_id = secrets.token_hex(12)
            conn.execute(
                """
                INSERT INTO jobs (id, topic, slug, status, title, description, category, hero_image, draft_html, visibility, created_at, updated_at)
                VALUES (?, ?, ?, 'READY', ?, ?, ?, ?, ?, 'public', ?, ?)
                """,
                (job_id, title or slug, slug, title or slug, desc, cat, hero, content_html, now, now),
            )

    log_event(DB_PATH, job_id, "READY", f"Imported from /blog/{slug}.html")
    return {"success": True, "id": job_id, "slug": slug}


@app.get("/api/jobs/{job_id}/logs")
def get_logs(job_id: str):
    with db_connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT ts, level, step, message FROM job_logs WHERE job_id = ? ORDER BY ts ASC LIMIT 2000",
            (job_id,),
        ).fetchall()
    logs = [{"ts": r[0], "level": r[1], "step": r[2], "message": r[3]} for r in rows]
    return {"success": True, "logs": logs}


@app.post("/api/jobs/{job_id}/generate")
def generate(job_id: str):
    with db_connect(DB_PATH) as conn:
        job = conn.execute(
            "SELECT id, topic, slug, status, category, hero_image, draft_html FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    _id, topic, slug, status, category, hero_image, draft_html = job

    log_event(DB_PATH, job_id, "INFO", "Starting generation")

    with db_connect(DB_PATH) as conn:
        conn.execute("UPDATE jobs SET status='GENERATING', error=NULL, updated_at=? WHERE id=?", (utcnow_iso(), job_id))

    log_event(DB_PATH, job_id, "INFO", "Status: GENERATING")

    existing = list_existing_posts(BLOG_DIR)
    draft = None
    problems: list[str] = []

    # Repair loop: enforce spec by feeding validation errors back to the model.
    for attempt in range(1, 4):
        try:
            log_event(DB_PATH, job_id, "INFO", f"Generate attempt {attempt}/3")
            draft = generate_draft(
                topic=topic,
                existing_posts=existing,
                category=category,
                hero_image=hero_image,
                slug_hint=slug,
                source_html=_sanitize_source_html(draft_html) if (draft_html and status != "NEW") else None,
                previous=draft,
                problems=problems if attempt > 1 else None,
            )
        except Exception as e:
            # Do not fail the job immediately; keep retrying (model JSON can be flaky).
            msg = f"Generation failed: {e}"
            log_event(DB_PATH, job_id, "WARN", msg)
            problems = [msg]
            continue
        before_desc = (draft.get("description") or "").strip()
        draft["description"] = fit_meta_description(draft.get("description"), fallback=topic or draft.get("title"))
        if draft["description"] != before_desc:
            log_event(DB_PATH, job_id, "INFO", f"Auto-fit meta description length: {len(before_desc)} -> {len(draft["description"])}")

        problems = validate_draft(draft)
        if not problems:
            break

        msg = "Validation failed: " + "; ".join(problems[:10])
        log_event(DB_PATH, job_id, "WARN", msg)

    if problems:
        msg = "Validation failed: " + "; ".join(problems[:10])
        log_event(DB_PATH, job_id, "ERROR", msg)
        with db_connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE jobs SET status='ERROR', error=?, updated_at=? WHERE id=?",
                (msg, utcnow_iso(), job_id),
            )
        return JSONResponse(status_code=200, content={"success": False, "error": msg, "problems": problems})

    # Generate hero + inline images immediately after successful draft generation
    # so Preview already shows real media (not only after Publish).
    try:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        image_model = (
            os.environ.get("GEMINI_IMAGE_MODEL")
            or os.environ.get("GEMINI_MODEL_IMAGE")
            or "gemini-2.5-flash-image"
        )
        hero_file, content_html, generated = ensure_hero_and_inline_images(
            api_key=api_key,
            image_model=image_model,
            blog_dir=BLOG_DIR,
            slug=draft.get("slug") or slug or _id,
            topic=topic or draft.get("title") or "",
            title=draft.get("title") or "",
            category=draft.get("category") or category or "Strategy",
            hero_image_hint=draft.get("heroImage") or hero_image,
            content_html=draft.get("contentHtml") or "",
        )
        draft["heroImage"] = hero_file
        draft["contentHtml"] = content_html
        if generated:
            log_event(DB_PATH, job_id, "INFO", f"Generated {len(generated)} image files for preview")
    except Exception as e:
        log_event(DB_PATH, job_id, "WARN", f"Image generation during generate failed: {e}")

    now = utcnow_iso()
    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status='READY', slug=?, title=?, description=?, category=?, hero_image=?,
                draft_html=?, faq_json=?, sources_json=?, error=NULL, updated_at=?
            WHERE id=?
            """,
            (
                draft["slug"],
                draft["title"],
                draft["description"],
                draft["category"],
                draft["heroImage"],
                draft["contentHtml"],
                json.dumps(draft.get("faq") or []),
                json.dumps({"sources": draft.get("sources") or [], "queries": draft.get("searchQueries") or []}),
                now,
                job_id,
            ),
        )

    log_event(DB_PATH, job_id, "READY", "Draft generated and validated")
    return {"success": True}


@app.get("/preview/{job_id}", response_class=HTMLResponse)
def preview(job_id: str):
    with db_connect(DB_PATH) as conn:
        r = conn.execute(
            "SELECT slug, title, description, category, hero_image, draft_html, faq_json, sources_json, updated_at FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()

    if not r:
        raise HTTPException(status_code=404, detail="Job not found")

    slug, title, desc, cat, hero, content_html, faq_json, sources_json, updated_at = r
    if not content_html:
        raise HTTPException(status_code=400, detail="No draft yet")

    faq = json.loads(faq_json) if faq_json else []
    parsed_sources = json.loads(sources_json) if sources_json else None
    sources = (parsed_sources.get("sources") if isinstance(parsed_sources, dict) else parsed_sources) or []

    html = render_post_html(
        blog_dir=BLOG_DIR,
        title=title or "",
        description=desc or "",
        category=cat or "Strategy",
        slug=slug or "preview",
        hero_image=hero or "logo.png",
        content_html=content_html,
        faq=faq,
        sources=sources,
        updated_at=updated_at or utcnow_iso(),
        noindex=True,
    )

    return HTMLResponse(content=html)


@app.post("/api/jobs/{job_id}/publish")
def publish(job_id: str):
    with db_connect(DB_PATH) as conn:
        r = conn.execute(
            """
            SELECT status, topic, slug, title, description, category, hero_image, draft_html, faq_json,
                   sources_json, updated_at, published_url, visibility
            FROM jobs
            WHERE id=?
            """,
            (job_id,),
        ).fetchone()

    if not r:
        raise HTTPException(status_code=404, detail="Job not found")

    status, topic, slug, title, desc, cat, hero, content_html, faq_json, sources_json, updated_at, published_url, visibility = r

    if status not in ("READY", "PUBLISHED", "ERROR"):
        raise HTTPException(status_code=400, detail=f"Job status must be READY, PUBLISHED, or ERROR, got {status}")

    if not slug or not content_html:
        raise HTTPException(status_code=400, detail="Missing slug or content")

    faq = json.loads(faq_json) if faq_json else []
    parsed_sources = json.loads(sources_json) if sources_json else None
    sources = (parsed_sources.get("sources") if isinstance(parsed_sources, dict) else parsed_sources) or []

    visibility = (visibility or "hidden").strip().lower()
    if visibility not in ("public", "hidden"):
        visibility = "hidden"

    # Hidden means: page exists, but not indexable and not linked from blog index/sitemap.
    noindex = visibility != "public"

    log_event(DB_PATH, job_id, "INFO", f"Publishing to landing (visibility={visibility})")

    # Auto-generate hero + inline images into /var/www/landing/blog
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    image_model = (
        os.environ.get("GEMINI_IMAGE_MODEL")
        or os.environ.get("GEMINI_MODEL_IMAGE")
        or "gemini-2.5-flash-image"
    )

    image_paths: list[str] = []
    try:
        hero_file, content_html, generated = ensure_hero_and_inline_images(
            api_key=api_key,
            image_model=image_model,
            blog_dir=BLOG_DIR,
            slug=slug,
            topic=topic or title or slug,
            title=title or "",
            category=cat or "Strategy",
            hero_image_hint=hero,
            content_html=content_html,
        )
        hero = hero_file
        image_paths = [os.path.join("blog", g.filename) for g in (generated or [])]
        # Always include hero in git add if it exists.
        if hero and os.path.exists(os.path.join(BLOG_DIR, os.path.basename(hero))):
            image_paths.append(os.path.join("blog", os.path.basename(hero)))
    except Exception as e:
        log_event(DB_PATH, job_id, "WARN", f"Image generation skipped/failed: {e}")

    html = render_post_html(
        blog_dir=BLOG_DIR,
        title=title or "",
        description=desc or "",
        category=cat or "Strategy",
        slug=slug,
        hero_image=hero or "logo.png",
        content_html=content_html,
        faq=faq,
        sources=sources,
        updated_at=updated_at or utcnow_iso(),
        noindex=noindex,
    )

    out_path = os.path.join(BLOG_DIR, f"{slug}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    url = f"https://myugc.studio/blog/{slug}.html"

    # Update blog index and sitemap according to visibility.
    if noindex:
        remove_blog_index_card(BLOG_DIR, slug=slug)
        remove_sitemap_url(SITEMAP_PATH, url=url)
    else:
        upsert_blog_index_card(
            BLOG_DIR,
            slug=slug,
            title=title or "",
            description=desc or "",
            category=cat or "Strategy",
            hero_image=os.path.basename(hero or "logo.png"),
        )
        upsert_sitemap_url(SITEMAP_PATH, url=url)

    # Git commit+push (include images)
    paths = [
        os.path.join("blog", f"{slug}.html"),
        os.path.join("blog", "index.html"),
        "sitemap.xml",
    ] + (image_paths or [])

    # de-dupe while preserving order
    seen = set()
    deduped = []
    for pp in paths:
        if pp in seen:
            continue
        seen.add(pp)
        deduped.append(pp)

    git_commit_push(
        repo_dir=LANDING_DIR,
        message=f"Auto-generated post: {title}",
        paths=deduped,
    )

    with db_connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE jobs SET status='PUBLISHED', published_url=?, hero_image=?, draft_html=?, error=NULL, updated_at=? WHERE id=?",
            (url, os.path.basename(hero or "logo.png"), content_html, utcnow_iso(), job_id),
        )

    log_event(DB_PATH, job_id, "PUBLISHED", f"Published: {url}")
    return {"success": True, "url": url}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with db_connect(DB_PATH) as conn:
        r = conn.execute(
            """
            SELECT id, topic, slug, status, title, description, category, hero_image,
                   draft_html, faq_json, error, sources_json, visibility, created_at, updated_at, published_url,
                   linkedin_status, linkedin_post_url, linkedin_posted_at, linkedin_error,
                   telegram_status, telegram_post_url, telegram_posted_at, telegram_error,
                   twitter_status, twitter_post_url, twitter_posted_at, twitter_error
            FROM jobs
            WHERE id=?
            """,
            (job_id,),
        ).fetchone()

    if not r:
        raise HTTPException(status_code=404, detail="Job not found")

    parsed_sources = json.loads(r[11]) if r[11] else None
    sources = (parsed_sources.get("sources") if isinstance(parsed_sources, dict) else parsed_sources)
    queries = parsed_sources.get("queries") if isinstance(parsed_sources, dict) else None

    return {
        "success": True,
        "job": {
            "id": r[0],
            "topic": r[1],
            "slug": r[2],
            "status": r[3],
            "title": r[4],
            "description": r[5],
            "category": r[6],
            "heroImage": r[7],
            "draftHtml": r[8],
            "faq": json.loads(r[9]) if r[9] else None,
            "error": r[10],
            "sources": sources,
            "queries": queries,
            "visibility": r[12],
            "createdAt": r[13],
            "updatedAt": r[14],
            "publishedUrl": r[15],
            "linkedinStatus": r[16],
            "linkedinPostUrl": r[17],
            "linkedinPostedAt": r[18],
            "linkedinError": r[19],
            "telegramStatus": r[20],
            "telegramPostUrl": r[21],
            "telegramPostedAt": r[22],
            "telegramError": r[23],
            "twitterStatus": r[24],
            "twitterPostUrl": r[25],
            "twitterPostedAt": r[26],
            "twitterError": r[27],
        },
    }


@app.put("/api/jobs/{job_id}")
async def update_job(job_id: str, request: Request):
    body = await request.json()

    with db_connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT slug, published_url FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()

    if not cur:
        raise HTTPException(status_code=404, detail="Job not found")

    cur_slug, published_url = cur

    updates: dict[str, Any] = {}

    def set_if(key: str, val: Any):
        if val is not None:
            updates[key] = val

    if isinstance(body.get("topic"), str):
        set_if("topic", body["topic"].strip())

    if isinstance(body.get("slug"), str):
        slug = body["slug"].strip() or None
        if published_url and slug and slug != cur_slug:
            raise HTTPException(status_code=400, detail="Cannot change slug after publish")
        set_if("slug", slug)

    if isinstance(body.get("title"), str):
        set_if("title", body["title"].strip())

    if isinstance(body.get("description"), str):
        set_if("description", body["description"].strip())

    if isinstance(body.get("category"), str):
        set_if("category", body["category"].strip())

    if isinstance(body.get("heroImage"), str):
        set_if("hero_image", body["heroImage"].strip())

    if isinstance(body.get("draftHtml"), str):
        set_if("draft_html", body["draftHtml"])

    if body.get("faq") is not None:
        if not isinstance(body["faq"], list):
            raise HTTPException(status_code=400, detail="faq must be a list")
        set_if("faq_json", json.dumps(body["faq"]))

    if isinstance(body.get("visibility"), str):
        visibility = body["visibility"].strip().lower()
        if visibility not in ("public", "hidden"):
            raise HTTPException(status_code=400, detail="visibility must be public|hidden")
        set_if("visibility", visibility)

    if not updates:
        return {"success": True}

    updates["status"] = "READY"
    updates["updated_at"] = utcnow_iso()
    updates["error"] = None

    sets = ", ".join([f"{k}=?" for k in updates.keys()])
    vals = list(updates.values())
    vals.append(job_id)

    with db_connect(DB_PATH) as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", vals)

    log_event(DB_PATH, job_id, "INFO", "Job updated")
    return {"success": True}


@app.post("/api/jobs/{job_id}/unpublish")
def unpublish(job_id: str):
    with db_connect(DB_PATH) as conn:
        r = conn.execute(
            "SELECT slug FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()

    if not r:
        raise HTTPException(status_code=404, detail="Job not found")

    slug = r[0]
    if not slug:
        raise HTTPException(status_code=400, detail="Missing slug")

    out_rel = os.path.join("blog", f"{slug}.html")
    out_abs = os.path.join(BLOG_DIR, f"{slug}.html")
    url = f"https://myugc.studio/blog/{slug}.html"

    if os.path.exists(out_abs):
        os.remove(out_abs)

    remove_blog_index_card(BLOG_DIR, slug=slug)
    remove_sitemap_url(SITEMAP_PATH, url=url)

    git_commit_push_with_remove(
        repo_dir=LANDING_DIR,
        message=f"Unpublish post: {slug}",
        add_paths=[os.path.join("blog", "index.html"), "sitemap.xml"],
        remove_paths=[out_rel],
    )

    with db_connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE jobs SET status='READY', published_url=NULL, updated_at=? WHERE id=?",
            (utcnow_iso(), job_id),
        )

    log_event(DB_PATH, job_id, "INFO", f"Unpublished: {url}")
    return {"success": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    with db_connect(DB_PATH) as conn:
        r = conn.execute(
            "SELECT slug, published_url FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()

    if not r:
        raise HTTPException(status_code=404, detail="Job not found")

    slug, published_url = r

    removed_paths: list[str] = []

    if slug:
        out_rel = os.path.join("blog", f"{slug}.html")
        out_abs = os.path.join(BLOG_DIR, f"{slug}.html")
        url = f"https://myugc.studio/blog/{slug}.html"

        if os.path.exists(out_abs):
            os.remove(out_abs)
            removed_paths.append(out_rel)

        remove_blog_index_card(BLOG_DIR, slug=slug)
        remove_sitemap_url(SITEMAP_PATH, url=url)

    if removed_paths:
        git_commit_push_with_remove(
            repo_dir=LANDING_DIR,
            message=f"Delete factory post: {slug}",
            add_paths=[os.path.join("blog", "index.html"), "sitemap.xml"],
            remove_paths=removed_paths,
        )

    with db_connect(DB_PATH) as conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        conn.execute("DELETE FROM job_logs WHERE job_id=?", (job_id,))

    return {"success": True}


# --- Auto Publish Scheduler ---

def _ap_read_settings() -> dict[str, Any]:
    with db_connect(DB_PATH) as conn:
        r = conn.execute(
            """
            SELECT enabled, times_per_day, channels_json, timezone, start_hour, end_hour, last_slot_key, last_run_at
            FROM autopublish_settings
            WHERE id=1
            """
        ).fetchone()

    if not r:
        return {
            "enabled": False,
            "times_per_day": 3,
            "channels": ["linkedin", "telegram", "twitter"],
            "timezone": "UTC",
            "start_hour": 9,
            "end_hour": 21,
            "last_slot_key": None,
            "last_run_at": None,
        }

    channels = []
    try:
        parsed = json.loads(r[2] or "[]")
        if isinstance(parsed, list):
            channels = [str(x).strip().lower() for x in parsed if str(x).strip().lower() in ("linkedin", "telegram", "twitter")]
    except Exception:
        channels = []
    if not channels:
        channels = ["linkedin", "telegram", "twitter"]

    return {
        "enabled": bool(r[0]),
        "times_per_day": int(r[1] or 3),
        "channels": channels,
        "timezone": (r[3] or "UTC").strip() or "UTC",
        "start_hour": int(r[4] if r[4] is not None else 9),
        "end_hour": int(r[5] if r[5] is not None else 21),
        "last_slot_key": r[6],
        "last_run_at": r[7],
    }


def _ap_write_settings(*, enabled: bool, times_per_day: int, channels: list[str], timezone_name: str, start_hour: int, end_hour: int, last_slot_key: str | None = None, last_run_at: str | None = None) -> None:
    ch_json = json.dumps(channels)
    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE autopublish_settings
            SET enabled=?, times_per_day=?, channels_json=?, timezone=?, start_hour=?, end_hour=?,
                last_slot_key=COALESCE(?, last_slot_key),
                last_run_at=COALESCE(?, last_run_at),
                updated_at=?
            WHERE id=1
            """,
            (1 if enabled else 0, times_per_day, ch_json, timezone_name, start_hour, end_hour, last_slot_key, last_run_at, utcnow_iso()),
        )


def _ap_slots(times_per_day: int, start_hour: int, end_hour: int) -> list[int]:
    n = max(1, min(8, int(times_per_day or 1)))
    start = max(0, min(23, int(start_hour)))
    end = max(0, min(23, int(end_hour)))
    if end < start:
        start, end = end, start
    if n == 1:
        return [int(round((start + end) / 2))]
    step = (end - start) / max(1, (n - 1))
    out = sorted(set(max(0, min(23, int(round(start + i * step)))) for i in range(n)))
    if not out:
        out = [start]
    return out


def _ap_now_local(tz_name: str) -> datetime:
    tz_name = (tz_name or "UTC").strip() or "UTC"
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _ap_wait_channel(job_id: str, channel: str, timeout_s: int = 240) -> tuple[bool, str | None, str | None]:
    status_col = f"{channel}_status"
    err_col = f"{channel}_error"
    url_col = f"{channel}_post_url"

    started = time.time()
    while time.time() - started < timeout_s:
        with db_connect(DB_PATH) as conn:
            r = conn.execute(f"SELECT {status_col}, {err_col}, {url_col} FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            return False, "job not found", None
        st = (r[0] or "").upper().strip()
        err = r[1]
        url = r[2]
        if st == "POSTED":
            return True, None, url
        if st == "ERROR":
            return False, err or f"{channel} failed", None
        time.sleep(2)

    return False, f"{channel} timeout", None


def _ap_log_run(started_at: str, finished_at: str, trigger: str, job_id: str | None, status: str, result: dict[str, Any]) -> None:
    with db_connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO autopublish_runs (started_at, finished_at, trigger, job_id, status, result_json) VALUES (?, ?, ?, ?, ?, ?)",
            (started_at, finished_at, trigger, job_id, status, json.dumps(result)),
        )


def _run_autopublish(trigger: str = "manual") -> dict[str, Any]:
    if not _AUTOPUBLISH_LOCK.acquire(blocking=False):
        return {"success": False, "status": "BUSY", "message": "autopublish already running"}

    started = utcnow_iso()
    try:
        settings = _ap_read_settings()
        channels = settings.get("channels") or []

        if trigger != "manual" and not settings.get("enabled"):
            result = {"success": False, "status": "DISABLED"}
            _ap_log_run(started, utcnow_iso(), trigger, None, "DISABLED", result)
            return result

        with db_connect(DB_PATH) as conn:
            j = conn.execute(
                "SELECT id FROM jobs WHERE status='READY' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()

        if not j:
            result = {"success": True, "status": "NOOP", "message": "no READY jobs"}
            _ap_log_run(started, utcnow_iso(), trigger, None, "NOOP", result)
            return result

        job_id = j[0]
        summary: dict[str, Any] = {"job_id": job_id, "channels": {}, "site_publish": None}

        # 1) publish site first
        try:
            out = publish(job_id)
            summary["site_publish"] = {"ok": True, "url": (out or {}).get("url") if isinstance(out, dict) else None}
        except Exception as e:
            msg = f"site publish failed: {e}"
            summary["site_publish"] = {"ok": False, "error": msg}
            _ap_log_run(started, utcnow_iso(), trigger, job_id, "ERROR", summary)
            return {"success": False, "status": "ERROR", **summary}

        # 2) publish socials
        if not channels:
            channels = ["linkedin", "telegram", "twitter"]

        for ch in channels:
            try:
                if ch == "linkedin":
                    linkedin_publish(job_id, {"as": "member", "includeLink": True})
                elif ch == "telegram":
                    telegram_publish(job_id, {"includeLink": True})
                elif ch == "twitter":
                    twitter_publish(job_id, {})
                else:
                    continue

                ok, err, url = _ap_wait_channel(job_id, ch)
                summary["channels"][ch] = {"ok": ok, "error": err, "url": url}
            except Exception as e:
                summary["channels"][ch] = {"ok": False, "error": str(e), "url": None}

        all_ok = all(v.get("ok") for v in summary["channels"].values()) if summary["channels"] else True
        status = "DONE" if all_ok else "PARTIAL"
        _ap_log_run(started, utcnow_iso(), trigger, job_id, status, summary)
        return {"success": all_ok, "status": status, **summary}
    finally:
        _AUTOPUBLISH_LOCK.release()


def _autopublish_loop() -> None:
    while True:
        try:
            st = _ap_read_settings()
            if st.get("enabled"):
                now_local = _ap_now_local(st.get("timezone") or "UTC")
                slots = _ap_slots(st.get("times_per_day") or 3, st.get("start_hour") or 9, st.get("end_hour") or 21)
                if now_local.hour in slots and now_local.minute < 10:
                    key = f"{now_local.date().isoformat()}-{now_local.hour:02d}"
                    if key != (st.get("last_slot_key") or ""):
                        _run_autopublish(trigger="schedule")
                        _ap_write_settings(
                            enabled=bool(st.get("enabled")),
                            times_per_day=int(st.get("times_per_day") or 3),
                            channels=list(st.get("channels") or ["linkedin", "telegram", "twitter"]),
                            timezone_name=(st.get("timezone") or "UTC"),
                            start_hour=int(st.get("start_hour") or 9),
                            end_hour=int(st.get("end_hour") or 21),
                            last_slot_key=key,
                            last_run_at=utcnow_iso(),
                        )
        except Exception:
            pass

        time.sleep(30)


def _autopublish_start_scheduler() -> None:
    global _AUTOPUBLISH_THREAD
    if _AUTOPUBLISH_THREAD and _AUTOPUBLISH_THREAD.is_alive():
        return
    _AUTOPUBLISH_THREAD = threading.Thread(target=_autopublish_loop, daemon=True, name="autopublish-scheduler")
    _AUTOPUBLISH_THREAD.start()


@app.get("/api/autopublish/settings")
def autopublish_get_settings():
    st = _ap_read_settings()
    slots = _ap_slots(st.get("times_per_day") or 3, st.get("start_hour") or 9, st.get("end_hour") or 21)
    return {"success": True, **st, "slots": slots}


@app.put("/api/autopublish/settings")
async def autopublish_set_settings(request: Request):
    body = await request.json()

    enabled = bool(body.get("enabled", False))
    times_per_day = int(body.get("timesPerDay") or body.get("times_per_day") or 3)
    times_per_day = max(1, min(8, times_per_day))

    channels = body.get("channels") or ["linkedin", "telegram", "twitter"]
    if not isinstance(channels, list):
        raise HTTPException(status_code=400, detail="channels must be list")
    channels = [str(x).strip().lower() for x in channels if str(x).strip().lower() in ("linkedin", "telegram", "twitter")]

    timezone_name = (body.get("timezone") or "UTC").strip() or "UTC"
    start_hour = int(body.get("startHour") if body.get("startHour") is not None else 9)
    end_hour = int(body.get("endHour") if body.get("endHour") is not None else 21)
    start_hour = max(0, min(23, start_hour))
    end_hour = max(0, min(23, end_hour))

    st = _ap_read_settings()
    _ap_write_settings(
        enabled=enabled,
        times_per_day=times_per_day,
        channels=channels,
        timezone_name=timezone_name,
        start_hour=start_hour,
        end_hour=end_hour,
        last_slot_key=st.get("last_slot_key"),
        last_run_at=st.get("last_run_at"),
    )

    return {"success": True}


@app.post("/api/autopublish/run")
def autopublish_run_now():
    out = _run_autopublish(trigger="manual")
    return {"success": True, "result": out}


@app.get("/api/autopublish/runs")
def autopublish_runs(limit: int = 20):
    lim = max(1, min(100, int(limit or 20)))
    with db_connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, started_at, finished_at, trigger, job_id, status, result_json FROM autopublish_runs ORDER BY id DESC LIMIT ?",
            (lim,),
        ).fetchall()

    out = []
    for r in rows:
        try:
            result = json.loads(r[6]) if r[6] else None
        except Exception:
            result = None
        out.append({
            "id": r[0],
            "startedAt": r[1],
            "finishedAt": r[2],
            "trigger": r[3],
            "jobId": r[4],
            "status": r[5],
            "result": result,
        })

    return {"success": True, "runs": out}



@app.get("/api/settings/social")
def settings_social_get():
    snap = _social_settings_snapshot()
    return {
        "success": True,
        "values": snap.get("values") or {},
        "masked": snap.get("masked") or {},
    }


@app.put("/api/settings/social")
async def settings_social_put(request: Request):
    body = await request.json()
    values = body.get("values") or {}
    clear = body.get("clear") or []

    if not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="values must be object")
    if not isinstance(clear, list):
        raise HTTPException(status_code=400, detail="clear must be list")

    updates: dict[str, str] = {}
    clears: set[str] = set()

    for k in clear:
        key = str(k or "").strip()
        if key in SOCIAL_ENV_KEYS:
            clears.add(key)

    for k, v in values.items():
        key = str(k or "").strip()
        if key not in SOCIAL_ENV_KEYS:
            continue
        val = str(v or "").strip()
        if val:
            updates[key] = val

    # Aliases kept in sync for compatibility with older env naming.
    if "LINKEDIN_CLIENT_ID" in updates:
        updates["LI_CLIENT_ID"] = updates["LINKEDIN_CLIENT_ID"]
    if "LINKEDIN_CLIENT_SECRET" in updates:
        updates["LI_CLIENT_SECRET"] = updates["LINKEDIN_CLIENT_SECRET"]
    if "LINKEDIN_PERSON_URN" in updates:
        updates["LI_PERSON_URN"] = updates["LINKEDIN_PERSON_URN"]
    if "LINKEDIN_AUTHOR_BIO" in updates:
        updates["LI_AUTHOR_BIO"] = updates["LINKEDIN_AUTHOR_BIO"]

    if "LINKEDIN_CLIENT_ID" in clears:
        clears.add("LI_CLIENT_ID")
    if "LINKEDIN_CLIENT_SECRET" in clears:
        clears.add("LI_CLIENT_SECRET")
    if "LINKEDIN_PERSON_URN" in clears:
        clears.add("LI_PERSON_URN")
    if "LINKEDIN_AUTHOR_BIO" in clears:
        clears.add("LI_AUTHOR_BIO")
    if "TWITTER_BEARER_TOKEN" in clears:
        clears.add("X_BEARER_TOKEN")

    _env_write_updates(ENV_PATH, updates, clears)

    for k in clears:
        os.environ.pop(k, None)
    for k, v in updates.items():
        os.environ[k] = v

    snap = _social_settings_snapshot()
    return {
        "success": True,
        "saved": sorted(list(updates.keys())),
        "cleared": sorted(list(clears)),
        "values": snap.get("values") or {},
        "masked": snap.get("masked") or {},
    }




# --- LinkedIn integration ---

@app.get("/api/linkedin/status")
def linkedin_status():
    auth = db_get_linkedin(DB_PATH) or {}
    org_env = (os.environ.get("LINKEDIN_ORG_URN") or "").strip()
    org_db = (auth.get("org_urn") or "").strip()
    org = org_env or org_db or None
    return {
        "success": True,
        "connected": bool((auth.get("access_token") or "").strip()),
        "memberUrn": auth.get("member_urn"),
        "orgUrn": org,
        "orgUrnConfigured": bool(org_env),
    }


@app.post("/api/linkedin/disconnect")
def linkedin_disconnect():
    db_clear_linkedin(DB_PATH)
    return {"success": True}


@app.get("/linkedin/connect")
def linkedin_connect(request: Request):
    client_id = (os.environ.get("LINKEDIN_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("LINKEDIN_CLIENT_SECRET") or "").strip()
    redirect_uri = (os.environ.get("LINKEDIN_REDIRECT_URI") or "").strip() or "https://myugc.studio/factory/linkedin/callback"

    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Missing LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET in .env")

    mode = (request.query_params.get('as') or 'member').strip().lower()
    if mode not in ('member', 'org'):
        mode = 'member'

    state = secrets.token_urlsafe(24)
    db_create_state(DB_PATH, provider="linkedin", state=state)
    url = linkedin_build_auth_url(client_id=client_id, redirect_uri=redirect_uri, state=state, mode=mode)
    return RedirectResponse(url=url, status_code=302)


@app.get("/linkedin/callback", response_class=HTMLResponse)
def linkedin_callback(code: str | None = None, state: str | None = None, error: str | None = None, error_description: str | None = None):
    if error:
        msg = f"LinkedIn OAuth error: {error}"
        if error_description:
            msg += f" ({error_description})"
        return HTMLResponse(
            content=f"<h3>{msg}</h3><p><a href='/factory/'>Back to Factory</a></p>",
            status_code=400,
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state")

    if not db_consume_state(DB_PATH, provider="linkedin", state=state, max_age_min=20):
        raise HTTPException(status_code=400, detail="Invalid/expired state")

    client_id = (os.environ.get("LINKEDIN_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("LINKEDIN_CLIENT_SECRET") or "").strip()
    redirect_uri = (os.environ.get("LINKEDIN_REDIRECT_URI") or "").strip() or "https://myugc.studio/factory/linkedin/callback"

    data = linkedin_exchange_code(code=code, redirect_uri=redirect_uri, client_id=client_id, client_secret=client_secret)

    access_token = (data.get("access_token") or "").strip()
    refresh_token = (data.get("refresh_token") or "").strip() or None
    expires_in = int(data.get("expires_in") or 0)
    expires_at_iso = (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=expires_in)).isoformat() if expires_in else None

    if not access_token:
        raise HTTPException(status_code=400, detail=f"No access_token returned: {data}")
    member_urn_env = (os.environ.get("LINKEDIN_PERSON_URN") or os.environ.get("LI_PERSON_URN") or "").strip() or None
    if member_urn_env:
        member_urn = member_urn_env
    else:
        member_id = linkedin_get_member_id(access_token=access_token)
        member_urn = f"urn:li:person:{member_id}"

    org_env = (os.environ.get("LINKEDIN_ORG_URN") or "").strip() or None

    db_set_linkedin(
        DB_PATH,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at_iso,
        member_urn=member_urn,
        org_urn=org_env,
    )

    return RedirectResponse(url="/factory/", status_code=302)


@app.post("/api/jobs/{job_id}/linkedin/publish")
def linkedin_publish(job_id: str, payload: dict[str, Any] | None = None):
    payload = payload or {}

    client_id = (os.environ.get("LINKEDIN_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("LINKEDIN_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Missing LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET")

    mode = (payload.get("as") or "member").strip().lower()
    if mode not in ("member", "org"):
        mode = "member"


    include_link = bool(payload.get("includeLink"))

    auth = db_get_linkedin(DB_PATH) or {}
    member_urn = (auth.get("member_urn") or "").strip()
    if not member_urn:
        raise HTTPException(status_code=400, detail="LinkedIn not connected")

    org_env = (os.environ.get("LINKEDIN_ORG_URN") or "").strip() or None
    org_urn = (payload.get("orgUrn") or "").strip() or org_env or (auth.get("org_urn") or "").strip() or None

    with db_connect(DB_PATH) as conn:
        job = conn.execute(
            "SELECT topic, slug, title, description, category, hero_image, draft_html, status, published_url, linkedin_status FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    topic, slug, title, description, category, hero_image, draft_html, status, published_url, li_status = job

    if li_status == "POSTING":
        return {"success": True, "status": "POSTING"}

    if not slug:
        raise HTTPException(status_code=400, detail="Missing slug")

    # We post a link to the live blog page.
    url = (published_url or f"https://myugc.studio/blog/{slug}.html").strip()

    hero_filename = os.path.basename(hero_image or "")
    hero_abs = os.path.join(BLOG_DIR, hero_filename) if hero_filename else ""
    if not hero_filename or not os.path.exists(hero_abs):
        raise HTTPException(status_code=400, detail="Hero image file not found in /var/www/landing/blog. Publish the article first so the hero is generated.")

    author_bio = (os.environ.get("LINKEDIN_AUTHOR_BIO") or os.environ.get("LI_AUTHOR_BIO") or "").strip()
    if not author_bio:
        author_bio = "I build practical marketing and workflow systems. Here's what I learned."

    # Mark as POSTING immediately so UI can disable the button.
    with db_connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE jobs SET linkedin_status='POSTING', linkedin_error=NULL, updated_at=? WHERE id=?",
            (utcnow_iso(), job_id),
        )

    log_event(DB_PATH, job_id, "INFO", f"LinkedIn posting started: mode={mode}")

    import threading

    def _worker():
        try:
            resp = post_job_to_linkedin(
                db_path=DB_PATH,
                client_id=client_id,
                client_secret=client_secret,
                author_mode=mode,
                member_urn=member_urn,
                org_urn=org_urn,
                title=title or topic,
                description=description or "",
                 content_html=draft_html or "",
                author_bio=author_bio,
                include_link=include_link,
                url=url,
                hero_abs_path=hero_abs,
                hero_filename=hero_filename,
            )

            post_id = None
            if isinstance(resp, dict):
                post_id = resp.get("id") or resp.get("urn") or resp.get("value")

            with db_connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE jobs SET linkedin_status='POSTED', linkedin_post_url=?, linkedin_posted_at=?, linkedin_error=NULL, updated_at=? WHERE id=?",
                    (post_id, utcnow_iso(), utcnow_iso(), job_id),
                )

            log_event(DB_PATH, job_id, "READY", "Posted to LinkedIn")
        except Exception as e:
            msg = f"LinkedIn publish failed: {e}"
            with db_connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE jobs SET linkedin_status='ERROR', linkedin_error=?, updated_at=? WHERE id=?",
                    (msg, utcnow_iso(), job_id),
                )
            log_event(DB_PATH, job_id, "ERROR", msg)

    threading.Thread(target=_worker, daemon=True).start()

    return {"success": True, "status": "POSTING"}


@app.post("/api/jobs/{job_id}/telegram/publish")
def telegram_publish(job_id: str, payload: dict[str, Any] | None = None):
    payload = payload or {}

    bot_token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (payload.get("chatId") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not bot_token or not chat_id:
        raise HTTPException(status_code=500, detail="Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")

    include_link = bool(payload.get("includeLink", True))

    with db_connect(DB_PATH) as conn:
        job = conn.execute(
            "SELECT topic, slug, title, description, hero_image, draft_html, status, published_url, telegram_status FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    topic, slug, title, description, hero_image, draft_html, status, published_url, tg_status = job

    if tg_status == "POSTING":
        return {"success": True, "status": "POSTING"}

    if not slug:
        raise HTTPException(status_code=400, detail="Missing slug")

    url = (published_url or f"https://myugc.studio/blog/{slug}.html").strip()
    hero_filename = os.path.basename(hero_image or "")
    hero_abs = os.path.join(BLOG_DIR, hero_filename) if hero_filename else ""
    if not hero_filename or not os.path.exists(hero_abs):
        hero_abs = None

    with db_connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE jobs SET telegram_status='POSTING', telegram_error=NULL, updated_at=? WHERE id=?",
            (utcnow_iso(), job_id),
        )
    log_event(DB_PATH, job_id, "INFO", "Telegram posting started")

    import threading

    def _worker():
        try:
            text = build_telegram_post_ru(
                title=title or topic,
                description=description or "",
                content_html=draft_html or "",
                url=url,
                include_link=include_link,
            )
            res = telegram_send(
                bot_token=bot_token,
                chat_id=chat_id,
                text=text,
                photo_abs_path=hero_abs,
            )
            message_id = (((res.get("message") or {}).get("result") or {}).get("message_id"))
            post_url = telegram_message_url(chat_id, message_id)

            with db_connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE jobs SET telegram_status='POSTED', telegram_post_url=?, telegram_posted_at=?, telegram_error=NULL, updated_at=? WHERE id=?",
                    (post_url, utcnow_iso(), utcnow_iso(), job_id),
                )
            log_event(DB_PATH, job_id, "READY", "Posted to Telegram")
        except Exception as e:
            msg = f"Telegram publish failed: {e}"
            with db_connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE jobs SET telegram_status='ERROR', telegram_error=?, updated_at=? WHERE id=?",
                    (msg, utcnow_iso(), job_id),
                )
            log_event(DB_PATH, job_id, "ERROR", msg)

    threading.Thread(target=_worker, daemon=True).start()
    return {"success": True, "status": "POSTING"}


@app.post("/api/jobs/{job_id}/twitter/publish")
def twitter_publish(job_id: str, payload: dict[str, Any] | None = None):
    payload = payload or {}

    access_token = (os.environ.get("TWITTER_BEARER_TOKEN") or os.environ.get("X_BEARER_TOKEN") or "").strip()
    if not access_token:
        raise HTTPException(status_code=500, detail="Missing TWITTER_BEARER_TOKEN (OAuth2 User token required)")

    with db_connect(DB_PATH) as conn:
        job = conn.execute(
            "SELECT topic, slug, title, description, draft_html, status, published_url, twitter_status FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    topic, slug, title, description, draft_html, status, published_url, tw_status = job

    if tw_status == "POSTING":
        return {"success": True, "status": "POSTING"}

    if not slug:
        raise HTTPException(status_code=400, detail="Missing slug")

    url = (published_url or f"https://myugc.studio/blog/{slug}.html").strip()

    with db_connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE jobs SET twitter_status='POSTING', twitter_error=NULL, updated_at=? WHERE id=?",
            (utcnow_iso(), job_id),
        )
    log_event(DB_PATH, job_id, "INFO", "X/Twitter thread posting started")

    import threading

    def _worker():
        try:
            tweets = build_twitter_thread_ru(
                title=title or topic,
                description=description or "",
                content_html=draft_html or "",
                url=url,
                max_posts=6,
            )
            out = twitter_post_thread(access_token=access_token, tweets=tweets)
            post_url = out.get("thread_url")

            with db_connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE jobs SET twitter_status='POSTED', twitter_post_url=?, twitter_posted_at=?, twitter_error=NULL, updated_at=? WHERE id=?",
                    (post_url, utcnow_iso(), utcnow_iso(), job_id),
                )
            log_event(DB_PATH, job_id, "READY", "Posted X/Twitter thread")
        except Exception as e:
            msg = f"X/Twitter publish failed: {e}"
            with db_connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE jobs SET twitter_status='ERROR', twitter_error=?, updated_at=? WHERE id=?",
                    (msg, utcnow_iso(), job_id),
                )
            log_event(DB_PATH, job_id, "ERROR", msg)

    threading.Thread(target=_worker, daemon=True).start()
    return {"success": True, "status": "POSTING"}
