import os
import json
import time
import difflib
import sqlite3
import secrets
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from factory.db import db_init, db_connect, log_event
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

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(APP_DIR, "templates")
DB_PATH = os.path.join(APP_DIR, "factory.sqlite")

LANDING_DIR = os.environ.get("LANDING_DIR", "/var/www/landing")
BLOG_DIR = os.path.join(LANDING_DIR, "blog")
SITEMAP_PATH = os.path.join(LANDING_DIR, "sitemap.xml")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

app = FastAPI()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# Keep AI rewrite source clean: the template already adds nav/share/cta blocks.
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
    db_init(DB_PATH)


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
                   draft_html, faq_json, error, sources_json, visibility, created_at, updated_at, published_url
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
    visibility = (body.get("visibility") or "hidden").strip().lower()
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
                   draft_html, faq_json, error, sources_json, visibility, created_at, updated_at, published_url
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
