import os
import json
import time
import difflib
import sqlite3
import secrets
import re
import threading
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urlparse
import urllib.request
import urllib.error
import html as html_lib

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
    extract_article_image_urls_for_x,
    twitter_post_thread,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(APP_DIR, "templates")
DB_PATH = os.path.join(APP_DIR, "factory.sqlite")
ENV_PATH = os.path.join(APP_DIR, ".env")

LANDING_DIR = os.environ.get("LANDING_DIR", "/var/www/landing")
BLOG_DIR = os.path.join(LANDING_DIR, "blog")
SITEMAP_PATH = os.path.join(LANDING_DIR, "sitemap-en.xml")
LOCALES = ("ru", "es", "de", "fr")


CATEGORY_CANONICAL = (
    "Wineries & Travel",

    "Wine Regions",
    "Grape Varieties",
    "Food Pairing",
    "Buying Guides",
)

CATEGORY_LOCALIZED = {
    "en": {
        "Wineries & Travel": "Wineries & Travel",
        "Wine Regions": "Wine Regions",
        "Grape Varieties": "Grape Varieties",
        "Food Pairing": "Food Pairing",
        "Buying Guides": "Buying Guides",
    },
    "ru": {
        "Wineries & Travel": "Винодельни и путешествия",
        "Wine Regions": "Винные регионы",
        "Grape Varieties": "Сорта винограда",
        "Food Pairing": "Подбор еды и вина",
        "Buying Guides": "Гайды по покупке",
    },
    "es": {
        "Wineries & Travel": "Bodegas y viajes",
        "Wine Regions": "Regiones vinícolas",
        "Grape Varieties": "Variedades de uva",
        "Food Pairing": "Maridaje",
        "Buying Guides": "Guías de compra",
    },
    "de": {
        "Wineries & Travel": "Weingüter & Reisen",
        "Wine Regions": "Wine Regions",
        "Grape Varieties": "Rebsorten",
        "Food Pairing": "Food Pairing",
        "Buying Guides": "Kaufratgeber",
    },
    "fr": {
        "Wineries & Travel": "Domaines & voyages",
        "Wine Regions": "Régions viticoles",
        "Grape Varieties": "Cépages",
        "Food Pairing": "Accords mets-vins",
        "Buying Guides": "Guides d'achat",
    },
}


def _canonical_wine_category(value: str | None, *, fallback: str = "Buying Guides") -> str:
    t = (value or "").strip().lower()

    if not t:
        return fallback

    for x in CATEGORY_CANONICAL:
        if t == x.lower():
            return x

    if re.search(r"(winery|wineries|travel|vineyard|oenotour|bodega|bodegas|viaje|viajes|weingut|reisen|domaines?|voyage|винодель|путешеств)", t):
        return "Wineries & Travel"
    if re.search(r"(region|regions|terroir|appellation|rioja|tuscany|bordeaux|регион|терруар|regiones|weinregion|région)", t):
        return "Wine Regions"
    if re.search(r"(grape|grapes|variet|viticulture|uva|uvas|cepage|cépage|rebsorte|виноград|сорт)", t):
        return "Grape Varieties"
    if re.search(r"(pair|pairing|food|dish|meal|maridaje|comida|accord|mets|speise|еда|блюд|сочет)", t):
        return "Food Pairing"
    if re.search(r"(buy|buying|guide|guides|price|cost|gift|compr|kauf|achat|покуп|гайд|руковод)", t):
        return "Buying Guides"

    return fallback


def _localize_category(canonical: str, locale: str = "en") -> str:
    labels = CATEGORY_LOCALIZED.get(locale) or CATEGORY_LOCALIZED["en"]
    return labels.get(canonical, canonical)


def _pick_category_from_content(*, topic: str | None, title: str | None, description: str | None, category_hint: str | None, content_html: str | None = None) -> str:
    base = _canonical_wine_category(category_hint, fallback="") if category_hint else ""
    text = " ".join([topic or "", title or "", description or "", category_hint or "", (content_html or "")[:1600]])
    guessed = _canonical_wine_category(text, fallback="Buying Guides")
    return guessed or base or "Buying Guides"

templates = Jinja2Templates(directory=TEMPLATES_DIR)

app = FastAPI()

_AUTOPUBLISH_LOCK = threading.Lock()
_TOPIC_DISCOVERY_LOCK = threading.Lock()
_AUTOPUBLISH_THREAD = None



SITE_ENV_KEYS = {
    "SITE_CTA_ENABLED",
    "SITE_CTA_TITLE",
    "SITE_CTA_TEXT",
    "SITE_CTA_BUTTON_TEXT",
    "SITE_CTA_BUTTON_URL",
    "SITE_CONTEXT",
    "SITE_SUBTOPICS",
    "SITE_BG_COLOR",
    "SITE_BG_ANIMATION",
    "SITE_BG_ANIMATION_SPEED",
    "SITE_ACCENT_COLOR",
    "SITE_ENABLED_LANGS",
}


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
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
    "GEMINI_API_KEY",
    "GEMINI_TEXT_MODEL",
    "GEMINI_IMAGE_MODEL",
}

SOCIAL_SECRET_KEYS = {
    "LINKEDIN_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TWITTER_BEARER_TOKEN",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
    "GEMINI_API_KEY",
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _site_origin() -> str:
    raw = (os.environ.get("SITE_ORIGIN") or "https://myugc.studio").strip()
    if not raw:
        raw = "https://myugc.studio"
    return raw.rstrip("/")



def _site_context() -> str:
    raw = (os.environ.get("SITE_CONTEXT") or "").strip()
    return raw or "Wine culture, tasting, wine regions, wineries, food pairing, and buying guidance"


def _optimize_site_images() -> None:
    """Best-effort image optimization in landing repo (creates .webp variants)."""
    try:
        subprocess.check_call(["node", "scripts/optimize-images.js"], cwd=LANDING_DIR)
    except Exception:
        pass


def _site_subtopics() -> list[str]:
    raw = (os.environ.get("SITE_SUBTOPICS") or "").strip()
    if not raw:
        return ["wine travel", "food pairing", "wineries", "grape varieties"]
    parts = re.split(r"[,\n;|]+", raw)
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        x = re.sub(r"\s+", " ", (p or "").strip())
        if not x:
            continue
        k = x.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    return out or ["wine travel", "food pairing", "wineries", "grape varieties"]


def _rotate_discovery_direction() -> str:
    ctx = _site_context()
    subs = _site_subtopics()
    if not subs:
        return ctx
    idx = int(datetime.now(timezone.utc).strftime("%j")) % len(subs)
    return f"{ctx}: {subs[idx]}"


def _gsc_site_url() -> str:
    raw = (os.environ.get("GSC_SITE_URL") or "").strip()
    if raw:
        if raw.startswith("sc-domain:"):
            return raw
        return raw if raw.endswith("/") else (raw + "/")
    origin = _site_origin().rstrip("/")
    return origin + "/"


def _submit_sitemaps_to_search_console(sitemaps: list[str]) -> dict[str, Any]:
    creds = (os.environ.get("GSC_CREDENTIALS_FILE") or os.path.join(APP_DIR, "keys", "gsc-service-account.json")).strip()
    script = os.path.join(APP_DIR, "scripts", "gsc_submit.js")
    site_url = _gsc_site_url()

    if not os.path.exists(script):
        return {"success": False, "error": f"gsc submit script not found: {script}"}
    if not os.path.exists(creds):
        return {"success": False, "error": f"gsc credentials not found: {creds}"}

    payload = {
        "credentials": creds,
        "siteUrl": site_url,
        "sitemaps": [s for s in (sitemaps or []) if s],
    }

    try:
        cp = subprocess.run(
            ["node", script],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}

    stdout = (cp.stdout or "").strip()
    stderr = (cp.stderr or "").strip()
    data = None
    if stdout:
        try:
            data = json.loads(stdout)
        except Exception:
            data = {"raw": stdout}

    ok = (cp.returncode == 0) and isinstance(data, dict) and bool(data.get("success"))
    if ok:
        return {"success": True, "result": data}

    return {
        "success": False,
        "error": (data.get("error") if isinstance(data, dict) else None) or stderr or stdout or f"exit {cp.returncode}",
        "result": data,
    }



def _ensure_sitemap(path: str) -> None:
    if not path or os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n')
        f.write('<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\"></urlset>\n')


def _locale_blog_dir(locale: str) -> str:
    return os.path.join(LANDING_DIR, locale, "blog")


def _locale_sitemap_path(locale: str) -> str:
    return os.path.join(LANDING_DIR, f"sitemap-{locale}.xml")


def _rebuild_blog_feed_from_index(index_path: str, out_path: str) -> None:
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            src = f.read()
    except Exception:
        return

    pattern = re.compile(
        r'<a\s+href="([^"]+)"\s+class="blog-card">[\s\S]*?'
        r'<div\s+class="card-image"[^>]*?background-image:\s*url\(\'([^\']+)\'\)[^>]*?>[\s\S]*?'
        r'<span\s+class="category">([\s\S]*?)</span>[\s\S]*?'
        r'<h3\s+class="card-title">([\s\S]*?)</h3>[\s\S]*?'
        r'<p\s+class="card-excerpt">([\s\S]*?)</p>[\s\S]*?'
        r'</a>',
        flags=re.IGNORECASE,
    )

    posts = []
    for m in pattern.finditer(src):
        href = (m.group(1) or '').strip()
        image = (m.group(2) or '').strip()
        category = html_lib.unescape(re.sub(r'<[^>]+>', '', (m.group(3) or ''))).strip()
        title = html_lib.unescape(re.sub(r'<[^>]+>', '', (m.group(4) or ''))).strip()
        desc = html_lib.unescape(re.sub(r'<[^>]+>', '', (m.group(5) or ''))).strip()

        if not href:
            continue
        if image and not image.startswith('/'):
            image = '/blog/' + image.lstrip('./')

        posts.append({
            'href': href,
            'image': image or '/hero_ai.jpg',
            'category': category,
            'title': title,
            'description': desc,
        })

    out = {'updatedAt': utcnow_iso(), 'posts': posts}
    try:
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
    except Exception:
        return


def _apply_hreflang_block(html: str, slug: str, locale: str) -> str:
    origin = _site_origin()
    canonical = f"{origin}/{locale}/blog/{slug}.html" if locale != "en" else f"{origin}/blog/{slug}.html"
    alts = {
        "en": f"{origin}/blog/{slug}.html",
        "ru": f"{origin}/ru/blog/{slug}.html",
        "es": f"{origin}/es/blog/{slug}.html",
        "de": f"{origin}/de/blog/{slug}.html",
        "fr": f"{origin}/fr/blog/{slug}.html",
    }
    block = (
        f'<link href="{canonical}" rel="canonical"/>'
        + "".join([f'<link href="{u}" hreflang="{k}" rel="alternate"/>' for k, u in alts.items()])
        + f'<link href="{alts["en"]}" hreflang="x-default" rel="alternate"/>'
    )
    html = re.sub(r"(?is)<link\s+[^>]*rel=[\"\']canonical[\"\'][^>]*>", "", html)
    html = re.sub(r"(?is)<link\s+[^>]*hreflang=[\"\'][^\"\']+[\"\'][^>]*rel=[\"\']alternate[\"\'][^>]*>", "", html)
    html = re.sub(r"(?is)<link\s+[^>]*rel=[\"\']alternate[\"\'][^>]*hreflang=[\"\'][^\"\']+[\"\'][^>]*>", "", html)
    html = re.sub(
        r"(?is)<meta\s+[^>]*property=[\"\']og:url[\"\'][^>]*>",
        f'<meta content="{canonical}" property="og:url"/>',
        html,
        count=1,
    )
    if "</head>" in html:
        html = html.replace("</head>", block + "</head>", 1)
    return html


def _translate_post_payload(
    *,
    api_key: str,
    model: str,
    locale: str,
    slug: str,
    title: str,
    description: str,
    category: str,
    content_html: str,
    faq: list[dict[str, Any]],
) -> dict[str, Any]:
    prompt = {
        "task": "translate_blog_post_html",
        "target_language": locale,
        "rules": [
            "Translate naturally, keep meaning and structure.",
            "All human-readable output must be in target language, except product/brand names and technical acronyms.",
            "Do not leave title/description/body in English when target language is not English.",
            "Do not translate brand names or product names.",
            "Keep all links, image src, filenames, and URLs unchanged.",
            "Keep valid HTML. Preserve tags and heading hierarchy.",
            "Return STRICT JSON only.",
        ],
        "input": {
            "slug": slug,
            "title": title,
            "description": description,
            "category": category,
            "contentHtml": content_html,
            "faq": faq,
        },
        "output_shape": {
            "title": "string",
            "description": "string",
            "category": "string",
            "contentHtml": "string",
            "faq": [{"question": "string", "answer": "string"}],
        },
    }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = {
        "generationConfig": {"responseMimeType": "application/json"},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    def _norm_text(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip()).lower()

    last_err: Exception | None = None
    for _attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = json.loads(resp.read().decode("utf-8"))

            text = (((raw.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [{}])[0].get("text") or ""
            text = re.sub(r"^```(?:json)?\s*", "", text.strip())
            text = re.sub(r"\s*```$", "", text)
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start:end + 1]
            out = json.loads(text)

            tr_title = (out.get("title") or title).strip()
            tr_desc = (out.get("description") or description).strip()
            tr_cat = (out.get("category") or category).strip() or category
            tr_html = out.get("contentHtml") or content_html
            tr_faq = out.get("faq") if isinstance(out.get("faq"), list) else faq

            if locale != "en":
                same_title = _norm_text(tr_title) == _norm_text(title)
                same_desc = _norm_text(tr_desc) == _norm_text(description)
                if same_title and same_desc:
                    raise ValueError("translation looks unchanged from EN source")

            return {
                "title": tr_title,
                "description": tr_desc,
                "category": tr_cat,
                "contentHtml": tr_html,
                "faq": tr_faq,
            }
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"translation failed after retries: {last_err}")


def _save_social_post(
    *,
    job_id: str,
    channel: str,
    content_text: str | None,
    content_json: dict[str, Any] | list[Any] | None,
    remote_url: str | None,
    status: str,
) -> None:
    payload = json.dumps(content_json, ensure_ascii=False) if content_json is not None else None
    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO social_posts (job_id, channel, content_text, content_json, remote_url, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, channel, content_text, payload, remote_url, status, utcnow_iso()),
        )



def _mark_stale_social_postings(max_age_min: int = 5) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_min)).replace(microsecond=0).isoformat()
    now = utcnow_iso()
    stale_msg = f"Stale POSTING timeout after {max_age_min} minutes"

    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET telegram_status='ERROR', telegram_error=?, updated_at=?
            WHERE telegram_status='POSTING' AND updated_at < ?
            """,
            (stale_msg, now, cutoff),
        )
        conn.execute(
            """
            UPDATE jobs
            SET linkedin_status='ERROR', linkedin_error=?, updated_at=?
            WHERE linkedin_status='POSTING' AND updated_at < ?
            """,
            (stale_msg, now, cutoff),
        )
        conn.execute(
            """
            UPDATE jobs
            SET twitter_status='ERROR', twitter_error=?, updated_at=?
            WHERE twitter_status='POSTING' AND updated_at < ?
            """,
            (stale_msg, now, cutoff),
        )


def _mark_stale_generating_jobs(max_age_min: int = 45) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_min)).replace(microsecond=0).isoformat()
    now = utcnow_iso()
    stale_msg = f"Stale GENERATING timeout after {max_age_min} minutes"

    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status='ERROR', error=?, updated_at=?
            WHERE status='GENERATING' AND updated_at < ?
            """,
            (stale_msg, now, cutoff),
        )


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
                # Always override site-specific keys from local .env for this factory instance.
                force_keys = {
                    "LANDING_DIR",
                    "SITE_ORIGIN",
                    "GSC_SITE_URL",
                    "GSC_CREDENTIALS_FILE",
                    "GSC_ENABLED",
                }
                if k and (k in force_keys or (k not in os.environ) or not (os.environ.get(k) or "").strip()):
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


def _normalize_linkedin_org_urn(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    # Accept either full URN (urn:li:organization:123) or plain numeric ID.
    if raw.lower().startswith("urn:li:organization:"):
        tail = raw.split(":")[-1].strip()
        if tail.isdigit():
            return f"urn:li:organization:{tail}"
    digits = re.sub(r"\D+", "", raw)
    if digits:
        return f"urn:li:organization:{digits}"
    raise ValueError("LINKEDIN_ORG_URN must be organization numeric id or urn:li:organization:<id>")


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


def _sanitize_hex_color(value: str | None, default: str = "#12070c") -> str:
    raw = (value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
        return raw.lower()
    if re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        return ("#" + raw).lower()
    return default


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = _sanitize_hex_color(hex_color).lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix_rgb(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, float(t)))
    return (
        int(round(a[0] + (b[0] - a[0]) * t)),
        int(round(a[1] + (b[1] - a[1]) * t)),
        int(round(a[2] + (b[2] - a[2]) * t)),
    )


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb



def _rgba(rgb: tuple[int, int, int], alpha: float) -> str:
    a = max(0.0, min(1.0, float(alpha)))
    return f"rgba({rgb[0]},{rgb[1]},{rgb[2]},{a:.2f})"


def _sanitize_bg_animation(value: str | None) -> str:
    v = (value or "").strip().lower()
    allowed = {"wine", "aurora", "sunset", "minimal"}
    return v if v in allowed else "wine"


def _sanitize_bg_speed(value: str | None, default: int = 34) -> int:
    try:
        n = int(str(value or "").strip())
    except Exception:
        n = default
    return max(8, min(120, n))




def _pick_theme_profile(context: str, subtopics: list[str]) -> str:
    text = ((context or "") + " " + " ".join(subtopics or [])).lower()
    if re.search(r"\b(wine|sommel|vineyard|grape|winery|cellar|pairing|rioja|bordeaux|burgundy|tuscany)\b", text):
        return "wine"
    if re.search(r"\b(ai|artificial intelligence|automation|agent|llm|prompt|model|machine learning|ml|tech|software|saas)\b", text):
        return "ai"
    if re.search(r"\b(travel|tour|trip|route|itinerary|destination|hotel|flight)\b", text):
        return "travel"
    if re.search(r"\b(ecommerce|shopify|dropshipping|conversion|product page|ads|ugc|marketing)\b", text):
        return "ecommerce"
    return "generic"


def _theme_pulse_values(profile: str, primary_subtopic: str = "") -> list[dict[str, Any]]:
    st = (primary_subtopic or "").strip().lower()
    if profile == "wine":
        if "travel" in st or "route" in st or "wineries" in st:
            return [
                {"value": 62, "suffix": "M"},
                {"value": 5.7, "suffix": "K"},
                {"value": 4.2, "suffix": "D"},
                {"value": 29, "suffix": "%"},
            ]
        if "pair" in st or "food" in st:
            return [
                {"value": 35, "suffix": "%"},
                {"value": 22, "suffix": "%"},
                {"value": 3.1, "suffix": "x"},
                {"value": 17, "suffix": "%"},
            ]
        if "grape" in st or "variet" in st:
            return [
                {"value": 10000, "suffix": "+"},
                {"value": 1200, "suffix": "+"},
                {"value": 8.4, "suffix": "K"},
                {"value": 26, "suffix": "%"},
            ]
        if "buy" in st or "guide" in st:
            return [
                {"value": 41, "suffix": "%"},
                {"value": 63, "suffix": "%"},
                {"value": 2.6, "suffix": "x"},
                {"value": 19, "suffix": "%"},
            ]
        return [
            {"value": 62, "suffix": "M"},
            {"value": 11.3, "suffix": "B"},
            {"value": 35, "suffix": "%"},
            {"value": 18, "suffix": "%"},
        ]

    m = {
        "ai": [
            {"value": 47, "suffix": "%"},
            {"value": 9.8, "suffix": "B"},
            {"value": 28, "suffix": "%"},
            {"value": 31, "suffix": "%"},
        ],
        "travel": [
            {"value": 74, "suffix": "M"},
            {"value": 13.6, "suffix": "B"},
            {"value": 41, "suffix": "%"},
            {"value": 22, "suffix": "%"},
        ],
        "ecommerce": [
            {"value": 58, "suffix": "%"},
            {"value": 6.9, "suffix": "B"},
            {"value": 33, "suffix": "%"},
            {"value": 24, "suffix": "%"},
        ],
        "generic": [
            {"value": 49, "suffix": "%"},
            {"value": 7.4, "suffix": "B"},
            {"value": 27, "suffix": "%"},
            {"value": 16, "suffix": "%"},
        ],
    }
    return m.get(profile, m["generic"])


def _theme_pulse_texts(profile: str, locale: str = "en", primary_subtopic: str = "") -> list[dict[str, str]]:
    st = (primary_subtopic or "").strip().lower()
    L = (locale or "en").lower()

    base_en = {
        "wine_default": [
            {"label":"Global wine tourists / year","meta":"Source blend: OIV + UN Tourism estimates"},
            {"label":"Annual sparkling wine market (USD)","meta":"Rounded industry estimate"},
            {"label":"Buyers choosing by food pairing","meta":"Consumer trend studies"},
            {"label":"Growth in no/low alcohol segment","meta":"YoY category trend"},
        ],
        "wine_travel": [
            {"label":"Wine-route travelers / year","meta":"Tourism boards + destination estimates"},
            {"label":"Active winery destinations","meta":"Major mapped wine destinations"},
            {"label":"Avg route length","meta":"Multi-day itinerary benchmark"},
            {"label":"Travelers adding tasting stops","meta":"Trip planning behavior"},
        ],
        "wine_pairing": [
            {"label":"Shoppers guided by pairing","meta":"Meal-first buying behavior"},
            {"label":"Higher order value with pairing","meta":"Basket uplift estimate"},
            {"label":"Conversion lift with pairing cards","meta":"Site UX benchmark"},
            {"label":"Repeat buyers from pairing content","meta":"Retention trend"},
        ],
        "wine_grape": [
            {"label":"Documented grape varieties","meta":"Viticulture reference sources"},
            {"label":"Commercial wine grapes","meta":"Global production varieties"},
            {"label":"Major appellations","meta":"Regional designation datasets"},
            {"label":"Readers preferring grape-led guides","meta":"Content preference trend"},
        ],
        "wine_buy": [
            {"label":"Buyers checking guides before purchase","meta":"Decision behavior trend"},
            {"label":"Consumers comparing labels in-store","meta":"Shelf behavior estimate"},
            {"label":"Conversion lift from buying guides","meta":"Editorial benchmark"},
            {"label":"Returns reduced by expectation matching","meta":"Post-purchase quality fit"},
        ],
        "ai": [
            {"label":"Teams using AI weekly","meta":"Adoption pulse"},
            {"label":"AI software market (USD)","meta":"Rounded market estimate"},
            {"label":"Workflows automated end-to-end","meta":"Ops trend"},
            {"label":"Cycle time reduction","meta":"Productivity benchmark"},
        ],
        "travel": [
            {"label":"Travelers planning routes online","meta":"Global planning behavior"},
            {"label":"Travel experiences market (USD)","meta":"Rounded estimate"},
            {"label":"Users preferring local guides","meta":"Search intent trend"},
            {"label":"Growth in curated itineraries","meta":"YoY trend"},
        ],
        "ecommerce": [
            {"label":"Stores investing in content-led growth","meta":"Commerce benchmark"},
            {"label":"Creator-commerce market (USD)","meta":"Rounded estimate"},
            {"label":"Teams running weekly experiments","meta":"Optimization rhythm"},
            {"label":"Lower CAC from better creative ops","meta":"Performance trend"},
        ],
        "generic": [
            {"label":"Audience growth from useful content","meta":"Editorial baseline"},
            {"label":"Niche media market size (USD)","meta":"Rounded estimate"},
            {"label":"Readers returning monthly","meta":"Loyalty trend"},
            {"label":"Faster publishing velocity","meta":"Workflow improvement"},
        ]
    }

    key = profile
    if profile == "wine":
        if "travel" in st or "route" in st or "wineries" in st:
            key = "wine_travel"
        elif "pair" in st or "food" in st:
            key = "wine_pairing"
        elif "grape" in st or "variet" in st:
            key = "wine_grape"
        elif "buy" in st or "guide" in st:
            key = "wine_buy"
        else:
            key = "wine_default"

    en = base_en.get(key, base_en["generic"])
    if L == "en":
        return en

    trans = {
      "ru": {
        "wine_default":[
          {"label":"Винные туристы в мире / год","meta":"Оценки OIV и UN Tourism"},
          {"label":"Рынок игристых вин (USD)","meta":"Округленная оценка"},
          {"label":"Покупатели, выбирающие по фуд-пейрингу","meta":"Потребительский тренд"},
          {"label":"Рост сегмента no/low alcohol","meta":"Год к году"},
        ]
      },
      "es": {
        "wine_default":[
          {"label":"Turistas del vino en el mundo / año","meta":"Estimaciones OIV + UN Tourism"},
          {"label":"Mercado anual de espumosos (USD)","meta":"Estimación redondeada"},
          {"label":"Compradores que eligen por maridaje","meta":"Tendencia de consumo"},
          {"label":"Crecimiento en no/low alcohol","meta":"Tendencia interanual"},
        ]
      },
      "de": {
        "wine_default":[
          {"label":"Weintouristen weltweit / Jahr","meta":"Schätzung aus OIV + UN Tourism"},
          {"label":"Jährlicher Schaumweinmarkt (USD)","meta":"Gerundete Schätzung"},
          {"label":"Käufer mit Fokus auf Food Pairing","meta":"Konsumententrend"},
          {"label":"Wachstum no/low alcohol Segment","meta":"Jahrestrend"},
        ]
      },
      "fr": {
        "wine_default":[
          {"label":"Œnotouristes dans le monde / an","meta":"Estimations OIV + UN Tourism"},
          {"label":"Marché annuel des vins effervescents (USD)","meta":"Estimation arrondie"},
          {"label":"Acheteurs guidés par l'accord mets-vins","meta":"Tendance consommateurs"},
          {"label":"Croissance du segment no/low alcohol","meta":"Tendance annuelle"},
        ]
      }
    }
    loc = trans.get(L, {})
    arr = loc.get(key) or loc.get('wine_default') or en
    if len(arr) < 4:
        arr = (arr + en)[:4]
    return arr


def _apply_pulse_profile_to_landing() -> dict[str, Any]:
    ctx = _site_context()
    subs = _site_subtopics()
    primary = (subs[0] if subs else "")
    profile = _pick_theme_profile(ctx, subs)

    files = [("en", os.path.join(LANDING_DIR, "index.html"))]
    for loc in LOCALES:
        files.append((loc, os.path.join(LANDING_DIR, loc, "index.html")))

    changed = 0
    scanned = 0
    values = _theme_pulse_values(profile, primary)
    for loc, p in files:
        if not os.path.exists(p):
            continue
        scanned += 1
        try:
            with open(p, "r", encoding="utf-8") as f:
                src = f.read()
        except Exception:
            continue

        items = []
        texts = _theme_pulse_texts(profile, loc, primary)
        for i in range(4):
            v = values[i] if i < len(values) else {"value": 0, "suffix": ""}
            t = texts[i] if i < len(texts) else {"label": "", "meta": ""}
            items.append({"value": v.get("value"), "suffix": v.get("suffix"), "label": t.get("label"), "meta": t.get("meta")})
        line = "window.__PULSE_ITEMS = " + json.dumps(items, ensure_ascii=False) + ";"

        if "window.__PULSE_ITEMS" in src:
            new = re.sub(r"window\.__PULSE_ITEMS\s*=\s*[^;]*;", line, src, count=1)
        else:
            anchor = "function renderWineStats(){"
            if anchor in src:
                new = src.replace(anchor, line + "\n\n    " + anchor, 1)
            else:
                continue

        if new != src:
            try:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(new)
                changed += 1
            except Exception:
                pass

    return {"ok": True, "profile": profile, "primary": primary, "values": values, "scanned": scanned, "changed": changed}


def _build_theme_override_css(bg_color: str, animation: str, speed: int, accent_color: str) -> str:
    base = _hex_to_rgb(bg_color)
    dark = _mix_rgb(base, (0, 0, 0), 0.35)
    mid = base
    light = _mix_rgb(base, (255, 255, 255), 0.22)

    if animation == "aurora":
        r1, r2, r3 = (56, 189, 248), (168, 85, 247), (34, 197, 94)
    elif animation == "sunset":
        r1, r2, r3 = (251, 146, 60), (244, 63, 94), (245, 158, 11)
    elif animation == "minimal":
        r1, r2, r3 = _mix_rgb(base, (255, 255, 255), 0.1), _mix_rgb(base, (0, 0, 0), 0.1), _mix_rgb(base, (255, 255, 255), 0.2)
    else:  # wine
        r1, r2, r3 = _mix_rgb(base, (190, 24, 93), 0.55), _mix_rgb(base, (225, 29, 72), 0.35), _mix_rgb(base, (136, 19, 55), 0.45)

    grad_dark = '#%02x%02x%02x' % dark
    grad_mid = '#%02x%02x%02x' % mid
    grad_light = '#%02x%02x%02x' % light
    grad = f"linear-gradient(135deg, {_sanitize_hex_color(grad_dark)} 0%, {_sanitize_hex_color(grad_mid)} 50%, {_sanitize_hex_color(grad_light)} 100%)"
    acc = _sanitize_hex_color(accent_color, "#b63a5a")
    acc_hover = _sanitize_hex_color(_rgb_to_hex(_mix_rgb(_hex_to_rgb(acc), (0,0,0), 0.18)), "#962f49")

    css = (
        f":root {{\n"
        f"  --bg-dark: {_sanitize_hex_color(bg_color)};\n"
        f"  --bg-gradient: {grad};\n"
        f"  --accent: {acc};\n"
        f"  --accent-hover: {acc_hover};\n"
        f"}}\n"
        f"body {{ background: var(--bg-dark) !important; }}\n"
        f".fixed-bg {{ background: var(--bg-gradient) !important; }}\n"
        f".fixed-bg:before {{\n"
        f"  background:\n"
        f"    radial-gradient(circle at 18% 26%, {_rgba(r1, 0.62)} 0%, transparent 36%),\n"
        f"    radial-gradient(circle at 82% 16%, {_rgba(r2, 0.44)} 0%, transparent 40%),\n"
        f"    radial-gradient(circle at 50% 76%, {_rgba(r3, 0.58)} 0%, transparent 42%) !important;\n"
        f"  background-size: 220% 220% !important;\n"
        f"  animation: shift {int(speed)}s ease infinite !important;\n"
        f"  will-change: background-position;\n"
        f"}}\n"
        f"@keyframes shift {{0%,100%{{background-position:0% 50%}}50%{{background-position:100% 50%}}}}"
    )
    return css


def _apply_theme_override_to_file(path: str, css: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception:
        return False

    block = f"<style id=\"site-theme-override\">\n{css}\n</style>"
    if "<style id=\"site-theme-override\">" in src:
        dst, n = re.subn(r"(?is)<style\s+id=\"site-theme-override\">.*?</style>", block, src, count=1)
        if n <= 0:
            return False
    elif "</head>" in src:
        dst = src.replace("</head>", block + "\n\n</head>", 1)
    else:
        return False

    if dst == src:
        return False
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(dst)
        return True
    except Exception:
        return False


def _apply_site_theme_to_landing() -> dict[str, Any]:
    bg = _sanitize_hex_color((os.environ.get("SITE_BG_COLOR") or "").strip(), "#12070c")
    anim = _sanitize_bg_animation((os.environ.get("SITE_BG_ANIMATION") or "").strip())
    speed = _sanitize_bg_speed((os.environ.get("SITE_BG_ANIMATION_SPEED") or "").strip(), 34)
    accent = _sanitize_hex_color((os.environ.get("SITE_ACCENT_COLOR") or "").strip(), "#b63a5a")
    css = _build_theme_override_css(bg, anim, speed, accent)

    changed = 0
    scanned = 0
    for root, _dirs, files in os.walk(LANDING_DIR):
        for name in files:
            if not name.endswith(".html"):
                continue
            path = os.path.join(root, name)
            scanned += 1
            if _apply_theme_override_to_file(path, css):
                changed += 1

    return {"scanned": scanned, "changed": changed, "bg": bg, "animation": anim, "speed": speed, "accent": accent}

_SUPPORTED_SWITCHER_LANGS = ("en", "ru", "es", "de", "fr")


def _normalize_enabled_languages(raw: str | None) -> list[str]:
    tokens = re.split(r"[,;|\s]+", (raw or "").strip())
    out: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        x = (t or "").strip().lower()
        if x not in _SUPPORTED_SWITCHER_LANGS:
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    if "en" not in seen:
        out.insert(0, "en")
    return out or ["en", "ru", "es", "de", "fr"]


def _apply_enabled_languages_to_landing() -> dict[str, Any]:
    langs = _normalize_enabled_languages((os.environ.get("SITE_ENABLED_LANGS") or "").strip())
    js_path = os.path.join(LANDING_DIR, "i18n-switcher.js")
    if not os.path.exists(js_path):
        return {"ok": False, "error": f"switcher not found: {js_path}"}

    try:
        with open(js_path, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    new_supported = 'var supported = [' + ', '.join([f'"{x}"' for x in langs]) + '];'
    src2, n = re.subn(r"var\s+supported\s*=\s*\[[^\]]*\];", new_supported, src, count=1)
    if n == 0:
        return {"ok": False, "error": "supported array not found in i18n-switcher.js"}

    try:
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(src2)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True, "languages": langs, "path": js_path}



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
    out["LINKEDIN_REDIRECT_URI"] = pick("LINKEDIN_REDIRECT_URI") or (_site_origin() + "/factory/linkedin/callback")
    out["LINKEDIN_PERSON_URN"] = pick("LINKEDIN_PERSON_URN", "LI_PERSON_URN")
    out["LINKEDIN_ORG_URN"] = pick("LINKEDIN_ORG_URN")
    out["LINKEDIN_AUTHOR_BIO"] = pick("LINKEDIN_AUTHOR_BIO", "LI_AUTHOR_BIO")
    out["TELEGRAM_BOT_TOKEN"] = pick("TELEGRAM_BOT_TOKEN")
    out["TELEGRAM_CHAT_ID"] = pick("TELEGRAM_CHAT_ID")
    out["TWITTER_BEARER_TOKEN"] = pick("TWITTER_BEARER_TOKEN", "X_BEARER_TOKEN")
    out["TWITTER_API_KEY"] = pick("TWITTER_API_KEY", "X_API_KEY", "TWITTER_CONSUMER_KEY")
    out["TWITTER_API_SECRET"] = pick("TWITTER_API_SECRET", "X_API_SECRET", "TWITTER_CONSUMER_SECRET")
    out["TWITTER_ACCESS_TOKEN"] = pick("TWITTER_ACCESS_TOKEN", "X_ACCESS_TOKEN")
    out["TWITTER_ACCESS_TOKEN_SECRET"] = pick("TWITTER_ACCESS_TOKEN_SECRET", "X_ACCESS_TOKEN_SECRET")
    out["GEMINI_API_KEY"] = pick("GEMINI_API_KEY", "GOOGLE_API_KEY")
    out["GEMINI_TEXT_MODEL"] = pick("GEMINI_TEXT_MODEL", "GEMINI_MODEL_TEXT", "GEMINI_MODEL") or "gemini-2.5-flash"
    out["GEMINI_IMAGE_MODEL"] = pick("GEMINI_IMAGE_MODEL", "GEMINI_MODEL_IMAGE") or "gemini-2.5-flash-image"

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


def _strip_html_text(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "").strip()


def _ensure_min_faq(draft: dict[str, Any], topic: str | None = None, min_items: int = 5) -> dict[str, Any]:
    if not isinstance(draft, dict):
        return draft

    faq = draft.get("faq")
    if not isinstance(faq, list):
        faq = []

    cleaned: list[dict[str, str]] = []
    for it in faq:
        if not isinstance(it, dict):
            continue
        q = str(it.get("question") or "").strip()
        a = str(it.get("answer") or "").strip()
        if q and a:
            cleaned.append({"question": q, "answer": a})

    if len(cleaned) >= min_items:
        draft["faq"] = cleaned
        return draft

    html = str(draft.get("contentHtml") or "")
    title = str(draft.get("title") or topic or "this topic").strip()

    q_pool: list[str] = []
    for tag in ("h2", "h3"):
        for m in re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", html, flags=re.IGNORECASE | re.DOTALL):
            t = _strip_html_text(m)
            t = re.sub(r"\s+", " ", t).strip()
            if not t:
                continue
            if not t.endswith("?"):
                t = t.rstrip(".:") + "?"
            if len(t) < 10:
                continue
            if t not in q_pool:
                q_pool.append(t)

    defaults = [
        f"What is the quickest way to implement {title}?",
        f"Which mistakes should you avoid when applying {title}?",
        f"How much does it cost to run {title} effectively?",
        f"How long does it take to see results from {title}?",
        f"Which metrics should you track for {title}?",
        f"Can beginners execute {title} without a big team?",
        f"What tools are required to scale {title}?",
    ]

    for q in defaults:
        if q not in q_pool:
            q_pool.append(q)

    text = re.sub(r"\s+", " ", _strip_html_text(html))
    short = text[:220].strip()
    if not short:
        short = "Use a structured plan, prioritize high-impact actions first, and iterate with measurable checkpoints."

    used = {x["question"] for x in cleaned}
    for q in q_pool:
        if len(cleaned) >= min_items:
            break
        if q in used:
            continue
        a = f"Short answer: {short} Focus on practical execution, measurable KPIs, and consistent iteration in 2026."
        cleaned.append({"question": q, "answer": a})
        used.add(q)

    draft["faq"] = cleaned
    return draft


def _extract_first_sentence(text: str, max_chars: int = 220) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return "Short answer."
    m = re.search(r"^(.+?[\.!?])(?:\s|$)", t)
    if m:
        ans = m.group(1).strip()
    else:
        ans = t[:max_chars].strip()
    if len(ans) > max_chars:
        ans = ans[:max_chars].rstrip()
    return ans or "Short answer."


def _ensure_strong_lead_paragraph(html_text: str) -> tuple[str, int]:
    if not html_text:
        return html_text, 0

    changed = 0
    m_h2 = re.search(r"<h2", html_text, flags=re.IGNORECASE)
    head = html_text if not m_h2 else html_text[: m_h2.start()]
    tail = "" if not m_h2 else html_text[m_h2.start():]

    m_p = re.search(r"<p([^>]*)>\s*(.*?)</p>", head, flags=re.IGNORECASE | re.DOTALL)
    if m_p:
        inner = (m_p.group(2) or "").lstrip()
        if not re.match(r"<strong>\s*.+?</strong>", inner, flags=re.IGNORECASE | re.DOTALL):
            plain = _strip_html_text(inner)
            answer = html.escape(_extract_first_sentence(plain))
            repl = f"<p{m_p.group(1)}><strong>{answer}</strong> " + inner + "</p>"
            head = head[:m_p.start()] + repl + head[m_p.end():]
            changed += 1

    return head + tail, changed


def _autofix_answer_first(html_text: str) -> tuple[str, int]:
    if not html_text:
        return html_text, 0

    total = 0
    html_text, c = _ensure_strong_lead_paragraph(html_text)
    total += c

    parts = re.split(r"(<h[23][^>]*>.*?</h[23]>)", html_text, flags=re.IGNORECASE | re.DOTALL)
    if len(parts) < 3:
        return html_text, total

    for i in range(1, len(parts), 2):
        heading_html = parts[i]
        after = parts[i + 1] if (i + 1) < len(parts) else ""

        m_p = re.search(r"<p([^>]*)>\s*(.*?)</p>", after, flags=re.IGNORECASE | re.DOTALL)
        if m_p:
            inner = (m_p.group(2) or "").lstrip()
            if not re.match(r"<strong>\s*.+?</strong>", inner, flags=re.IGNORECASE | re.DOTALL):
                plain = _strip_html_text(inner)
                answer = html.escape(_extract_first_sentence(plain))
                repl = f"<p{m_p.group(1)}><strong>{answer}</strong> " + inner + "</p>"
                after = after[:m_p.start()] + repl + after[m_p.end():]
                parts[i + 1] = after
                total += 1
            continue

        htxt = _strip_html_text(heading_html).strip()
        if htxt.endswith("?"):
            htxt = htxt[:-1].strip()
        seed = _extract_first_sentence(htxt or "Short answer")
        lead = f"<p><strong>{html.escape(seed)}.</strong></p>"
        parts[i + 1] = lead + after
        total += 1

    return "".join(parts), total


@app.on_event("startup")
def _startup() -> None:
    _load_dotenv(os.path.join(APP_DIR, ".env"))

    # Recompute paths after .env load (LANDING_DIR may come from .env).
    global LANDING_DIR, BLOG_DIR, SITEMAP_PATH
    LANDING_DIR = os.environ.get("LANDING_DIR", LANDING_DIR)
    BLOG_DIR = os.path.join(LANDING_DIR, "blog")
    SITEMAP_PATH = os.path.join(LANDING_DIR, "sitemap-en.xml")
    try:
        os.makedirs(BLOG_DIR, exist_ok=True)
    except Exception:
        pass

    db_init(DB_PATH)
    # Mark stale async states only on startup (not during UI polling)
    _mark_stale_social_postings(max_age_min=30)
    _mark_stale_generating_jobs(max_age_min=45)
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
    # Keep UI responsive: clear stale async statuses on polling.
    try:
        _mark_stale_social_postings(max_age_min=12)
        _mark_stale_generating_jobs(max_age_min=60)
    except Exception:
        pass

    with db_connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, topic, slug, status, title, description, category, hero_image,
                   draft_html, faq_json, error, sources_json, visibility, created_at, updated_at, published_url,
                   linkedin_status, linkedin_post_url, linkedin_posted_at, linkedin_error,
                   telegram_status, telegram_post_url, telegram_posted_at, telegram_error,
                   twitter_status, twitter_post_url, twitter_posted_at, twitter_error,
                   product_mode
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
                "productMode": bool(r[28]),
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
    category = _canonical_wine_category((body.get("category") or "").strip(), fallback="") or None
    hero_image = (body.get("heroImage") or "").strip() or None
    visibility = (body.get("visibility") or "public").strip().lower()
    if visibility not in ("public", "hidden"):
        raise HTTPException(status_code=400, detail="visibility must be public|hidden")

    # slug can be empty; generate later
    slug = (body.get("slug") or "").strip() or None
    product_mode = bool(body.get("productMode", False))

    job_id = secrets.token_hex(12)
    now = utcnow_iso()

    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, topic, slug, status, category, hero_image, visibility, product_mode, created_at, updated_at)
            VALUES (?, ?, ?, 'NEW', ?, ?, ?, ?, ?, ?)
            """,
            (job_id, topic, slug, category, hero_image, visibility, 1 if product_mode else 0, now, now),
        )

    log_event(DB_PATH, job_id, "NEW", "Job created")
    return {"success": True, "id": job_id}




@app.post("/api/topics/discover")
async def api_topics_discover(request: Request):
    body = await request.json()
    direction = (body.get("direction") or body.get("topic") or "").strip()
    if len(direction) < 3:
        raise HTTPException(status_code=400, detail="Direction must be at least 3 characters")

    category_hint = _canonical_wine_category((body.get("categoryHint") or body.get("category") or "").strip(), fallback="") or None

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


def _td_read_settings() -> dict[str, Any]:
    with db_connect(DB_PATH) as conn:
        r = conn.execute(
            """
            SELECT enabled, timezone, run_hour, direction, category_hint, per_run_limit, min_score, top_n, last_run_key, last_run_at
            FROM topic_discovery_settings
            WHERE id=1
            """
        ).fetchone()
    if not r:
        return {
            "enabled": False,
            "timezone": "UTC",
            "runHour": 6,
            "direction": "",
            "categoryHint": "",
            "perRunLimit": 15,
            "minScore": 55.0,
            "topN": 3,
            "lastRunKey": None,
            "lastRunAt": None,
        }
    return {
        "enabled": bool(r[0]),
        "timezone": (r[1] or "UTC").strip() or "UTC",
        "runHour": int(r[2] if r[2] is not None else 6),
        "direction": (r[3] or "").strip(),
        "categoryHint": (r[4] or "").strip(),
        "perRunLimit": int(r[5] if r[5] is not None else 15),
        "minScore": float(r[6] if r[6] is not None else 55.0),
        "topN": int(r[7] if r[7] is not None else 3),
        "lastRunKey": r[8],
        "lastRunAt": r[9],
    }


def _td_write_settings(
    *,
    enabled: bool,
    timezone_name: str,
    run_hour: int,
    direction: str,
    category_hint: str,
    per_run_limit: int,
    min_score: float,
    top_n: int,
    last_run_key: str | None = None,
    last_run_at: str | None = None,
) -> None:
    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE topic_discovery_settings
            SET enabled=?, timezone=?, run_hour=?, direction=?, category_hint=?,
                per_run_limit=?, min_score=?, top_n=?,
                last_run_key=COALESCE(?, last_run_key),
                last_run_at=COALESCE(?, last_run_at),
                updated_at=?
            WHERE id=1
            """,
            (
                1 if enabled else 0,
                timezone_name,
                run_hour,
                direction,
                category_hint,
                per_run_limit,
                min_score,
                top_n,
                last_run_key,
                last_run_at,
                utcnow_iso(),
            ),
        )


def _td_log_run(started_at: str, finished_at: str, trigger: str, direction: str, status: str, found_count: int, queued_count: int, result: dict[str, Any]) -> None:
    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO topic_discovery_runs (started_at, finished_at, trigger, direction, status, found_count, queued_count, result_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (started_at, finished_at, trigger, direction, status, int(found_count), int(queued_count), json.dumps(result, ensure_ascii=False)),
        )


def _topic_key(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", (s or "").lower())).strip()


def _topic_is_queueable(topic: str) -> bool:
    t = (topic or "").strip()
    if len(t) < 14 or len(t) > 95:
        return False
    lo = t.lower()
    banned = (
        "frankly shocking",
        "what kind of business model",
        "don't pay for the upgrade",
        "later addressed",
        "reversed course",
        "this isn't a",
        "nano banana",
        "banano",
        "claude best",
    )
    if any(b in lo for b in banned):
        return False
    if t.count(".") > 1 or t.count("!") > 1 or t.count("?") > 1:
        return False
    if "$" in t and len(t) > 70:
        return False
    return True


def _run_topic_autodiscovery(trigger: str = "manual", override: dict[str, Any] | None = None) -> dict[str, Any]:
    if not _TOPIC_DISCOVERY_LOCK.acquire(blocking=False):
        return {"success": False, "status": "BUSY", "message": "topic discovery already running"}

    started = utcnow_iso()
    try:
        base = _td_read_settings()
        cfg = dict(base)
        if isinstance(override, dict):
            cfg.update({k: v for k, v in override.items() if v is not None})

        direction = str(cfg.get("direction") or "").strip()
        if len(direction) < 3:
            direction = _rotate_discovery_direction()

        category_hint = str(cfg.get("categoryHint") or "").strip() or None
        per_run_limit = max(5, min(30, int(cfg.get("perRunLimit") or 15)))
        min_score = float(cfg.get("minScore") if cfg.get("minScore") is not None else 55.0)
        top_n = max(1, min(12, int(cfg.get("topN") or 3)))

        data = discover_topics(direction=direction, limit=per_run_limit, category_hint=category_hint)
        items = list(data.get("items") or [])

        # Filter by score.
        scored = []
        for it in items:
            try:
                sc = float(it.get("score") if it.get("score") is not None else 0)
            except Exception:
                sc = 0.0
            if sc >= min_score and (it.get("topic") or "").strip():
                scored.append((sc, it))
        scored.sort(key=lambda x: x[0], reverse=True)

        with db_connect(DB_PATH) as conn:
            rows = conn.execute("SELECT topic FROM jobs").fetchall()
            existing_topic_keys = {_topic_key(r[0] or "") for r in rows}

        queued = 0
        queued_topics: list[str] = []
        skipped_duplicates = 0
        skipped_unqueueable = 0

        # Deduplicate/validate first, then take top N queue additions.
        for _, it in scored:
            if queued >= top_n:
                break

            topic = (it.get("topic") or "").strip()
            if not topic:
                continue
            if not _topic_is_queueable(topic):
                skipped_unqueueable += 1
                continue
            tk = _topic_key(topic)
            if not tk or tk in existing_topic_keys:
                skipped_duplicates += 1
                continue
            existing_topic_keys.add(tk)

            category = (it.get("category") or category_hint or "").strip() or None
            slug = (it.get("topic") or "").strip().lower()
            slug = re.sub(r"[^a-z0-9\s-]", "", slug)
            slug = re.sub(r"\s+", "-", slug).strip("-")
            slug = slug[:120] if slug else None
            now = utcnow_iso()
            job_id = secrets.token_hex(12)

            with db_connect(DB_PATH) as conn:
                conn.execute(
                    """
                    INSERT INTO jobs (id, topic, slug, status, category, visibility, product_mode, created_at, updated_at)
                    VALUES (?, ?, ?, 'NEW', ?, 'public', 0, ?, ?)
                    """,
                    (job_id, topic, slug, category, now, now),
                )
            log_event(DB_PATH, job_id, "NEW", "Job created by topic autodiscovery")
            queued += 1
            queued_topics.append(topic)

        # Second synthetic fallback in app.py is disabled intentionally.
        # Synthetic variants are now produced only inside factory/discovery.py.
        synthetic_added = 0

        result = {
            "success": True,
            "status": "DONE",
            "direction": direction,
            "foundCount": len(items),
            "eligibleCount": len(scored),
            "queuedCount": queued,
            "queuedTopics": queued_topics,
            "skippedDuplicates": skipped_duplicates,
            "skippedUnqueueable": skipped_unqueueable,
            "syntheticAdded": synthetic_added,
        }
        _td_log_run(started, utcnow_iso(), trigger, direction, "DONE", len(items), queued, result)
        return result
    except Exception as e:
        result = {"success": False, "status": "ERROR", "message": str(e)}
        _td_log_run(started, utcnow_iso(), trigger, str((override or {}).get("direction") or ""), "ERROR", 0, 0, result)
        return result
    finally:
        _TOPIC_DISCOVERY_LOCK.release()


@app.get("/api/topics/autodiscovery/settings")
def topic_autodiscovery_get_settings():
    _autopublish_start_scheduler()
    return {"success": True, **_td_read_settings()}


@app.put("/api/topics/autodiscovery/settings")
async def topic_autodiscovery_set_settings(request: Request):
    _autopublish_start_scheduler()
    body = await request.json()

    enabled = bool(body.get("enabled", False))
    timezone_name = (body.get("timezone") or "UTC").strip() or "UTC"
    try:
        run_hour = int(body.get("runHour") if body.get("runHour") is not None else 6)
    except Exception:
        run_hour = 6
    run_hour = max(0, min(23, run_hour))

    direction = (body.get("direction") or "").strip()
    if enabled and len(direction) < 3:
        direction = _rotate_discovery_direction()

    category_hint = _canonical_wine_category((body.get("categoryHint") or "").strip(), fallback="")
    try:
        per_run_limit = int(body.get("perRunLimit") if body.get("perRunLimit") is not None else 15)
    except Exception:
        per_run_limit = 15
    per_run_limit = max(5, min(30, per_run_limit))

    try:
        min_score = float(body.get("minScore") if body.get("minScore") is not None else 55.0)
    except Exception:
        min_score = 55.0
    min_score = max(0.0, min(100.0, min_score))

    try:
        top_n = int(body.get("topN") if body.get("topN") is not None else 3)
    except Exception:
        top_n = 3
    top_n = max(1, min(12, top_n))

    st = _td_read_settings()
    _td_write_settings(
        enabled=enabled,
        timezone_name=timezone_name,
        run_hour=run_hour,
        direction=direction,
        category_hint=category_hint,
        per_run_limit=per_run_limit,
        min_score=min_score,
        top_n=top_n,
        last_run_key=st.get("lastRunKey"),
        last_run_at=st.get("lastRunAt"),
    )
    return {"success": True}


@app.post("/api/topics/autodiscovery/run")
async def topic_autodiscovery_run(request: Request):
    _autopublish_start_scheduler()
    body = await request.json()
    override = {
        "direction": (body.get("direction") or "").strip() if isinstance(body, dict) else "",
        "categoryHint": (body.get("categoryHint") or "").strip() if isinstance(body, dict) else "",
        "perRunLimit": body.get("perRunLimit") if isinstance(body, dict) else None,
        "minScore": body.get("minScore") if isinstance(body, dict) else None,
        "topN": body.get("topN") if isinstance(body, dict) else None,
    }
    # keep persisted config, override only explicitly passed fields
    override = {k: v for k, v in override.items() if v not in (None, "")}
    out = _run_topic_autodiscovery(trigger="manual", override=override)
    if not out.get("success"):
        raise HTTPException(status_code=400, detail=out.get("message") or "autodiscovery failed")
    return out


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
            "SELECT id, topic, slug, status, category, hero_image, draft_html, product_mode FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    _id, topic, slug, status, category, hero_image, draft_html, product_mode = job

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
                product_mode=bool(product_mode),
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
            log_event(DB_PATH, job_id, "INFO", f"Auto-fit meta description length: {len(before_desc)} -> {len(draft['description'])}")

        # Hard site isolation: never keep myugc absolute links in non-myugc tenants.
        try:
            origin = _site_origin().rstrip("/")
            if origin:
                if isinstance(draft.get("contentHtml"), str):
                    draft["contentHtml"] = re.sub(r"https?://myugc\.studio", origin, draft.get("contentHtml") or "", flags=re.IGNORECASE)
                if isinstance(draft.get("sources"), list):
                    fixed_sources = []
                    for it in draft.get("sources"):
                        if isinstance(it, dict):
                            u = str(it.get("url") or "")
                            if u:
                                it["url"] = re.sub(r"https?://myugc\.studio", origin, u, flags=re.IGNORECASE)
                        fixed_sources.append(it)
                    draft["sources"] = fixed_sources
        except Exception:
            pass

        draft = _ensure_min_faq(draft, topic=topic, min_items=5)
        draft["category"] = _pick_category_from_content(topic=topic, title=draft.get("title"), description=draft.get("description"), category_hint=draft.get("category") or category, content_html=draft.get("contentHtml"))
        try:
            fixed_html, fixed_count = _autofix_answer_first(str(draft.get("contentHtml") or ""))
            if fixed_count > 0:
                draft["contentHtml"] = fixed_html
                log_event(DB_PATH, job_id, "INFO", f"Auto-fixed answer-first blocks: {fixed_count}")
        except Exception as _af_err:
            log_event(DB_PATH, job_id, "WARN", f"answer-first auto-fix skipped: {_af_err}")
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
            category=draft.get("category") or category or "Buying Guides",
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
        category=cat or "Buying Guides",
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
    cat = _pick_category_from_content(topic=topic, title=title, description=desc, category_hint=cat, content_html=content_html)

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

    _ensure_sitemap(SITEMAP_PATH)

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
            category=cat or "Buying Guides",
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

    # Build/refresh webp variants before rendering cards/feed.
    _optimize_site_images()

    html = render_post_html(
        blog_dir=BLOG_DIR,
        title=title or "",
        description=desc or "",
        category=cat or "Buying Guides",
        slug=slug,
        hero_image=hero or "logo.png",
        content_html=content_html,
        faq=faq,
        sources=sources,
        updated_at=updated_at or utcnow_iso(),
        noindex=noindex,
    )

    html = _apply_hreflang_block(html, slug, "en")
    out_path = os.path.join(BLOG_DIR, f"{slug}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    url = f"{_site_origin()}/blog/{slug}.html"

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
            category=cat or "Buying Guides",
            hero_image=os.path.basename(hero or "logo.png"),
        )
        upsert_sitemap_url(SITEMAP_PATH, url=url)

    _rebuild_blog_feed_from_index(os.path.join(BLOG_DIR, "index.html"), os.path.join(BLOG_DIR, "feed.json"))

    paths = [os.path.join("blog", f"{slug}.html"), os.path.join("blog", "index.html"), os.path.join("blog", "feed.json"), "sitemap-en.xml"] + (image_paths or [])

    # Publish localized versions (ru/es/de/fr) in the same publish action.
    text_api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    text_model = (
        os.environ.get("GEMINI_TEXT_MODEL")
        or os.environ.get("GEMINI_MODEL_TEXT")
        or os.environ.get("GEMINI_MODEL")
        or "gemini-2.5-flash"
    )
    toc_titles = {
        "ru": "На этой странице",
        "es": "En esta página",
        "de": "Auf dieser Seite",
        "fr": "Sur cette page",
    }
    for loc in LOCALES:
        _ensure_sitemap(_locale_sitemap_path(loc))
        loc_blog_dir = _locale_blog_dir(loc)
        loc_sitemap = _locale_sitemap_path(loc)
        loc_url = f"{_site_origin()}/{loc}/blog/{slug}.html"
        loc_out_rel = os.path.join(loc, "blog", f"{slug}.html")
        loc_idx_rel = os.path.join(loc, "blog", "index.html")
        loc_title = title or ""
        loc_desc = desc or ""
        loc_cat = cat or "Buying Guides"
        loc_content = content_html
        loc_faq = faq
        loc_cat = _localize_category(_pick_category_from_content(topic=topic, title=loc_title, description=loc_desc, category_hint=cat, content_html=loc_content), loc)

        if text_api_key:
            tr = _translate_post_payload(
                api_key=text_api_key,
                model=text_model,
                locale=loc,
                slug=slug,
                title=loc_title,
                description=loc_desc,
                category=loc_cat,
                content_html=loc_content,
                faq=loc_faq,
            )
            loc_title = tr["title"]
            loc_desc = tr["description"]
            loc_cat = _localize_category(_pick_category_from_content(topic=topic, title=loc_title, description=loc_desc, category_hint=tr.get("category"), content_html=loc_content), loc)
            loc_content = tr["contentHtml"]
            loc_faq = tr["faq"]
        else:
            raise HTTPException(status_code=500, detail=f"Localization {loc} failed: no GEMINI_API_KEY/GOOGLE_API_KEY")

        loc_html = render_post_html(
            blog_dir=BLOG_DIR,
            title=loc_title,
            description=loc_desc,
            category=loc_cat,
            slug=slug,
            hero_image=hero or "logo.png",
            content_html=loc_content,
            faq=loc_faq,
            sources=sources,
            updated_at=updated_at or utcnow_iso(),
            noindex=noindex,
            toc_title=toc_titles.get(loc, "On this page"),
        )
        loc_html = re.sub(r'(?is)<html\s+lang="[^"]+"', f'<html lang="{loc}"', loc_html, count=1)
        loc_html = _apply_hreflang_block(loc_html, slug, loc)

        os.makedirs(loc_blog_dir, exist_ok=True)
        with open(os.path.join(loc_blog_dir, f"{slug}.html"), "w", encoding="utf-8") as f:
            f.write(loc_html)

        if noindex:
            remove_blog_index_card(
                loc_blog_dir,
                slug=slug,
                href_prefix=f"/{loc}/blog",
                marker_prefix=f"FACTORY-{loc.upper()}",
            )
            remove_sitemap_url(loc_sitemap, url=loc_url)
        else:
            upsert_blog_index_card(
                loc_blog_dir,
                slug=slug,
                title=loc_title,
                description=loc_desc,
                category=loc_cat,
                hero_image=f"/blog/{os.path.basename(hero or 'logo.png')}",
                href_prefix=f"/{loc}/blog",
                marker_prefix=f"FACTORY-{loc.upper()}",
            )
            upsert_sitemap_url(loc_sitemap, url=loc_url)

        _rebuild_blog_feed_from_index(os.path.join(loc_blog_dir, "index.html"), os.path.join(loc_blog_dir, "feed.json"))
        paths.extend([loc_out_rel, loc_idx_rel, os.path.join(loc, "blog", "feed.json"), f"sitemap-{loc}.xml"])

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
    try:
        origin = _site_origin().rstrip("/")
        candidates = [
            f"{origin}/sitemap_index.xml",
            f"{origin}/sitemap.xml",
            f"{origin}/sitemap-en.xml",
            f"{origin}/sitemap-ru.xml",
            f"{origin}/sitemap-es.xml",
            f"{origin}/sitemap-de.xml",
            f"{origin}/sitemap-fr.xml",
            f"{origin}/sitemap_blog.xml",
        ]

        # Avoid HEAD pre-checks (can be flaky behind CDN/proxy and return empty list).
        sitemap_urls = list(dict.fromkeys([s for s in candidates if s]))

        gsc = _submit_sitemaps_to_search_console(sitemap_urls)
        if gsc.get("success"):
            log_event(DB_PATH, job_id, "INFO", "Search Console sitemap submit: OK")
        else:
            log_event(DB_PATH, job_id, "WARN", f"Search Console sitemap submit failed: {gsc.get('error')}")
    except Exception as e:
        log_event(DB_PATH, job_id, "WARN", f"Search Console sitemap submit error: {e}")

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
                   twitter_status, twitter_post_url, twitter_posted_at, twitter_error,
                   product_mode
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
            "productMode": bool(r[28]),
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
        set_if("category", _canonical_wine_category(body["category"].strip(), fallback="Buying Guides"))

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

    if isinstance(body.get("productMode"), bool):
        set_if("product_mode", 1 if body.get("productMode") else 0)

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
    url = f"{_site_origin()}/blog/{slug}.html"
    remove_paths = [out_rel]
    add_paths = [os.path.join("blog", "index.html"), "sitemap-en.xml"]

    if os.path.exists(out_abs):
        os.remove(out_abs)

    remove_blog_index_card(BLOG_DIR, slug=slug)
    remove_sitemap_url(SITEMAP_PATH, url=url)

    for loc in LOCALES:
        loc_blog_dir = _locale_blog_dir(loc)
        loc_abs = os.path.join(loc_blog_dir, f"{slug}.html")
        loc_rel = os.path.join(loc, "blog", f"{slug}.html")
        loc_url = f"{_site_origin()}/{loc}/blog/{slug}.html"
        if os.path.exists(loc_abs):
            os.remove(loc_abs)
        remove_blog_index_card(
            loc_blog_dir,
            slug=slug,
            href_prefix=f"/{loc}/blog",
            marker_prefix=f"FACTORY-{loc.upper()}",
        )
        remove_sitemap_url(_locale_sitemap_path(loc), url=loc_url)
        remove_paths.append(loc_rel)
        add_paths.extend([os.path.join(loc, "blog", "index.html"), f"sitemap-{loc}.xml"])

    git_commit_push_with_remove(
        repo_dir=LANDING_DIR,
        message=f"Unpublish post: {slug}",
        add_paths=add_paths,
        remove_paths=remove_paths,
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
        url = f"{_site_origin()}/blog/{slug}.html"

        if os.path.exists(out_abs):
            os.remove(out_abs)
            removed_paths.append(out_rel)

        remove_blog_index_card(BLOG_DIR, slug=slug)
        remove_sitemap_url(SITEMAP_PATH, url=url)

        for loc in LOCALES:
            loc_blog_dir = _locale_blog_dir(loc)
            loc_abs = os.path.join(loc_blog_dir, f"{slug}.html")
            loc_rel = os.path.join(loc, "blog", f"{slug}.html")
            loc_url = f"{_site_origin()}/{loc}/blog/{slug}.html"

            if os.path.exists(loc_abs):
                os.remove(loc_abs)
                removed_paths.append(loc_rel)

            remove_blog_index_card(
                loc_blog_dir,
                slug=slug,
                href_prefix=f"/{loc}/blog",
                marker_prefix=f"FACTORY-{loc.upper()}",
            )
            remove_sitemap_url(_locale_sitemap_path(loc), url=loc_url)

    if removed_paths:
        add_paths = [os.path.join("blog", "index.html"), "sitemap-en.xml"]
        for loc in LOCALES:
            add_paths.extend([os.path.join(loc, "blog", "index.html"), f"sitemap-{loc}.xml"])
        git_commit_push_with_remove(
            repo_dir=LANDING_DIR,
            message=f"Delete factory post: {slug}",
            add_paths=add_paths,
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
            SELECT enabled, times_per_day, channels_json, timezone, start_hour, end_hour, linkedin_include_link, telegram_include_link, last_slot_key, last_run_at
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
            "linkedin_include_link": False,
            "telegram_include_link": False,
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
        "linkedin_include_link": bool(r[6]),
        "telegram_include_link": bool(r[7]),
        "last_slot_key": r[8],
        "last_run_at": r[9],
    }


def _ap_write_settings(*, enabled: bool, times_per_day: int, channels: list[str], timezone_name: str, start_hour: int, end_hour: int, linkedin_include_link: bool = False, telegram_include_link: bool = False, last_slot_key: str | None = None, last_run_at: str | None = None) -> None:
    ch_json = json.dumps(channels)
    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE autopublish_settings
            SET enabled=?, times_per_day=?, channels_json=?, timezone=?, start_hour=?, end_hour=?,
                linkedin_include_link=?, telegram_include_link=?,
                last_slot_key=COALESCE(?, last_slot_key),
                last_run_at=COALESCE(?, last_run_at),
                updated_at=?
            WHERE id=1
            """,
            (1 if enabled else 0, times_per_day, ch_json, timezone_name, start_hour, end_hour, 1 if linkedin_include_link else 0, 1 if telegram_include_link else 0, last_slot_key, last_run_at, utcnow_iso()),
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


def _ap_generate_oldest_new_to_ready(max_attempts: int = 5) -> str | None:
    """Try to promote queued NEW jobs into READY by generating oldest first."""
    with db_connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status='NEW' ORDER BY created_at ASC LIMIT ?",
            (max_attempts,),
        ).fetchall()

    for r in rows:
        job_id = str(r[0])
        try:
            gen_out = generate(job_id)
            if isinstance(gen_out, dict) and gen_out.get('success') is False:
                continue
        except Exception:
            continue

        with db_connect(DB_PATH) as conn:
            st = conn.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
        if st and str(st[0] or '').upper().strip() == 'READY':
            return job_id

    return None


def _ap_autofill_from_topic_discovery() -> str | None:
    """When autopublish queue is empty, promote NEW first, then discover->create->generate."""
    # 1) Prefer already queued NEW topics before discovering anything new.
    existing = _ap_generate_oldest_new_to_ready(max_attempts=5)
    if existing:
        return existing

    # 2) If nothing queued, run topic discovery settings and try again.
    try:
        td = _td_read_settings()
    except Exception:
        return None

    if not td.get('enabled'):
        return None

    direction = str(td.get('direction') or '').strip()
    if len(direction) < 3:
        return None

    try:
        _run_topic_autodiscovery(trigger='autopublish')
    except Exception:
        return None

    return _ap_generate_oldest_new_to_ready(max_attempts=8)


def _run_autopublish(trigger: str = "manual") -> dict[str, Any]:
    if not _AUTOPUBLISH_LOCK.acquire(blocking=False):
        if trigger == "schedule":
            started = utcnow_iso()
            result = {"success": False, "status": "BUSY", "message": "autopublish already running"}
            _ap_log_run(started, utcnow_iso(), trigger, None, "BUSY", result)
        return {"success": False, "status": "BUSY", "message": "autopublish already running"}

    started = utcnow_iso()
    try:
        settings = _ap_read_settings()
        channels = settings.get("channels") or []

        if trigger != "manual" and not settings.get("enabled"):
            result = {"success": False, "status": "DISABLED"}
            _ap_log_run(started, utcnow_iso(), trigger, None, "DISABLED", result)
            return result

        if not channels:
            channels = ["linkedin", "telegram", "twitter"]

        with db_connect(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT id, slug, published_url,
                       COALESCE(linkedin_status, ''),
                       COALESCE(telegram_status, ''),
                       COALESCE(twitter_status, '')
                FROM jobs
                WHERE status='READY'
                ORDER BY created_at ASC
                LIMIT 300
                """
            ).fetchall()

        selected = None
        for r in rows:
            jid, slug, published_url, li_st, tg_st, tw_st = r
            st_map = {
                "linkedin": (li_st or "").upper().strip(),
                "telegram": (tg_st or "").upper().strip(),
                "twitter": (tw_st or "").upper().strip(),
            }
            has_unposted_channel = any(st_map.get(ch, "") != "POSTED" for ch in channels)
            if has_unposted_channel:
                selected = (jid, slug, (published_url or "").strip())
                break

        if not selected:
            # Try to fill the queue automatically (topic autodiscovery -> generate 1 draft)
            # so scheduled slots can publish something when possible.
            filled_id = _ap_autofill_from_topic_discovery()
            if filled_id:
                sql = (
                    "SELECT id, slug, published_url, "
                    "COALESCE(linkedin_status, ''), "
                    "COALESCE(telegram_status, ''), "
                    "COALESCE(twitter_status, '') "
                    "FROM jobs WHERE status='READY' ORDER BY created_at ASC LIMIT 300"
                )
                with db_connect(DB_PATH) as conn:
                    rows = conn.execute(sql).fetchall()

                for r in rows:
                    jid, slug, published_url, li_st, tg_st, tw_st = r
                    st_map = {
                        'linkedin': (li_st or '').upper().strip(),
                        'telegram': (tg_st or '').upper().strip(),
                        'twitter': (tw_st or '').upper().strip(),
                    }
                    has_unposted_channel = any(st_map.get(ch, '') != 'POSTED' for ch in channels)
                    if has_unposted_channel:
                        selected = (jid, slug, (published_url or '').strip())
                        break

        if not selected:
            result = {"success": True, "status": "NOOP", "message": "no eligible READY jobs"}
            _ap_log_run(started, utcnow_iso(), trigger, None, "NOOP", result)
            return result

        job_id, slug, published_url = selected
        summary: dict[str, Any] = {"job_id": job_id, "channels": {}, "site_publish": None}

        # 1) publish site first
        try:
            if published_url:
                summary["site_publish"] = {"ok": True, "url": published_url, "skipped": True}
            else:
                out = publish(job_id)
                summary["site_publish"] = {"ok": True, "url": (out or {}).get("url") if isinstance(out, dict) else None}
        except Exception as e:
            msg = f"site publish failed: {e}"
            summary["site_publish"] = {"ok": False, "error": msg}
            _ap_log_run(started, utcnow_iso(), trigger, job_id, "ERROR", summary)
            return {"success": False, "status": "ERROR", **summary}

        # 2) publish socials only for channels not yet POSTED
        with db_connect(DB_PATH) as conn:
            st = conn.execute(
                "SELECT COALESCE(linkedin_status,''), COALESCE(telegram_status,''), COALESCE(twitter_status,'') FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        st_map = {
            "linkedin": (st[0] if st else "").upper().strip(),
            "telegram": (st[1] if st else "").upper().strip(),
            "twitter": (st[2] if st else "").upper().strip(),
        }

        for ch in channels:
            if st_map.get(ch, "") == "POSTED":
                summary["channels"][ch] = {"ok": True, "error": None, "url": None, "skipped": True}
                continue
            try:
                if ch == "linkedin":
                    linkedin_publish(job_id, {"includeLink": bool(settings.get("linkedin_include_link"))})
                elif ch == "telegram":
                    telegram_publish(job_id, {"includeLink": bool(settings.get("telegram_include_link"))})
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
                            linkedin_include_link=bool(st.get("linkedin_include_link")),
                            telegram_include_link=bool(st.get("telegram_include_link")),
                            last_slot_key=key,
                            last_run_at=utcnow_iso(),
                        )

            # Daily topic autodiscovery (uses same scheduler thread)
            td = _td_read_settings()
            if td.get("enabled"):
                now_local = _ap_now_local(td.get("timezone") or "UTC")
                run_hour = max(0, min(23, int(td.get("runHour") if td.get("runHour") is not None else 6)))
                if now_local.hour == run_hour and now_local.minute < 10:
                    key = f"{now_local.date().isoformat()}-{run_hour:02d}"
                    if key != (td.get("lastRunKey") or ""):
                        out = _run_topic_autodiscovery(trigger="schedule")
                        _td_write_settings(
                            enabled=bool(td.get("enabled")),
                            timezone_name=(td.get("timezone") or "UTC"),
                            run_hour=run_hour,
                            direction=str(td.get("direction") or ""),
                            category_hint=str(td.get("categoryHint") or ""),
                            per_run_limit=int(td.get("perRunLimit") or 15),
                            min_score=float(td.get("minScore") if td.get("minScore") is not None else 55.0),
                            top_n=int(td.get("topN") or 3),
                            last_run_key=key,
                            last_run_at=utcnow_iso() if out.get("success") else td.get("lastRunAt"),
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
    _autopublish_start_scheduler()
    st = _ap_read_settings()
    slots = _ap_slots(st.get("times_per_day") or 3, st.get("start_hour") or 9, st.get("end_hour") or 21)
    return {"success": True, **st, "slots": slots}


@app.get("/api/autopublish/health")
def autopublish_health():
    _autopublish_start_scheduler()
    st = _ap_read_settings()
    now_local = _ap_now_local(st.get("timezone") or "UTC")
    slots = _ap_slots(st.get("times_per_day") or 3, st.get("start_hour") or 9, st.get("end_hour") or 21)
    alive = bool(_AUTOPUBLISH_THREAD and _AUTOPUBLISH_THREAD.is_alive())
    return {
        "success": True,
        "threadAlive": alive,
        "nowLocal": now_local.isoformat(),
        "slots": slots,
        **st,
    }


@app.put("/api/autopublish/settings")
async def autopublish_set_settings(request: Request):
    _autopublish_start_scheduler()
    body = await request.json()

    enabled = bool(body.get("enabled", False))
    times_per_day = int(body.get("timesPerDay") or body.get("times_per_day") or 3)
    times_per_day = max(1, min(8, times_per_day))

    channels = body.get("channels") or ["linkedin", "telegram", "twitter"]
    if not isinstance(channels, list):
        raise HTTPException(status_code=400, detail="channels must be list")
    channels = [str(x).strip().lower() for x in channels if str(x).strip().lower() in ("linkedin", "telegram", "twitter")]

    timezone_name = (body.get("timezone") or "UTC").strip() or "UTC"
    linkedin_include_link = bool(body.get("linkedinIncludeLink", body.get("linkedin_include_link", False)))
    telegram_include_link = bool(body.get("telegramIncludeLink", body.get("telegram_include_link", False)))
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
        linkedin_include_link=linkedin_include_link,
        telegram_include_link=telegram_include_link,
        last_slot_key=st.get("last_slot_key"),
        last_run_at=st.get("last_run_at"),
    )

    return {"success": True}


@app.post("/api/autopublish/run")
def autopublish_run_now():
    _autopublish_start_scheduler()
    out = _run_autopublish(trigger="manual")
    return {"success": True, "result": out}


@app.get("/api/autopublish/runs")
def autopublish_runs(limit: int = 20):
    _autopublish_start_scheduler()
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
        if key == "LINKEDIN_ORG_URN" and val:
            try:
                val = _normalize_linkedin_org_urn(val)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        if val:
            updates[key] = val
        else:
            clears.add(key)

    # Aliases kept in sync for compatibility with older env naming.
    if "LINKEDIN_CLIENT_ID" in updates:
        updates["LI_CLIENT_ID"] = updates["LINKEDIN_CLIENT_ID"]
    if "LINKEDIN_CLIENT_SECRET" in updates:
        updates["LI_CLIENT_SECRET"] = updates["LINKEDIN_CLIENT_SECRET"]
    if "LINKEDIN_PERSON_URN" in updates:
        updates["LI_PERSON_URN"] = updates["LINKEDIN_PERSON_URN"]
    if "LINKEDIN_AUTHOR_BIO" in updates:
        updates["LI_AUTHOR_BIO"] = updates["LINKEDIN_AUTHOR_BIO"]
    if "TWITTER_BEARER_TOKEN" in updates:
        updates["X_BEARER_TOKEN"] = updates["TWITTER_BEARER_TOKEN"]
    if "TWITTER_API_KEY" in updates:
        updates["X_API_KEY"] = updates["TWITTER_API_KEY"]
        updates["TWITTER_CONSUMER_KEY"] = updates["TWITTER_API_KEY"]
    if "TWITTER_API_SECRET" in updates:
        updates["X_API_SECRET"] = updates["TWITTER_API_SECRET"]
        updates["TWITTER_CONSUMER_SECRET"] = updates["TWITTER_API_SECRET"]
    if "TWITTER_ACCESS_TOKEN" in updates:
        updates["X_ACCESS_TOKEN"] = updates["TWITTER_ACCESS_TOKEN"]
    if "TWITTER_ACCESS_TOKEN_SECRET" in updates:
        updates["X_ACCESS_TOKEN_SECRET"] = updates["TWITTER_ACCESS_TOKEN_SECRET"]
    if "GEMINI_API_KEY" in updates:
        updates["GOOGLE_API_KEY"] = updates["GEMINI_API_KEY"]
    if "GEMINI_TEXT_MODEL" in updates:
        updates["GEMINI_MODEL_TEXT"] = updates["GEMINI_TEXT_MODEL"]
    if "GEMINI_IMAGE_MODEL" in updates:
        updates["GEMINI_MODEL_IMAGE"] = updates["GEMINI_IMAGE_MODEL"]

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
    if "TWITTER_API_KEY" in clears:
        clears.add("X_API_KEY")
        clears.add("TWITTER_CONSUMER_KEY")
    if "TWITTER_API_SECRET" in clears:
        clears.add("X_API_SECRET")
        clears.add("TWITTER_CONSUMER_SECRET")
    if "TWITTER_ACCESS_TOKEN" in clears:
        clears.add("X_ACCESS_TOKEN")
    if "TWITTER_ACCESS_TOKEN_SECRET" in clears:
        clears.add("X_ACCESS_TOKEN_SECRET")
    if "GEMINI_API_KEY" in clears:
        clears.add("GOOGLE_API_KEY")
    if "GEMINI_TEXT_MODEL" in clears:
        clears.add("GEMINI_MODEL_TEXT")
    if "GEMINI_IMAGE_MODEL" in clears:
        clears.add("GEMINI_MODEL_IMAGE")

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

    org_env = (os.environ.get("LINKEDIN_ORG_URN") or "").strip()
    mode = (request.query_params.get('as') or '').strip().lower()
    if mode not in ('member', 'org'):
        mode = 'org' if org_env else 'member'

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

    include_link = bool(payload.get("includeLink"))

    auth = db_get_linkedin(DB_PATH) or {}
    member_urn = (auth.get("member_urn") or "").strip()
    if not member_urn:
        raise HTTPException(status_code=400, detail="LinkedIn not connected")

    org_env = (os.environ.get("LINKEDIN_ORG_URN") or "").strip() or None
    org_urn = org_env or (auth.get("org_urn") or "").strip() or None

    # Posting mode comes from global settings: if org URN configured -> org, else member.
    # Keep payload["as"] only for backward compatibility with older UI clients.
    mode = (payload.get("as") or "").strip().lower()
    if mode not in ("member", "org"):
        mode = "org" if org_urn else "member"
    if mode == "org" and not org_urn:
        mode = "member"

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
    url = (published_url or f"{_site_origin()}/blog/{slug}.html").strip()


    # Use exactly the same image as in article HTML (first local <img src>). No social image generation.
    hero_filename = ""
    if draft_html:
        m_first_img = re.search(r"(?is)<img[^>]+src=[\"']([^\"']+)[\"']", draft_html)
        if m_first_img:
            src = (m_first_img.group(1) or "").strip()
            # Only local blog files; ignore absolute URLs/data URIs
            if src and not src.startswith(("http://", "https://", "data:")):
                src = src.split("?", 1)[0].split("#", 1)[0]
                hero_filename = os.path.basename(src)

    # Fallback to known generated local files if first image is absent/invalid.
    candidates = []
    if hero_filename:
        candidates.append(hero_filename)
    candidates.extend([
        f"{slug}-img-1.png",
        f"{slug}-img-1.jpg",
        f"{slug}-img-1.jpeg",
        f"{slug}-img-2.png",
        f"{slug}-img-3.png",
    ])
    hero_fallback = os.path.basename(hero_image or "")
    if hero_fallback:
        candidates.append(hero_fallback)

    chosen = None
    for name in candidates:
        if not name:
            continue
        abs_path = os.path.join(BLOG_DIR, name)
        if os.path.exists(abs_path):
            chosen = name
            break

    hero_filename = chosen or ""
    hero_abs = os.path.join(BLOG_DIR, hero_filename) if hero_filename else ""
    if not hero_filename or not os.path.exists(hero_abs):
        raise HTTPException(status_code=400, detail="Article image file not found in blog directory. Publish/generate article first.")

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
            sent_text = None
            api_resp = resp
            if isinstance(resp, dict):
                if isinstance(resp.get("api_response"), dict):
                    api_resp = resp.get("api_response")
                    sent_text = (resp.get("sent_text") or "").strip() or None
                post_id = (api_resp or {}).get("id") or (api_resp or {}).get("urn") or (api_resp or {}).get("value")

            with db_connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE jobs SET linkedin_status='POSTED', linkedin_post_url=?, linkedin_posted_at=?, linkedin_error=NULL, updated_at=? WHERE id=?",
                    (post_id, utcnow_iso(), utcnow_iso(), job_id),
                )

            _save_social_post(
                job_id=job_id,
                channel="linkedin",
                content_text=sent_text,
                content_json=api_resp if isinstance(api_resp, dict) else None,
                remote_url=post_id,
                status="POSTED",
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

    include_link = bool(payload.get("includeLink", False))

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

    url = (published_url or f"{_site_origin()}/blog/{slug}.html").strip()

    # Reuse already generated article images only (no social re-generation).
    # Prefer square inline image from article; fallback to hero if needed.
    candidates = [
        f"{slug}-img-1.png",
        f"{slug}-img-1.jpg",
        f"{slug}-img-1.jpeg",
        f"{slug}-img-2.png",
        f"{slug}-img-3.png",
    ]
    hero_filename = os.path.basename(hero_image or "")
    if hero_filename:
        candidates.append(hero_filename)

    chosen = None
    for name in candidates:
        if not name:
            continue
        abs_path = os.path.join(BLOG_DIR, name)
        if os.path.exists(abs_path):
            chosen = name
            break

    hero_filename = chosen or (hero_filename or "")
    hero_abs = os.path.join(BLOG_DIR, hero_filename) if hero_filename and os.path.exists(os.path.join(BLOG_DIR, hero_filename)) else None
    hero_public_url = f"{_site_origin()}/blog/{hero_filename}" if hero_filename else None

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
                hero_public_url=hero_public_url,
            )
            message_id = (((res.get("message") or {}).get("result") or {}).get("message_id"))
            post_url = telegram_message_url(chat_id, message_id)
            sent_text = (res.get("sent_text") or text or "").strip()
            mode = (res.get("mode") or "unknown").strip()

            with db_connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE jobs SET telegram_status='POSTED', telegram_post_url=?, telegram_posted_at=?, telegram_error=NULL, updated_at=? WHERE id=?",
                    (post_url, utcnow_iso(), utcnow_iso(), job_id),
                )

            _save_social_post(
                job_id=job_id,
                channel="telegram",
                content_text=sent_text,
                content_json={"mode": mode, "chat_id": chat_id, "response": res},
                remote_url=post_url,
                status="POSTED",
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
    oauth1 = {
        "api_key": (os.environ.get("TWITTER_API_KEY") or os.environ.get("X_API_KEY") or os.environ.get("TWITTER_CONSUMER_KEY") or "").strip(),
        "api_secret": (os.environ.get("TWITTER_API_SECRET") or os.environ.get("X_API_SECRET") or os.environ.get("TWITTER_CONSUMER_SECRET") or "").strip(),
        "access_token": (os.environ.get("TWITTER_ACCESS_TOKEN") or os.environ.get("X_ACCESS_TOKEN") or "").strip(),
        "access_token_secret": (os.environ.get("TWITTER_ACCESS_TOKEN_SECRET") or os.environ.get("X_ACCESS_TOKEN_SECRET") or "").strip(),
    }
    has_oauth1 = all(oauth1.values())
    if not access_token and not has_oauth1:
        raise HTTPException(status_code=500, detail="Missing X credentials: set TWITTER_BEARER_TOKEN (OAuth2) or OAuth1 keys")

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

    url = (published_url or f"{_site_origin()}/blog/{slug}.html").strip()

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
            media_urls = extract_article_image_urls_for_x(content_html=draft_html or "", page_url=url, max_images=4)
            out = twitter_post_thread(access_token=access_token, tweets=tweets, oauth1=(oauth1 if has_oauth1 else None), media_urls=media_urls)
            post_url = out.get("thread_url")

            with db_connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE jobs SET twitter_status='POSTED', twitter_post_url=?, twitter_posted_at=?, twitter_error=NULL, updated_at=? WHERE id=?",
                    (post_url, utcnow_iso(), utcnow_iso(), job_id),
                )

            _save_social_post(
                job_id=job_id,
                channel="twitter",
                content_text="\n\n---\n\n".join(tweets),
                content_json={"tweets": tweets, "response": out, "media_urls": media_urls},
                remote_url=post_url,
                status="POSTED",
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


@app.get("/api/settings/site")
def settings_site_get():
    values = _env_file_values(ENV_PATH)

    def pick(key: str) -> str:
        return (values.get(key) or os.environ.get(key) or "").strip()

    out = {k: pick(k) for k in SITE_ENV_KEYS}
    return {"success": True, "values": out}


@app.put("/api/settings/site")
async def settings_site_put(request: Request):
    body = await request.json()
    values = body.get("values") or {}
    if not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="values must be object")

    updates: dict[str, str] = {}
    for k, v in values.items():
        key = str(k or "").strip()
        if key not in SITE_ENV_KEYS:
            continue
        updates[key] = str(v or "").strip()

    _env_write_updates(ENV_PATH, updates, set())
    for k, v in updates.items():
        os.environ[k] = v

    theme_result = None
    if any(k in updates for k in ("SITE_BG_COLOR", "SITE_BG_ANIMATION", "SITE_BG_ANIMATION_SPEED", "SITE_ACCENT_COLOR")):
        theme_result = _apply_site_theme_to_landing()

    pulse_result = None
    if any(k in updates for k in ("SITE_CONTEXT", "SITE_SUBTOPICS")):
        pulse_result = _apply_pulse_profile_to_landing()

    langs_result = None
    if "SITE_ENABLED_LANGS" in updates:
        langs_csv = ",".join(_normalize_enabled_languages(updates.get("SITE_ENABLED_LANGS", "")))
        updates["SITE_ENABLED_LANGS"] = langs_csv
        os.environ["SITE_ENABLED_LANGS"] = langs_csv
        _env_write_updates(ENV_PATH, {"SITE_ENABLED_LANGS": langs_csv}, set())
        langs_result = _apply_enabled_languages_to_landing()

    out = {"success": True, "values": {k: (os.environ.get(k) or "").strip() for k in SITE_ENV_KEYS}}
    if theme_result is not None:
        out["theme_apply"] = theme_result
    if langs_result is not None:
        out["languages_apply"] = langs_result
    if pulse_result is not None:
        out["pulse_apply"] = pulse_result
    return out
