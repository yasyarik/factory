import os
import json
import time
import difflib
import sqlite3
import secrets
import re
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urlparse
import urllib.request
import urllib.error
import urllib.parse
import html as html_lib
import base64
import hashlib
import hmac
import crypt

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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
from factory.tumblr import (
    tumblr_build_auth_url,
    tumblr_request_token,
    tumblr_exchange_access_token,
    tumblr_publish_text_post,
    db_get_tumblr,
    db_set_tumblr,
    db_clear_tumblr,
    db_put_tumblr_temp,
    db_pop_tumblr_temp,
    build_tumblr_post_html,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(APP_DIR, "templates")
DB_PATH = os.path.join(APP_DIR, "factory.sqlite")
ENV_PATH = os.path.join(APP_DIR, ".env")

LANDING_DIR = os.environ.get("LANDING_DIR", "/var/www/yaswine")
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


FACTORY_SESSION_COOKIE = "factory_session"
FACTORY_SESSION_TTL_SECONDS = int((os.environ.get("FACTORY_SESSION_TTL_SECONDS") or "43200").strip())
FACTORY_HTPASSWD_FILE = os.environ.get("FACTORY_HTPASSWD_FILE", "/etc/nginx/.htpasswd_factory")


def _factory_secret() -> bytes:
    raw = (os.environ.get("FACTORY_SESSION_SECRET") or os.environ.get("FACTORY_COOKIE_SECRET") or "yaswine-factory-session-secret").strip()
    return raw.encode("utf-8")


def _factory_b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _factory_b64url_decode(data: str) -> bytes:
    pad = '=' * ((4 - (len(data) % 4)) % 4)
    return base64.urlsafe_b64decode((data + pad).encode("ascii"))


def _factory_sign(payload: str) -> str:
    sig = hmac.new(_factory_secret(), payload.encode("utf-8"), hashlib.sha256).digest()
    return _factory_b64url(sig)


def _factory_issue_token(username: str) -> str:
    exp = int(time.time()) + max(600, FACTORY_SESSION_TTL_SECONDS)
    nonce = secrets.token_urlsafe(8)
    payload = f"{username}:{exp}:{nonce}"
    return f"{_factory_b64url(payload.encode('utf-8'))}.{_factory_sign(payload)}"


def _factory_verify_token(token: str | None) -> tuple[bool, str | None]:
    if not token or '.' not in token:
        return (False, None)
    p64, sig = token.split('.', 1)
    try:
        payload = _factory_b64url_decode(p64).decode('utf-8', 'ignore')
    except Exception:
        return (False, None)
    expected = _factory_sign(payload)
    if not hmac.compare_digest(sig, expected):
        return (False, None)
    parts = payload.split(':')
    if len(parts) < 3:
        return (False, None)
    user, exp = parts[0], parts[1]
    try:
        if int(exp) < int(time.time()):
            return (False, None)
    except Exception:
        return (False, None)
    return (True, user)


def _factory_auth_htpasswd_verify(username: str, password: str) -> bool:
    if not username or not password:
        return False

    env_user = (os.environ.get('FACTORY_LOGIN_USER') or '').strip()
    env_pass = (os.environ.get('FACTORY_LOGIN_PASS') or '').strip()
    if env_user and env_pass and username == env_user:
        return hmac.compare_digest(password, env_pass)

    try:
        lines = Path(FACTORY_HTPASSWD_FILE).read_text(encoding='utf-8', errors='ignore').splitlines()
    except Exception:
        lines = []

    for line in lines:
        if ':' not in line:
            continue
        u, h = line.split(':', 1)
        u = u.strip()
        h = h.strip()
        if u != username or not h:
            continue
        cand = crypt.crypt(password, h)
        if cand and hmac.compare_digest(cand, h):
            return True
        if h.startswith('$2y$'):
            h2 = '$2b$' + h[4:]
            cand2 = crypt.crypt(password, h2)
            if cand2 and hmac.compare_digest(cand2, h2):
                return True
    return False


def _factory_login_html(error: str = '') -> str:
    err = html_lib.escape(error or '')
    html = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"> 
<meta name="robots" content="noindex,nofollow,noarchive,nosnippet">
<title>Factory Login</title>
<style>
  :root { color-scheme: dark; }
  body {
    font-family: Inter, system-ui, -apple-system, Segoe UI, sans-serif;
    background: #1f0f1a;
    color: #f3e8ef;
    min-height: 100vh;
    margin: 0;
  }
  .shell {
    min-height: 100vh;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding: clamp(64px, 12vh, 120px) 24px 24px;
    box-sizing: border-box;
  }
  .card {
    width: min(420px, 92vw);
    background: #341427;
    border: 1px solid #5e2c49;
    border-radius: 16px;
    padding: 24px;
    box-sizing: border-box;
  }
  h1 { margin: 0 0 10px; font-size: 34px; line-height: 1.1; font-weight: 800; }
  .sub { margin: 0 0 14px; color: #d9bfd0; }
  label { display: block; font-size: 13px; margin: 10px 0 6px; color: #e6c9da; }
  input {
    width: 100%;
    height: 44px;
    box-sizing: border-box;
    padding: 0 12px;
    border-radius: 10px;
    border: 1px solid #6d3a56;
    background: #2a1020;
    color: #fff;
    outline: none;
  }
  input:focus { border-color: #8f5cff; box-shadow: 0 0 0 1px #8f5cff; }
  .error {
    min-height: 20px;
    margin: 0 0 8px;
    color: #ffb4b4;
    font-size: 13px;
  }
  button {
    margin-top: 14px;
    width: 100%;
    height: 44px;
    border: 0;
    border-radius: 10px;
    background: #d65a9a;
    color: #190911;
    font-weight: 700;
    cursor: pointer;
  }
</style>
</head><body>
<div class="shell"><form class="card" method="post" action="/factory-login" novalidate>
<h1>YAS Factory Login</h1><p class="sub">Sign in to access the publishing factory.</p>
<p class="error" id="form-error">__ERR__</p>
<label for="username">Login</label><input id="username" name="username" autocomplete="username" required>
<label for="password">Password</label><input id="password" type="password" name="password" autocomplete="current-password" required>
<input type="hidden" name="next" value="{next}">
<button type="submit">Sign in</button></form></div>
<script>
(function() {
  var form = document.querySelector('form.card');
  var user = document.getElementById('username');
  var pass = document.getElementById('password');
  var err = document.getElementById('form-error');
  form.addEventListener('submit', function(e) {
    if (!user.value.trim() || !pass.value.trim()) {
      e.preventDefault();
      err.textContent = 'Enter login and password.';
      if (!user.value.trim()) user.focus(); else pass.focus();
      return;
    }
    err.textContent = '';
  });
})();
</script>
</body></html>"""
    return html.replace('__ERR__', err)


@app.get('/factory-login', response_class=HTMLResponse)
def factory_login_page(request: Request, next: str = '/'):
    ok, _ = _factory_verify_token(request.cookies.get(FACTORY_SESSION_COOKIE))
    if ok:
        target = next if (next or '').startswith('/') else '/'
        return RedirectResponse(url=target, status_code=302)
    html = _factory_login_html('').replace('{next}', html_lib.escape(next if (next or '').startswith('/') else '/'))
    return HTMLResponse(content=html, headers={'X-Robots-Tag': 'noindex, nofollow, noarchive, nosnippet'})


@app.post('/factory-login', response_class=HTMLResponse)
async def factory_login_submit(request: Request):
    form = await request.form()
    username = (form.get('username') or '').strip()
    password = (form.get('password') or '').strip()
    next_url = (form.get('next') or '/').strip()
    if not next_url.startswith('/'):
        next_url = '/'

    if not _factory_auth_htpasswd_verify(username, password):
        html = _factory_login_html('Invalid login or password.').replace('{next}', html_lib.escape(next_url))
        return HTMLResponse(status_code=401, content=html, headers={'X-Robots-Tag': 'noindex, nofollow, noarchive, nosnippet'})

    token = _factory_issue_token(username)
    resp = RedirectResponse(url=next_url, status_code=302)
    resp.set_cookie(FACTORY_SESSION_COOKIE, token, max_age=max(600, FACTORY_SESSION_TTL_SECONDS), httponly=True, secure=True, samesite='lax', path='/')
    resp.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive, nosnippet'
    return resp


@app.get('/factory-logout')
def factory_logout(next: str = '/factory/factory-login'):
    target = next if (next or '').startswith('/') else '/factory/factory-login'
    resp = RedirectResponse(url=target, status_code=302)
    resp.delete_cookie(FACTORY_SESSION_COOKIE, path='/')
    resp.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive, nosnippet'
    return resp


@app.get('/api/internal/factory-auth')
def factory_internal_auth(request: Request):
    ok, _ = _factory_verify_token(request.cookies.get(FACTORY_SESSION_COOKIE))
    if not ok:
        return Response(status_code=401)
    return Response(status_code=204)

def _seo_enabled() -> bool:
    raw = (os.environ.get("SEO_MODULE_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on")

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
    "SEO_PUBLISH_LOCALES",
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
    "TUMBLR_CONSUMER_KEY",
    "TUMBLR_CONSUMER_SECRET",
    "TUMBLR_BLOG_HOSTNAME",
    "TWITTER_BEARER_TOKEN",
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_BACKUP",
    "GEMINI_ACTIVE_KEY",
    "GEMINI_TEXT_MODEL",
    "GEMINI_IMAGE_MODEL",
}

SOCIAL_SECRET_KEYS = {
    "LINKEDIN_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TUMBLR_CONSUMER_SECRET",
    "TWITTER_BEARER_TOKEN",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_BACKUP",
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


def _seo_sections_mode() -> bool:
    val = str(os.environ.get("SEO_SECTIONS_MODE", "0")).strip().lower()
    return val in ("1", "true", "yes", "on")


def _seo_section_for_entity(entity_type: str):
    if not _seo_sections_mode():
        return None
    et = (entity_type or "").strip().lower()
    if et == "country":
        return "wine-countries"
    if et == "region":
        return "wine-regions"
    return None


def _seo_section_for_job(job_id: str):
    try:
        with db_connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT entity_type FROM seo_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        return _seo_section_for_entity(row[0] or "")
    except Exception:
        return None


def _section_url(section: str, slug: str, locale: str = "en") -> str:
    origin = _site_origin().rstrip("/")
    if locale and locale != "en":
        return f"{origin}/{locale}/{section}/{slug}/"
    return f"{origin}/{section}/{slug}/"


def _apply_hreflang_block_for_path(html: str, section_path: str, locale: str) -> str:
    origin = _site_origin().rstrip("/")
    base = "/" + (section_path or "").strip("/") + "/"

    if locale != "en":
        canonical = f"{origin}/{locale}{base}"
    else:
        canonical = f"{origin}{base}"

    alts = {"en": f"{origin}{base}"}
    for loc in LOCALES:
        alts[loc] = f"{origin}/{loc}{base}"

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

    blog_dir = os.path.dirname(index_path)
    posts = []
    seen_href: set[str] = set()
    seen_title_key: set[str] = set()

    def _title_key(s: str) -> str:
        t = (s or "").lower().strip()
        t = t.replace("&", " and ")
        t = re.sub(r"\b20\d{2}\b", " ", t)
        t = re.sub(r"[^a-z0-9]+", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    def _feed_safe_image(url_path: str) -> str:
        img = (url_path or "").strip()
        if not img or not img.startswith("/blog/"):
            return img
        file_name = os.path.basename(img)
        local_abs = os.path.join(blog_dir, file_name)

        # Homepage JS prefers *-card.webp. If it does not exist, add query param
        # so JS keeps original file and does not rewrite to a missing thumbnail.
        if re.search(r"-hero\.webp$", file_name, flags=re.IGNORECASE):
            card_name = re.sub(r"-hero\.webp$", "-hero-card.webp", file_name, flags=re.IGNORECASE)
            if not os.path.exists(os.path.join(blog_dir, card_name)) and os.path.exists(local_abs):
                return img + "?full=1"
        if re.search(r"-img-1\.webp$", file_name, flags=re.IGNORECASE):
            card_name = re.sub(r"-img-1\.webp$", "-img-1-card.webp", file_name, flags=re.IGNORECASE)
            if not os.path.exists(os.path.join(blog_dir, card_name)) and os.path.exists(local_abs):
                return img + "?full=1"
        return img

    def _desc_from_blog_page(href: str) -> str:
        h = (href or '').strip()
        if not h:
            return ''
        rel = h.lstrip('/')
        if rel.startswith('blog/'):
            page_abs = os.path.join(site_root, rel)
        else:
            page_abs = os.path.join(site_root, rel)
        if not os.path.exists(page_abs):
            return ''
        try:
            src_page = Path(page_abs).read_text(encoding='utf-8')
            for meta in re.finditer(r"<meta\b[^>]*>", src_page, flags=re.IGNORECASE):
                tag = meta.group(0)
                if not re.search(r'\bname\s*=\s*(["\'])description\1', tag, flags=re.IGNORECASE):
                    continue
                m_content = re.search(r'\bcontent\s*=\s*(["\'])(.*?)\1', tag, flags=re.IGNORECASE | re.DOTALL)
                if m_content:
                    val = html_lib.unescape((m_content.group(2) or '').strip())
                    if val:
                        return re.sub(r"\s+", " ", val).strip()
            m_p = re.search(r'<p[^>]*>(.*?)</p>', src_page, flags=re.IGNORECASE | re.DOTALL)
            if m_p:
                txt = re.sub(r'<[^>]+>', ' ', m_p.group(1) or '')
                txt = html_lib.unescape(re.sub(r"\s+", " ", txt).strip())
                if txt:
                    return txt[:220]
        except Exception:
            return ''
        return ''
    for m in pattern.finditer(src):
        href = (m.group(1) or '').strip()
        image = (m.group(2) or '').strip()
        category = html_lib.unescape(re.sub(r'<[^>]+>', '', (m.group(3) or ''))).strip()
        title = html_lib.unescape(re.sub(r'<[^>]+>', '', (m.group(4) or ''))).strip()
        desc = html_lib.unescape(re.sub(r'<[^>]+>', '', (m.group(5) or ''))).strip()

        if not href:
            continue
        if not desc:
            desc = _desc_from_blog_page(href)
        if image and not image.startswith('/'):
            image = '/blog/' + image.lstrip('./')

        title_key = _title_key(title)
        href_key = href.lower()
        if href_key in seen_href:
            continue
        if title_key and title_key in seen_title_key:
            continue
        seen_href.add(href_key)
        if title_key:
            seen_title_key.add(title_key)

        image = _feed_safe_image(image)
        posts.append({
            'href': href,
            'image': image or '/hero_ai.jpg',
            'category': category,
            'title': title,
            'description': desc,
        })

    # Include published SEO pages (wine-countries / wine-regions) so homepage hero can rotate them too.
    locale = _feed_locale_from_path(out_path)
    site_root = _feed_site_root_from_path(out_path)
    section_posts = _section_feed_posts_for_locale(locale, site_root)
    if section_posts:
        seen_mix: set[str] = set()
        merged_posts: list[dict[str, Any]] = []
        for item in [*section_posts, *posts]:
            href_key = str((item.get('href') or '')).strip().lower()
            title_key = _title_key(str(item.get('title') or ''))
            key = href_key or title_key
            if not key or key in seen_mix:
                continue
            seen_mix.add(key)
            item.pop('_ts', None)
            merged_posts.append(item)
        posts = merged_posts

    out = {'updatedAt': utcnow_iso(), 'posts': posts}
    try:
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
    except Exception:
        return



def _feed_locale_from_path(path: str) -> str:
    p = (path or "").replace("\\", "/")
    m = re.search(r"/(ru|es|de|fr)/blog/feed\.json$", p)
    return (m.group(1) if m else "en")


def _feed_site_root_from_path(path: str) -> str:
    p = (path or "").replace("\\", "/")
    m_loc = re.search(r"^(.*)/(ru|es|de|fr)/blog/feed\.json$", p)
    if m_loc:
        return m_loc.group(1) or "/"
    m_en = re.search(r"^(.*)/blog/feed\.json$", p)
    if m_en:
        return m_en.group(1) or "/"
    return LANDING_DIR


def _section_feed_posts_for_locale(locale: str, site_root: str) -> list[dict[str, Any]]:
    loc = (locale or "en").strip().lower() or "en"
    root = (site_root or LANDING_DIR).rstrip("/") or "/"
    sec_labels = {
        "wine-countries": {"en": "Wine Countries", "ru": "Винные страны", "es": "Paises del vino", "de": "Weinlander", "fr": "Pays du vin"},
        "wine-regions": {"en": "Wine Regions", "ru": "Винные регионы", "es": "Regiones vinicolas", "de": "Weinregionen", "fr": "Regions viticoles"},
    }

    def _idx_abs(section: str) -> str:
        if loc == "en":
            return os.path.join(root, section, "index.html")
        return os.path.join(root, loc, section, "index.html")

    def _slug_from_href(href: str) -> str:
        h = (href or "").strip().strip("/")
        if not h:
            return ""
        parts = [x for x in h.split("/") if x]
        if not parts:
            return ""
        return parts[-1]

    def _page_abs(section: str, slug: str) -> str:
        if loc == "en":
            return os.path.join(root, section, slug, "index.html")
        return os.path.join(root, loc, section, slug, "index.html")

    def _safe_img(src: str, section: str, slug: str) -> str:
        u = (src or "").strip()
        if u.startswith("http://") or u.startswith("https://"):
            try:
                parsed = urllib.parse.urlparse(u)
                if parsed.path:
                    u = parsed.path
            except Exception:
                pass
        if u and u.startswith("/"):
            return u

        p_abs = _page_abs(section, slug)
        if os.path.exists(p_abs):
            try:
                page_src = Path(p_abs).read_text(encoding="utf-8")
                m_og = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', page_src, flags=re.IGNORECASE)
                if m_og:
                    og = (m_og.group(1) or "").strip()
                    if og.startswith("http://") or og.startswith("https://"):
                        parsed = urllib.parse.urlparse(og)
                        if parsed.path:
                            og = parsed.path
                    if og.startswith("/"):
                        return og
            except Exception:
                pass
        return "/hero_ai.jpg"

    def _desc_from_page(section: str, slug: str) -> str:
        p_abs = _page_abs(section, slug)
        if not os.path.exists(p_abs):
            return ""
        try:
            page_src = Path(p_abs).read_text(encoding="utf-8")

            # Robust meta-description extraction: supports any attribute order and quote type.
            for meta in re.finditer(r"<meta\b[^>]*>", page_src, flags=re.IGNORECASE):
                tag = meta.group(0)
                m_name = re.search(r'\bname\s*=\s*(["\'])description\1', tag, flags=re.IGNORECASE)
                if not m_name:
                    continue
                m_content = re.search(r'\bcontent\s*=\s*(["\'])(.*?)\1', tag, flags=re.IGNORECASE | re.DOTALL)
                if m_content:
                    val = html_lib.unescape((m_content.group(2) or "").strip())
                    if val:
                        return re.sub(r"\s+", " ", val).strip()

            # Fallback: first meaningful paragraph text.
            m_p = re.search(r"<p[^>]*>(.*?)</p>", page_src, flags=re.IGNORECASE | re.DOTALL)
            if m_p:
                txt = re.sub(r"<[^>]+>", " ", m_p.group(1) or "")
                txt = html_lib.unescape(re.sub(r"\s+", " ", txt).strip())
                if txt:
                    return txt[:220]
        except Exception:
            return ""
        return ""


    rows: list[dict[str, Any]] = []
    for section in ("wine-countries", "wine-regions"):
        idx = _idx_abs(section)
        if not os.path.exists(idx):
            continue
        try:
            src = Path(idx).read_text(encoding="utf-8")
        except Exception:
            continue

        for m in re.finditer(r'<a\s+class="card"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', src, flags=re.IGNORECASE | re.DOTALL):
            href = (m.group(1) or "").strip()
            block = m.group(2) or ""
            if not href:
                continue
            slug = _slug_from_href(href)
            if not slug:
                continue

            m_name = re.search(r'<div\s+class="name">(.*?)</div>', block, flags=re.IGNORECASE | re.DOTALL)
            title = html_lib.unescape(re.sub(r'<[^>]+>', '', (m_name.group(1) if m_name else slug.replace('-', ' ').title()))).strip()
            m_bg = re.search(r"background-image:url\('([^']+)'\)", block, flags=re.IGNORECASE)
            img = _safe_img((m_bg.group(1) if m_bg else ""), section, slug)
            desc = _desc_from_page(section, slug)

            ts = 0.0
            p_abs = _page_abs(section, slug)
            try:
                ts = os.path.getmtime(p_abs) if os.path.exists(p_abs) else os.path.getmtime(idx)
            except Exception:
                ts = 0.0

            rows.append({
                "_ts": ts,
                "href": href,
                "image": img,
                "category": sec_labels.get(section, {}).get(loc, sec_labels.get(section, {}).get("en", "Wine")),
                "title": title,
                "description": desc,
            })

    rows.sort(key=lambda x: x.get("_ts") or 0, reverse=True)
    return rows

def _apply_hreflang_block(html: str, slug: str, locale: str) -> str:
    origin = _site_origin()
    canonical = f"{origin}/{locale}/blog/{slug}/" if locale != "en" else f"{origin}/blog/{slug}/"
    alts = {
        "en": f"{origin}/blog/{slug}/",
        "ru": f"{origin}/ru/blog/{slug}/",
        "es": f"{origin}/es/blog/{slug}/",
        "de": f"{origin}/de/blog/{slug}/",
        "fr": f"{origin}/fr/blog/{slug}/",
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


def _ensure_min_inline_placeholders(content_html: str, slug: str, min_images: int = 3) -> str:
    src = content_html or ""
    existing = len(re.findall(r"<img\b", src, flags=re.IGNORECASE))
    need = max(0, int(min_images) - existing)
    if need <= 0:
        return src

    placeholders = []
    base = _slugify(slug or "seo")
    for i in range(1, need + 1):
        ph_src = f"{base}-inline-{i}.png"
        placeholders.append(
            (
                f'<figure class="seo-inline-image">'
                f'<img src="{ph_src}" alt="Wine illustration {i}" loading="lazy"/>'
                f"</figure>"
            )
        )

    idx = 0
    def _inject_after_h2(m: re.Match[str]) -> str:
        nonlocal idx
        out = m.group(0)
        if idx < len(placeholders):
            out += "\n" + placeholders[idx] + "\n"
            idx += 1
        return out

    out = re.sub(r"</h2>", _inject_after_h2, src, flags=re.IGNORECASE)
    while idx < len(placeholders):
        out += "\n" + placeholders[idx] + "\n"
        idx += 1
    return out


def _rewrite_section_blog_artifacts(html: str, section: str, slug: str, locale: str) -> str:
    out = html or ""
    # Some legacy SEO jobs may store full published HTML instead of article body.
    # Extract only article content to avoid nested old templates/CSS leaking in.
    if re.search(r"(?is)<html\b|<!doctype", out):
        m_article = re.search(r'(?is)<article[^>]*class="[^"]*\bcontent\b[^"]*"[^>]*>(.*?)</article>', out)
        if m_article:
            out = m_article.group(1)
        else:
            m_post = re.search(r'(?is)<div[^>]*class="[^"]*\bpost-content\b[^"]*"[^>]*>(.*?)</div>', out)
            if m_post:
                out = m_post.group(1)
            else:
                m_body = re.search(r"(?is)<body[^>]*>(.*?)</body>", out)
                if m_body:
                    out = m_body.group(1)
        # Remove wrappers that are irrelevant for section body.
        out = re.sub(r"(?is)<(style|script|header|footer|nav)\b[^>]*>.*?</\1>", "", out)
        out = re.sub(r"(?is)<(html|head|body)\b[^>]*>", "", out)
        out = re.sub(r"(?is)</(html|head|body)>", "", out)

    origin = _site_origin().rstrip("/")
    locale_prefix = f"/{locale}" if locale and locale != "en" else ""
    blog_url = f"{origin}{locale_prefix}/blog/{slug}/"
    section_url = f"{origin}{locale_prefix}/{section}/{slug}/"
    section_index = f"{origin}{locale_prefix}/{section}/"
    section_name = "Wine Countries" if section == "wine-countries" else ("Wine Regions" if section == "wine-regions" else "Wine")

    # Fix schema/breadcrumb canonical references generated by blog template.
    out = out.replace(blog_url, section_url)
    out = out.replace(f'href="{locale_prefix}/blog/"', f'href="{locale_prefix}/{section}/"')
    out = out.replace(f'href="{origin}{locale_prefix}/blog/"', f'href="{section_index}"')
    out = out.replace(">Blog<", f">{section_name}<")

    # Section pages are not under /blog/, so relative inline image src must point to /blog assets.
    def _img_src_to_blog(m: re.Match[str]) -> str:
        src = (m.group(1) or "").strip()
        if not src:
            return m.group(0)
        low = src.lower()
        if low.startswith("http://") or low.startswith("https://") or low.startswith("data:"):
            return m.group(0)
        if src.startswith("/"):
            return m.group(0)
        return m.group(0).replace(src, f"/blog/{src}")

    out = re.sub(r'<img[^>]*\bsrc="([^"]+)"', _img_src_to_blog, out, flags=re.IGNORECASE)
    return out



def _rewrite_blog_inline_img_srcs(html: str) -> str:
    out = html or ""

    # Blog posts in /<locale>/blog/*.html must reference shared media in /blog/.
    # Relative sources like "slug-img-1.webp" break on localized routes.
    def _img_src_to_blog(m: re.Match[str]) -> str:
        src = (m.group(1) or "").strip()
        if not src:
            return m.group(0)
        low = src.lower()
        if low.startswith("http://") or low.startswith("https://") or low.startswith("data:"):
            return m.group(0)
        if src.startswith("/"):
            return m.group(0)
        return m.group(0).replace(src, f"/blog/{src}")

    return re.sub(r'<img[^>]*\bsrc="([^"]+)"', _img_src_to_blog, out, flags=re.IGNORECASE)



def _sanitize_desc_tail(s: str) -> str:
    t = re.sub(r"\s+", " ", (s or "").strip())
    if not t:
        return t
    dangling = {
        "and", "or", "with", "for", "to", "of", "in", "on", "at", "from", "by", "as",
        "und", "et", "y", "e", "de", "del", "la", "le", "da", "di", "con",
        "и", "с", "на", "по", "для", "а", "или",
    }
    parts = t.split(" ")
    while len(parts) > 1 and parts[-1].strip(" ,;:-").lower() in dangling:
        parts.pop()
    out = " ".join(parts).rstrip(" ,;:-")
    if out and out[-1] not in ".!?":
        out += "."
    return out



_COUNTRY_FLAG_CODE = {
    "argentina": "ar", "australia": "au", "austria": "at", "brazil": "br", "chile": "cl",
    "france": "fr", "georgia": "ge", "germany": "de", "greece": "gr", "hungary": "hu",
    "italy": "it", "mexico": "mx", "moldova": "md", "new zealand": "nz", "portugal": "pt",
    "romania": "ro", "south africa": "za", "spain": "es", "switzerland": "ch",
    "united states": "us", "uruguay": "uy", "usa": "us", "u.s.": "us",
}


def _country_flag_url(country: str) -> str:
    code = _COUNTRY_FLAG_CODE.get((country or "").strip().lower(), "")
    return f"https://flagcdn.com/{code}.svg" if code else ""


def _section_entity_type(section: str) -> str | None:
    sec = (section or "").strip().lower()
    if sec == "wine-countries":
        return "country"
    if sec == "wine-regions":
        return "region"
    return None


def _refresh_seo_section_indexes(section: str) -> list[str]:
    et = _section_entity_type(section)
    if not et:
        return []

    def _unesc(x: str) -> str:
        return html_lib.unescape((x or '').strip())

    def _guess_country_from_slug(slug: str) -> str:
        parts = (slug or '').strip('/').split('-')
        if len(parts) >= 2:
            tail = ' '.join(parts[-2:])
            if tail.lower() in _COUNTRY_FLAG_CODE:
                return tail
            one = parts[-1]
            if one.lower() in _COUNTRY_FLAG_CODE:
                return one
        return ''

    # 1) Existing cards from current EN index (so we never drop already visible cards).
    existing_cards: dict[str, dict[str, str]] = {}
    en_idx = os.path.join(LANDING_DIR, section, 'index.html')
    if os.path.exists(en_idx):
        src = Path(en_idx).read_text(encoding='utf-8')
        m_grid = re.search(r'<section class="grid">(.*?)</section>', src, flags=re.DOTALL)
        grid = m_grid.group(1) if m_grid else ''
        for m in re.finditer(r'<a class="card" href="(?:/[^/]+)?/' + re.escape(section) + r'/([^/]+)/">(.*?)</a>', grid, flags=re.DOTALL):
            slug = (m.group(1) or '').strip()
            block = m.group(2) or ''
            if not slug:
                continue
            m_name = re.search(r'<div class="name">(.*?)</div>', block, flags=re.DOTALL)
            label = _unesc(re.sub(r'<[^>]+>', '', m_name.group(1) if m_name else slug.replace('-', ' ').title()))
            m_bg = re.search(r"background-image:url\('([^']+)'\)", block)
            hero_url = (m_bg.group(1).strip() if m_bg else '/logo.png')
            m_flag = re.search(r'<img[^>]*class="flag-badge"[^>]*src="([^"]+)"', block)
            flag_url = (m_flag.group(1).strip() if m_flag else '')
            country = ''
            if ', ' in label:
                country = label.split(',')[-1].strip()
            if not country:
                country = _guess_country_from_slug(slug)
            existing_cards[slug] = {
                'slug': slug,
                'label': label,
                'hero_url': hero_url,
                'flag_url': flag_url,
                'country': country,
            }

    # 2) Published cards from DB (override existing where same slug).
    db_cards: dict[str, dict[str, str]] = {}
    with db_connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT j.slug, j.hero_image, e.country, e.region
            FROM seo_jobs sj
            JOIN seo_entities e ON e.id = sj.entity_id
            JOIN jobs j ON j.id = sj.id
            WHERE sj.status='PUBLISHED' AND e.entity_type=?
            ORDER BY LOWER(COALESCE(e.country,'')), LOWER(COALESCE(e.region,'')), LOWER(COALESCE(j.slug,''))
            """,
            (et,),
        ).fetchall()

    for slug, hero_image, country, region in rows:
        slug = (slug or '').strip()
        if not slug:
            continue
        country = (country or '').strip()
        region = (region or '').strip()
        if et == 'country':
            label = country or slug.replace('wine-country-', '').replace('-', ' ').title()
        else:
            region_label = region or slug.replace('wine-region-', '').replace('wine-guide-', '').replace('-', ' ').title()
            label = f"{region_label}, {country}" if country else region_label
        hero = Path((hero_image or '').strip()).name if hero_image else ''
        hero_url = f"/blog/{hero}" if hero else existing_cards.get(slug, {}).get('hero_url', '/logo.png')
        flag_url = _country_flag_url(country) or existing_cards.get(slug, {}).get('flag_url', '')
        db_cards[slug] = {
            'slug': slug,
            'label': label,
            'hero_url': hero_url,
            'flag_url': flag_url,
            'country': country,
        }

    # 3) Merge: keep existing, enrich/override with DB where possible.
    merged = dict(existing_cards)
    merged.update(db_cards)

    # 4) Filesystem fallback: keep any existing published section pages visible in index.
    section_dir = os.path.join(LANDING_DIR, section)
    if os.path.isdir(section_dir):
        for name in sorted(os.listdir(section_dir)):
            slug = (name or '').strip()
            if not slug or slug == 'index.html':
                continue
            dpath = os.path.join(section_dir, slug)
            ipath = os.path.join(dpath, 'index.html')
            if not os.path.isdir(dpath) or not os.path.exists(ipath):
                continue
            if slug in merged:
                continue

            country = _guess_country_from_slug(slug)
            if et == 'country':
                label = (country or slug.replace('wine-country-', '').replace('-', ' ').title())
            else:
                base = slug.replace('wine-region-', '').replace('wine-guide-', '').strip('-')
                bits = [b for b in base.split('-') if b]
                region_bits = bits[:-2] if len(bits) >= 3 else bits[:-1]
                region_label = ' '.join(region_bits).title() if region_bits else base.replace('-', ' ').title()
                label = f"{region_label}, {country.title()}" if country else region_label

            # Prefer hero extracted from page itself (works for legacy slug patterns like wine-guide-*).
            hero_url = '/logo.png'
            try:
                page_src = Path(ipath).read_text(encoding='utf-8')
                m_og = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', page_src, flags=re.IGNORECASE)
                if m_og:
                    u = (m_og.group(1) or '').strip()
                    if u.startswith('https://') or u.startswith('http://'):
                        parsed = urllib.parse.urlparse(u)
                        if parsed.path:
                            u = parsed.path
                    if u.startswith('/blog/'):
                        hero_url = u
                if hero_url == '/logo.png':
                    m_bg = re.search(r"hero-cover[^>]*style=\"background-image:url\('([^']+)'\)\"", page_src, flags=re.IGNORECASE)
                    if m_bg:
                        u = (m_bg.group(1) or '').strip()
                        if u.startswith('/blog/'):
                            hero_url = u
            except Exception:
                pass

            if hero_url == '/logo.png':
                hero_candidate = f"/blog/{slug}-hero.webp"
                hero_abs = os.path.join(LANDING_DIR, 'blog', f"{slug}-hero.webp")
                hero_url = hero_candidate if os.path.exists(hero_abs) else '/logo.png'
            flag_url = _country_flag_url(country)
            merged[slug] = {
                'slug': slug,
                'label': label,
                'hero_url': hero_url,
                'flag_url': flag_url,
                'country': country,
            }

    cards = sorted(merged.values(), key=lambda c: ((c.get('country') or '').lower(), (c.get('label') or '').lower(), (c.get('slug') or '').lower()))

    def _country_key(name: str) -> str:
        k = (name or '').strip().lower()
        k = re.sub(r"[^a-z0-9]+", '-', k)
        k = re.sub(r"-+", '-', k).strip('-')
        return k or 'unknown'

    changed: list[str] = []
    locales = ['en', *LOCALES]
    for loc in locales:
        idx_abs = os.path.join(LANDING_DIR, section, 'index.html') if loc == 'en' else os.path.join(LANDING_DIR, loc, section, 'index.html')
        if not os.path.exists(idx_abs):
            continue
        src = Path(idx_abs).read_text(encoding='utf-8')

        locale_prefix = '' if loc == 'en' else f'/{loc}'

        # Build country chips from currently visible region cards.
        countries = []
        seen_c = set()
        for c in cards:
            c_name = (c.get('country') or '').strip()
            if not c_name:
                continue
            c_key = _country_key(c_name)
            if c_key in seen_c:
                continue
            seen_c.add(c_key)
            countries.append({
                'key': c_key,
                'name': c_name,
                'flag': (c.get('flag_url') or '').strip(),
            })
        countries = sorted(countries, key=lambda x: x['name'].lower())

        out_cards = []
        for c in cards:
            href = f"{locale_prefix}/{section}/{c['slug']}/"
            country_txt = (c.get('country') or '').strip()
            country_key = _country_key(country_txt)
            flag_html = f'<img class="flag-badge" src="{c.get("flag_url", "")}" alt="{html_lib.escape(country_txt)} flag" loading="lazy"/>' if c.get('flag_url') else ''
            hero_for_card = c.get('hero_url', '/logo.png')
            if hero_for_card == '/logo.png':
                try:
                    page_path = os.path.join(LANDING_DIR, section, c['slug'], 'index.html')
                    if os.path.exists(page_path):
                        page_src = Path(page_path).read_text(encoding='utf-8')
                        m_og = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', page_src, flags=re.IGNORECASE)
                        if m_og:
                            u = (m_og.group(1) or '').strip()
                            if u.startswith('https://') or u.startswith('http://'):
                                parsed = urllib.parse.urlparse(u)
                                if parsed.path:
                                    u = parsed.path
                            if u.startswith('/blog/'):
                                hero_for_card = u
                except Exception:
                    pass
            out_cards.append(
                f'        <a class="card" data-country="{country_key}" href="{href}"><div class="thumb" style="background-image:url(\'{hero_for_card}\')">{flag_html}</div><div class="name">{html_lib.escape(c.get("label", ""))}</div></a>'
            )
        grid_html = "\n".join(out_cards)
        new_src, n = re.subn(
            r'(<section class="grid">)(.*?)(</section>)',
            lambda m: m.group(1) + ("\n" + grid_html + "\n      " if grid_html else "") + m.group(3),
            src,
            count=1,
            flags=re.DOTALL,
        )

        if section == 'wine-regions':
            all_label = {'en': 'All', 'ru': 'Все', 'es': 'Todos', 'de': 'Alle', 'fr': 'Tous'}.get(loc, 'All')
            chips = [f'<button class="country-chip is-all is-active" type="button" data-country="all" aria-pressed="true">{all_label}</button>']
            for ci in countries:
                flag = f'<img class="country-chip-flag" src="{ci["flag"]}" alt="{html_lib.escape(ci["name"])} flag" loading="lazy"/>' if ci.get('flag') else ''
                chips.append(f'<button class="country-chip" type="button" data-country="{ci["key"]}" aria-pressed="false">{flag}<span>{html_lib.escape(ci["name"])}</span></button>')
            chips_html = '<section class="country-filters" data-country-filters>' + ''.join(chips) + '</section>'
            if '<section class="country-filters"' in new_src:
                new_src = re.sub(r'<section class="country-filters"[^>]*>.*?</section>', chips_html, new_src, count=1, flags=re.DOTALL)
            else:
                new_src = re.sub(r'(<p class="sub">.*?</p>)', r'\1\n      ' + chips_html, new_src, count=1, flags=re.DOTALL)

            css = '''
    .country-filters{margin-top:14px;display:flex;flex-wrap:wrap;gap:8px}
    .country-chip{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;border:1px solid var(--line);background:rgba(255,255,255,.05);color:var(--text);font-weight:600;font-size:13px;cursor:pointer}
    .country-chip:hover{background:rgba(255,255,255,.1)}
    .country-chip.is-active{background:rgba(151,36,66,.45);border-color:rgba(255,255,255,.35)}
    .country-chip-flag{width:22px;height:15px;border-radius:3px;object-fit:cover;border:1px solid rgba(255,255,255,.4)}
    .card.is-hidden{display:none !important}
    @media (max-width:740px){.country-chip{padding:7px 10px;font-size:12px}.country-chip-flag{width:18px;height:12px}}
'''
            if '.country-filters{' not in new_src:
                new_src = new_src.replace('</style>', css + '\n  </style>', 1)

            js = '''
<script id="country-filter-script">
(function(){
  const root = document.querySelector('[data-country-filters]');
  const grid = document.querySelector('.grid');
  if(!root || !grid) return;
  const chips = Array.from(root.querySelectorAll('.country-chip'));
  const cards = Array.from(grid.querySelectorAll('.card[data-country]'));
  let active = new Set();

  const allChip = root.querySelector('.country-chip[data-country="all"]');
  function sync(){
    const has = active.size > 0;
    chips.forEach(ch=>{
      const key = ch.getAttribute('data-country');
      const on = key === 'all' ? !has : active.has(key);
      ch.classList.toggle('is-active', on);
      ch.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    cards.forEach(card=>{
      const k = card.getAttribute('data-country') || '';
      const show = !has || active.has(k);
      card.classList.toggle('is-hidden', !show);
    });
  }

  chips.forEach(ch=>{
    ch.addEventListener('click', ()=>{
      const key = ch.getAttribute('data-country');
      if(key === 'all'){
        active.clear();
        sync();
        return;
      }
      if(active.has(key)) active.delete(key);
      else active.add(key);
      sync();
    });
  });

  if(allChip){ active.clear(); sync(); }
})();
</script>
'''
            if 'id="country-filter-script"' not in new_src:
                new_src = new_src.replace('</body>', js + '\n</body>', 1)

        if n and new_src != src:
            Path(idx_abs).write_text(new_src, encoding='utf-8')
            rel = os.path.join(section, 'index.html') if loc == 'en' else os.path.join(loc, section, 'index.html')
            changed.append(rel)
    return changed

def _render_seo_section_html(
    *,
    title: str,
    description: str,
    section: str,
    slug: str,
    hero_image: str,
    content_html: str,
    updated_at: str,
    locale: str = "en",
    noindex: bool = False,
) -> str:
    def _slugify_local(s: str) -> str:
        x = (s or "").strip().lower()
        x = re.sub(r"[^a-z0-9\s-]", "", x)
        x = re.sub(r"\s+", "-", x)
        x = re.sub(r"-+", "-", x)
        return x[:120].strip("-") or "section"

    origin = _site_origin().rstrip("/")
    section_label = "Wine Countries" if section == "wine-countries" else ("Wine Regions" if section == "wine-regions" else "Wine")
    locale_prefix = f"/{locale}" if locale and locale != "en" else ""
    section_index = f"{locale_prefix}/{section}/"
    page_url = f"{origin}{locale_prefix}/{section}/{slug}/"
    hero_file = os.path.basename(hero_image or "logo.png")
    hero_url = f"/blog/{hero_file}" if hero_file else "/logo.png"
    body_html = _rewrite_section_blog_artifacts(content_html or "", section, slug, locale)
    # Build compact TOC from H2/H3 in section content.
    toc_items = []
    for m in re.finditer(r"<(h2|h3)([^>]*)>(.*?)</\1>", body_html, flags=re.IGNORECASE | re.DOTALL):
        tag = (m.group(1) or "").lower()
        attrs = m.group(2) or ""
        inner = m.group(3) or ""
        text = re.sub(r"<[^>]+>", "", inner).strip()
        if not text:
            continue
        id_match = re.search(r'\bid\s*=\s*"([^"]+)"', attrs, flags=re.IGNORECASE)
        hid = (id_match.group(1).strip() if id_match else "") or _slugify_local(text)
        if not id_match:
            full = m.group(0)
            with_id = f"<{tag}{attrs} id=\"{hid}\">{inner}</{tag}>"
            body_html = body_html.replace(full, with_id, 1)
        toc_items.append((tag, hid, html_lib.escape(text)))
    toc_items = toc_items[:18]
    toc_title_map = {
        "en": "On this page",
        "ru": "На этой странице",
        "es": "En esta página",
        "de": "Auf dieser Seite",
        "fr": "Sur cette page",
    }
    toc_title = toc_title_map.get(locale, "On this page")
    toc_html = ""
    if toc_items:
        toc_html = (
            '<aside class="toc"><div class="toc-title">' + toc_title + '</div><ul>'
            + "".join([
                f'<li><a class="{("toc-h3" if lvl=="h3" else "toc-h2")}" href="#{hid}">{txt}</a></li>'
                for lvl, hid, txt in toc_items
            ])
            + "</ul></aside>"
        )
    robots = "noindex,nofollow" if noindex else "index,follow"
    raw_desc = fit_meta_description(description or "", fallback=title or "")
    raw_desc = _sanitize_desc_tail(raw_desc)
    t = html_lib.escape(title or "")
    d = html_lib.escape(raw_desc)
    updated = html_lib.escape((updated_at or "")[:10] or utcnow_iso()[:10])
    share_title = urllib.parse.quote(title or "")
    share_url = urllib.parse.quote(page_url)
    share_block = (
        '<section class="share-section"><div class="share-label">Share</div><div class="share-group">'
        + f'<a class="share-icon-btn" href="https://www.linkedin.com/sharing/share-offsite/?url={share_url}" target="_blank" rel="noreferrer" aria-label="Share on LinkedIn" title="LinkedIn">'
        + '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M6.94 8.5H3.56V19h3.38V8.5zm.22-3.24a1.95 1.95 0 1 0-3.9 0 1.95 1.95 0 0 0 3.9 0zM20.44 13.03V19h-3.36v-5.5c0-1.3-.47-2.2-1.64-2.2-.9 0-1.43.6-1.66 1.18-.09.2-.11.48-.11.76V19H10.3s.04-9.4 0-10.5h3.37v1.49l-.02.03h.02v-.03c.44-.68 1.22-1.65 2.98-1.65 2.17 0 3.8 1.41 3.8 4.44z"/></svg></a>'
        + f'<a class="share-icon-btn" href="https://twitter.com/intent/tweet?url={share_url}&text={share_title}" target="_blank" rel="noreferrer" aria-label="Share on X" title="X">'
        + '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M18.9 2H22l-6.8 7.77L23 22h-6.2l-4.85-6.33L6.4 22H3.3l7.27-8.31L1 2h6.36l4.38 5.77L18.9 2zm-1.1 18h1.72L6.44 3.9H4.58L17.8 20z"/></svg></a>'
        + f'<a class="share-icon-btn" href="https://www.facebook.com/sharer/sharer.php?u={share_url}" target="_blank" rel="noreferrer" aria-label="Share on Facebook" title="Facebook">'
        + '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M13.5 22v-8h2.7l.4-3h-3.1V9.1c0-.9.3-1.6 1.6-1.6h1.7V4.8c-.3 0-1.3-.1-2.5-.1-2.5 0-4.2 1.5-4.2 4.3V11H7.5v3h2.6v8h3.4z"/></svg></a>'
        + f'<a class="share-icon-btn" href="https://api.whatsapp.com/send?text={share_title}%20{share_url}" target="_blank" rel="noreferrer" aria-label="Share on WhatsApp" title="WhatsApp">'
        + '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M12 2a10 10 0 0 0-8.8 14.73L2 22l5.43-1.14A10 10 0 1 0 12 2zm0 18.2c-1.53 0-3.02-.4-4.32-1.17l-.31-.18-3.22.68.69-3.14-.2-.32A8.24 8.24 0 1 1 12 20.2zm4.52-6.18c-.25-.13-1.5-.74-1.73-.83-.23-.08-.4-.12-.58.13-.17.25-.66.83-.81 1-.15.16-.3.19-.56.06-.25-.12-1.06-.39-2.02-1.26-.75-.67-1.25-1.5-1.4-1.75-.15-.25-.02-.39.11-.52.12-.11.25-.3.37-.44.12-.15.16-.25.25-.42.08-.17.04-.31-.02-.44-.06-.13-.58-1.4-.79-1.92-.21-.5-.42-.43-.58-.43h-.5c-.17 0-.44.06-.67.31-.23.25-.88.86-.88 2.1s.9 2.44 1.02 2.61c.12.17 1.77 2.7 4.28 3.79.6.26 1.06.41 1.42.53.6.19 1.15.16 1.58.1.48-.07 1.5-.61 1.71-1.2.21-.6.21-1.11.15-1.2-.06-.09-.23-.15-.48-.28z"/></svg></a>'
        + f'<a class="share-icon-btn" href="https://t.me/share/url?url={share_url}&text={share_title}" target="_blank" rel="noreferrer" aria-label="Share on Telegram" title="Telegram">'
        + '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69.01-.03.01-.14-.07-.2-.08-.06-.19-.04-.27-.02-.11.02-1.93 1.23-5.46 3.62-.51.35-.98.52-1.4.51-.46-.01-1.35-.26-2.01-.48-.81-.27-1.45-.42-1.39-.89.03-.24.36-.48.99-.74 3.84-1.67 6.41-2.77 7.7-3.3 3.66-1.5 4.42-1.76 4.92-1.77.11 0 .36.03.52.16.13.11.17.26.19.37.02.04.02.13.01.18z"/></svg></a>'
        + '</div></section>'
    )

    return f"""<!DOCTYPE html>
<html lang="{locale}">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{t}</title>
  <meta name="description" content="{d}"/>
  <meta name="robots" content="{robots}"/>
  <meta property="og:type" content="article"/>
  <meta property="og:title" content="{t}"/>
  <meta property="og:description" content="{d}"/>
  <meta property="og:image" content="{origin}{hero_url}"/>
  <meta name="twitter:card" content="summary_large_image"/>
  <meta name="twitter:title" content="{t}"/>
  <meta name="twitter:description" content="{d}"/>
  <meta name="twitter:image" content="{origin}{hero_url}"/>
  <style>
    :root {{
      --bg-dark:#12070c;
      --bg-gradient:linear-gradient(135deg,#12070c 0%,#2a0d16 50%,#4f1424 100%);
      --accent:#b63a5a;
      --accent-hover:#962f49;
      --glass-bg:rgba(255,255,255,.035);
      --glass-border:rgba(255,255,255,.12);
      --text:#f4edf0;
      --dim:#efe6ea;
      --line:rgba(255,255,255,.12);
      --card:rgba(255,255,255,.03);
      --container:1280px;
    }}
    * {{ box-sizing:border-box; }}
    html {{ scroll-behavior:smooth; }}
    body {{ margin:0; font-family:ui-sans-serif,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; color:var(--text); background:var(--bg-dark); overflow-x:hidden; }}
    .fixed-bg {{ position:fixed; inset:0; z-index:-1; background:var(--bg-gradient); }}
    .fixed-bg:before {{ content:""; position:absolute; inset:0; background:
      radial-gradient(circle at 18% 26%, rgba(144,22,56,.45) 0%, transparent 36%),
      radial-gradient(circle at 82% 16%, rgba(188,88,116,.28) 0%, transparent 40%),
      radial-gradient(circle at 50% 76%, rgba(92,16,37,.42) 0%, transparent 42%);
      background-size:200% 200%;
      animation:shift 34s ease infinite;
    }}
    @keyframes shift {{ 0%,100%{{background-position:0% 50%}} 50%{{background-position:100% 50%}} }}
    .container {{ max-width:var(--container); margin:0 auto; padding:0 24px; }}
    nav {{ padding:24px 0; display:flex; justify-content:space-between; align-items:center; position:fixed; top:0; left:0; right:0; z-index:20; transition:.25s; }}
    nav.nav-scrolled {{ background:rgba(18,7,12,.85); backdrop-filter:blur(12px); padding:15px 0; border-bottom:1px solid rgba(255,255,255,.06); }}
    .nav-container {{ max-width:var(--container); margin:0 auto; padding:0 24px; display:flex; justify-content:space-between; align-items:center; width:100%; }}
    .logo {{ display:flex; align-items:center; text-decoration:none; }}
    .logo-img {{ height:64px; width:auto; object-fit:contain; }}
    .nav-links {{ display:flex; gap:22px; align-items:center; }}
    .nav-links a {{ color:var(--dim); text-decoration:none; font-size:14px; font-weight:600; }}
    .nav-links a:hover {{ color:#fff; }}
    .countries-menu {{ position:relative; display:inline-flex; align-items:center; }}
    .countries-btn {{ color:var(--dim); text-decoration:none; font-size:14px; font-weight:600; cursor:pointer; display:inline-flex; align-items:center; gap:6px; }}
    .countries-btn:after {{ content:"▾"; font-size:11px; opacity:.9; }}
    .countries-list {{ position:absolute; top:calc(100% + 10px); left:0; min-width:220px; display:none; flex-direction:column; padding:8px; border:1px solid var(--line); border-radius:12px; background:rgba(18,7,12,.95); box-shadow:0 14px 30px rgba(0,0,0,.35); z-index:50; }}
    .countries-menu:hover .countries-list, .countries-menu:focus-within .countries-list {{ display:flex; }}
    .countries-list a {{ margin:0; padding:8px 10px; border-radius:8px; white-space:nowrap; }}
    .countries-list a:hover {{ background:rgba(182,58,90,.2); }}
    main.container {{ padding-top:120px; padding-bottom:40px; }}
    .hero {{ border:1px solid var(--line); border-radius:20px; background:var(--card); overflow:hidden; box-shadow:0 20px 50px rgba(0,0,0,.35); }}
    .hero-cover {{ width:100%; height:480px; background-size:cover; background-position:center; }}
    .hero-body {{ padding:20px; }}
    .kicker {{ color:var(--dim); font-size:13px; margin-bottom:8px; }}
    h1 {{ margin:0; font-size:36px; line-height:1.12; letter-spacing:-.02em; }}
    .desc {{ margin:12px 0 0; color:var(--dim); font-size:17px; }}
    .meta {{ margin-top:12px; color:var(--dim); font-size:13px; }}
    .crumbs {{ margin-top:8px; color:var(--dim); font-size:13px; }}
    .crumbs a {{ color:var(--dim); text-decoration:none; border-bottom:1px dashed rgba(255,255,255,.25); }}
    .layout {{ margin-top:18px; display:grid; grid-template-columns: 290px minmax(0,1fr); gap:16px; align-items:start; }}
    .toc {{ border:1px solid var(--line); border-radius:16px; background:var(--card); padding:14px; position:sticky; top:92px; max-height:calc(100vh - 108px); overflow:auto; }}
    .toc-title {{ font-size:14px; color:var(--dim); margin-bottom:8px; font-weight:700; }}
    .toc ul {{ list-style:none; margin:0; padding:0; display:grid; gap:6px; }}
    .toc a {{ color:#eddde9; text-decoration:none; font-size:13px; line-height:1.3; display:block; border:1px solid transparent; border-radius:10px; padding:6px 8px; }}
    .toc a:hover {{ border-color:var(--line); background:rgba(255,255,255,.03); }}
    .toc-h3 {{ margin-left:10px; opacity:.92; font-size:12px !important; }}
    .content {{ border:1px solid var(--line); border-radius:20px; background:var(--card); padding:24px; }}
    .content h2 {{ margin-top:28px; margin-bottom:10px; font-size:28px; }}
    .content h3 {{ margin-top:20px; margin-bottom:8px; font-size:21px; color:#f4d8e0; }}
    .content h2[id], .content h3[id] {{ scroll-margin-top:110px; }}
    .content p, .content li {{ color:#efe3f1; line-height:1.72; font-size:17px; }}
    .content a {{ color:#ffc7d4; }}
    .content img {{ max-width:100%; border-radius:14px; border:1px solid var(--line); box-shadow:0 10px 24px rgba(0,0,0,.3); }}
    .content figure {{ margin:22px 0; }}
    .content table {{ width:100%; border-collapse:collapse; margin:14px 0; }}
    .content th,.content td {{ border:1px solid var(--line); padding:10px; text-align:left; }}
    .content blockquote {{ border-left:4px solid var(--accent); margin:20px 0; padding:10px 14px; background:rgba(182,58,90,.12); }}
    footer {{ border-top:1px solid var(--glass-border); padding:40px 0; text-align:center; color:var(--dim); font-size:14px; margin-top:28px; }}
    footer a {{ color:var(--dim); text-decoration:none; margin:0 10px; }}
    @media (max-width: 980px) {{ .layout {{ grid-template-columns:1fr; }} .toc {{ position:static; top:auto; max-height:none; }} }}
  
    .share-section {{ margin-top:60px; padding:28px; background:var(--glass-bg); border:1px solid var(--glass-border); border-radius:24px; display:flex; justify-content:center; align-items:center; gap:20px; flex-direction:column; text-align:center; }}
    .share-label {{ font-weight:700; font-size:16px; color:#fff; text-align:center; }}
    .share-group {{ display:flex; gap:12px; flex-wrap:wrap; justify-content:center; }}
    .share-icon-btn {{ width:44px; height:44px; display:flex; align-items:center; justify-content:center; background:rgba(255,255,255,.05); border:1px solid var(--glass-border); border-radius:12px; color:var(--dim); text-decoration:none; transition:.25s ease; }}
    .share-icon-btn:hover {{ background:rgba(182,58,90,.32); color:#fff; transform:translateY(-2px); }}

    @media (max-width:740px) {{ html, body {{ overflow-x:hidden; }} *, *::before, *::after {{ box-sizing:border-box; }} .container {{ width:100%; max-width:100%; padding:0 16px; margin:0 auto; }} .nav-container {{ width:100%; max-width:100%; padding:0 16px; overflow:hidden; }} .logo-img {{ height:46px; }} .nav-links {{ gap:10px; min-width:0; max-width:100%; overflow:hidden; }} .countries-list {{ display:none !important; }} main.container {{ width:100%; max-width:100%; padding-top:92px; padding-bottom:24px; overflow-x:hidden; }} .hero, .layout, .toc, .content, .share-section {{ width:100%; max-width:100%; min-width:0; }} .hero {{ border-radius:16px; overflow:hidden; }} .hero-cover {{ width:100%; height:420px; background-size:cover; background-position:center; background-repeat:no-repeat; }} .hero-body {{ padding:14px; }} h1 {{ margin:0; font-size:28px; line-height:1.15; word-break:break-word; }} .desc {{ margin-top:10px; font-size:15px; line-height:1.5; word-break:break-word; }} .meta, .crumbs {{ font-size:12px; word-break:break-word; }} .layout {{ margin-top:14px; display:grid; grid-template-columns:1fr; gap:12px; }} .toc {{ display:block; position:static; top:auto; max-height:none; margin:0; padding:12px; border-radius:14px; overflow:hidden; }} .content {{ padding:18px; border-radius:16px; overflow:hidden; }} .content h2 {{ font-size:24px; margin-top:22px; }} .content h3 {{ font-size:19px; }} .content p, .content li {{ font-size:16px; line-height:1.65; word-break:break-word; }} .content img {{ display:block; max-width:100%; height:auto; }} .content table {{ display:block; width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch; }} .content th, .content td {{ min-width:140px; }} .share-section {{ margin-top:18px; border-radius:16px; padding:16px; overflow:hidden; }} .share-label {{ text-align:center; }} .share-group {{ width:100%; display:flex; flex-wrap:wrap; justify-content:center; gap:8px; }} .share-icon-btn {{ width:40px; height:40px; flex:0 0 auto; }} footer {{ margin-top:20px; padding:24px 0; }} }}
  </style>
</head>
<body>
  <div class="fixed-bg"></div>
  <nav>
    <div class="nav-container">
      <a class="logo" href="/"><img class="logo-img" src="/logo-yas-wine-64.webp" alt="YAS Wine Logo" width="64" height="64" decoding="async" fetchpriority="low" /></a>
      <div class="nav-links">
        <a href="/">Home</a>
        <a href="/blog/">Blog</a>
        <div class="countries-menu">
          <a class="countries-btn" href="/wine-countries/">Countries</a>
          <div class="countries-list">
            <a href="/wine-countries/">All Countries</a>
            <a href="/wine-countries/wine-country-brazil/">Brazil</a>
            <a href="/wine-countries/wine-country-chile/">Chile</a>
            <a href="/wine-countries/wine-country-portugal/">Portugal</a>
            <a href="/wine-countries/wine-country-uruguay/">Uruguay</a>
          </div>
        </div>
        <div id="lang-switcher-host"></div>
      </div>
    </div>
  </nav>
  <main class="container">
    <section class="hero">
      <div class="hero-cover" style="background-image:url('{hero_url}')"></div>
      <div class="hero-body">
        <div class="kicker">{section_label}</div>
        <h1>{t}</h1>
        <p class="desc">{d}</p>
        <div class="meta">Updated: {updated}</div>
        <div class="crumbs"><a href="/">Home</a> · <a href="{section_index}">{section_label}</a></div>
      </div>
    </section>
    <section class="layout">
      {toc_html}
      <article class="content">
        {body_html}
      </article>
    </section>
    {share_block}
  </main>
  <footer>
    <p>© {datetime.now(timezone.utc).year} YAS Wine. All rights reserved.</p>
    <div style="margin-top:15px">
      <a href="/policy/terms/">Terms of Service</a> |
      <a href="/policy/privacy/">Privacy Policy</a>
    </div>
  </footer>
  <script defer src="/i18n-switcher.js?v=20260218-1"></script>
  <script defer src="/shared/layout.js?v=20260306-3"></script>
</body>
</html>"""


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

            # Keep publish resilient: some locales may legitimately keep title/description close
            # to EN when they contain many proper nouns/brands.

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
        conn.execute(
            """
            UPDATE jobs
            SET tumblr_status='ERROR', tumblr_error=?, updated_at=?
            WHERE tumblr_status='POSTING' AND updated_at < ?
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


def _mark_stale_seo_jobs(max_age_min: int = 45) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_min)).replace(microsecond=0).isoformat()
    now = utcnow_iso()
    stale_msg = f"Stale SEO job timeout after {max_age_min} minutes"
    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE seo_jobs
            SET status='ERROR', error=COALESCE(error, ?), updated_at=?
            WHERE status IN ('GENERATING','PUBLISHING') AND updated_at < ?
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


def _normalize_publish_locales(raw: str | None) -> tuple[str, ...]:
    tokens = re.split(r"[,;|\s]+", (raw or "").strip())
    out: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        x = (t or "").strip().lower()
        if x == "en":
            continue
        if x not in LOCALES or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return tuple(out)


def _seo_publish_locales() -> tuple[str, ...]:
    raw = (os.environ.get("SEO_PUBLISH_LOCALES") or os.environ.get("LOCALE_PUBLISH_LANGS") or "").strip()
    return _normalize_publish_locales(raw) or tuple(LOCALES)


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



def _gemini_key_settings() -> dict[str, str]:
    values = _env_file_values(ENV_PATH)

    def pick(key: str, *fallbacks: str) -> str:
        for k in (key, *fallbacks):
            v = (values.get(k) or os.environ.get(k) or "").strip()
            if v:
                return v
        return ""

    primary = pick("GEMINI_API_KEY", "GOOGLE_API_KEY")
    backup = pick("GEMINI_API_KEY_BACKUP", "GOOGLE_API_KEY_BACKUP")
    active = (pick("GEMINI_ACTIVE_KEY") or "primary").lower()
    if active not in ("primary", "backup"):
        active = "primary"
    if active == "backup" and not backup:
        active = "primary"
    return {"primary": primary, "backup": backup, "active": active}


def _activate_gemini_key(target: str) -> str:
    st = _gemini_key_settings()
    choice = (target or st.get("active") or "primary").strip().lower()
    if choice == "backup" and st.get("backup"):
        key = st["backup"]
    else:
        key = st.get("primary") or ""
    if key:
        os.environ["GEMINI_API_KEY"] = key
        os.environ["GOOGLE_API_KEY"] = key
    return key


def _active_gemini_api_key() -> str:
    st = _gemini_key_settings()
    return _activate_gemini_key(st.get("active") or "primary")


def _is_gemini_runtime_error(err: Any) -> bool:
    msg = str(err or "").lower()
    if not msg:
        return False
    signals = (
        "http error",
        "too many requests",
        "resource_exhausted",
        "quota",
        "rate limit",
        "api key",
        "forbidden",
        "unauthorized",
        "deadline exceeded",
        "timed out",
        "temporarily unavailable",
        "generativelanguage.googleapis.com",
        "gemini",
    )
    return any(s in msg for s in signals)


def _switch_gemini_to_backup(reason: str = "", job_id: str | None = None) -> bool:
    st = _gemini_key_settings()
    if st.get("active") != "primary" or not st.get("backup"):
        return False

    updates = {
        "GEMINI_ACTIVE_KEY": "backup",
    }
    _env_write_updates(ENV_PATH, updates, set())
    for k, v in updates.items():
        os.environ[k] = v
    _activate_gemini_key("backup")
    if job_id:
        log_event(DB_PATH, job_id, "WARN", f"Gemini key auto-switched to backup: {reason or 'runtime error'}")
    return True



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
    out["TUMBLR_CONSUMER_KEY"] = pick("TUMBLR_CONSUMER_KEY")
    out["TUMBLR_CONSUMER_SECRET"] = pick("TUMBLR_CONSUMER_SECRET")
    out["TUMBLR_BLOG_HOSTNAME"] = pick("TUMBLR_BLOG_HOSTNAME")
    out["TWITTER_BEARER_TOKEN"] = pick("TWITTER_BEARER_TOKEN", "X_BEARER_TOKEN")
    out["TWITTER_API_KEY"] = pick("TWITTER_API_KEY", "X_API_KEY", "TWITTER_CONSUMER_KEY")
    out["TWITTER_API_SECRET"] = pick("TWITTER_API_SECRET", "X_API_SECRET", "TWITTER_CONSUMER_SECRET")
    out["TWITTER_ACCESS_TOKEN"] = pick("TWITTER_ACCESS_TOKEN", "X_ACCESS_TOKEN")
    out["TWITTER_ACCESS_TOKEN_SECRET"] = pick("TWITTER_ACCESS_TOKEN_SECRET", "X_ACCESS_TOKEN_SECRET")
    out["GEMINI_API_KEY"] = pick("GEMINI_API_KEY", "GOOGLE_API_KEY")
    out["GEMINI_API_KEY_BACKUP"] = pick("GEMINI_API_KEY_BACKUP", "GOOGLE_API_KEY_BACKUP")
    active = (pick("GEMINI_ACTIVE_KEY") or "primary").lower()
    if active not in ("primary", "backup"):
        active = "primary"
    if active == "backup" and not out["GEMINI_API_KEY_BACKUP"]:
        active = "primary"
    out["GEMINI_ACTIVE_KEY"] = active
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
        a = f"Short answer: {short} Focus on practical execution, measurable KPIs, and consistent iteration."
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
    _mark_stale_seo_jobs(max_age_min=45)
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


@app.get("/seo", response_class=HTMLResponse)
def seo_dashboard(request: Request):
    if not _seo_enabled():
        raise HTTPException(status_code=404, detail="SEO module disabled")
    return templates.TemplateResponse(
        "seo.html",
        {
            "request": request,
            "build": utcnow_iso(),
        },
    )


@app.get("/api/seo/health")
def seo_health():
    if not _seo_enabled():
        return {"ok": False, "enabled": False}

    with db_connect(DB_PATH) as conn:
        entities = conn.execute("SELECT COUNT(1) FROM seo_entities").fetchone()[0]
        jobs = conn.execute("SELECT COUNT(1) FROM seo_jobs").fetchone()[0]
        by_status_rows = conn.execute(
            "SELECT status, COUNT(1) FROM seo_jobs GROUP BY status ORDER BY COUNT(1) DESC"
        ).fetchall()

    by_status = {r[0]: r[1] for r in by_status_rows}
    return {
        "ok": True,
        "enabled": True,
        "entities": entities,
        "jobs": jobs,
        "jobsByStatus": by_status,
    }


@app.get("/api/seo/entities")
def seo_entities_list(entity_type: str = "", limit: int = 200):
    if not _seo_enabled():
        raise HTTPException(status_code=404, detail="SEO module disabled")

    lim = max(1, min(int(limit or 200), 1000))
    q = """
        SELECT id, entity_type, entity_key, slug, title, country, region, grape, winery, year,
               abv, body, acidity, score, indexable, status, created_at, updated_at
        FROM seo_entities
    """
    params = []
    if entity_type:
        q += " WHERE entity_type = ?"
        params.append(entity_type.strip())
    q += " ORDER BY score DESC, updated_at DESC LIMIT ?"
    params.append(lim)

    with db_connect(DB_PATH) as conn:
        rows = conn.execute(q, tuple(params)).fetchall()

    # Build effective status from latest seo_jobs/jobs (instead of static seo_entities.status)
    entity_pairs = [(r[0], r[1]) for r in rows]
    latest_map = {}
    if entity_pairs:
        placeholders = ",".join(["?"] * len(entity_pairs))
        flat_ids = [eid for eid, _ in entity_pairs]
        with db_connect(DB_PATH) as conn:
            sj_rows = conn.execute(
                f"""
                SELECT sj.entity_id, sj.entity_type, sj.status, COALESCE(j.status,''), COALESCE(j.published_url,''), sj.updated_at
                FROM seo_jobs sj
                LEFT JOIN jobs j ON j.id = sj.id
                WHERE sj.entity_id IN ({placeholders})
                ORDER BY sj.updated_at DESC
                """,
                tuple(flat_ids),
            ).fetchall()
        for x in sj_rows:
            key = (x[0], x[1])
            if key not in latest_map:
                latest_map[key] = x

    def _effective_status(base_status: str, entity_id: int, entity_type: str) -> str:
        row = latest_map.get((entity_id, entity_type))
        if not row:
            return (base_status or "NEW").upper()
        seo_st = str(row[2] or "").upper()
        job_st = str(row[3] or "").upper()
        has_url = bool((row[4] or "").strip())
        if seo_st == "PUBLISHED" or job_st == "PUBLISHED" or has_url:
            return "PUBLISHED"
        if seo_st in ("PUBLISHING", "GENERATING", "QUEUED"):
            return seo_st
        if seo_st == "GENERATED" or job_st == "READY":
            return "GENERATED"
        if job_st in ("ERROR",):
            return "ERROR"
        return seo_st or job_st or (base_status or "NEW").upper()

    items = []
    for r in rows:
        items.append({
            "id": r[0],
            "entityType": r[1],
            "entityKey": r[2],
            "slug": r[3],
            "title": r[4],
            "country": r[5],
            "region": r[6],
            "grape": r[7],
            "winery": r[8],
            "year": r[9],
            "abv": r[10],
            "body": r[11],
            "acidity": r[12],
            "score": r[13],
            "indexable": bool(r[14]),
            "status": _effective_status(r[15], r[0], r[1]),
            "rawStatus": r[15],
            "createdAt": r[16],
            "updatedAt": r[17],
        })

    return {"ok": True, "items": items, "count": len(items)}


@app.get("/api/seo/jobs")
def seo_jobs_list(status: str = "", limit: int = 200):
    if not _seo_enabled():
        raise HTTPException(status_code=404, detail="SEO module disabled")

    try:
        _mark_stale_generating_jobs(max_age_min=60)
        _mark_stale_seo_jobs(max_age_min=60)
    except Exception:
        pass

    lim = max(1, min(int(limit or 200), 1000))
    q = """
        SELECT sj.id, sj.entity_id, sj.entity_type, sj.status, sj.error, sj.output_path, sj.created_at, sj.updated_at,
               j.status, j.slug, j.published_url, j.topic, j.title
        FROM seo_jobs sj
        LEFT JOIN jobs j ON j.id = sj.id
    """
    params = []
    if status:
        q += " WHERE sj.status = ?"
        params.append(status.strip())
    q += " ORDER BY sj.created_at DESC LIMIT ?"
    params.append(lim)

    with db_connect(DB_PATH) as conn:
        rows = conn.execute(q, tuple(params)).fetchall()

    items = []
    for r in rows:
        items.append({
            "id": r[0],
            "entityId": r[1],
            "entityType": r[2],
            "status": r[3],
            "error": r[4],
            "outputPath": r[5],
            "createdAt": r[6],
            "updatedAt": r[7],
            "jobStatus": r[8],
            "slug": r[9],
            "publishedUrl": r[10],
            "topic": r[11],
            "title": r[12],
        })

    return {"ok": True, "items": items, "count": len(items)}


def _seo_topic_for_entity(entity_type: str, title: str, country: str, region: str) -> tuple[str, str]:
    et = (entity_type or "").strip().lower()
    t = (title or "").strip()
    c = (country or "").strip()
    r = (region or "").strip()
    if et == "country":
        label = c or t
        topic = f"Create a unique country wine landing page for {label}"
        category = "Buying Guides"
    elif et == "region":
        label = r or t
        topic = f"Create a unique regional wine landing page for {label}"
        category = "Wine Regions"
    else:
        label = t or c or r or "wine"
        topic = f"Wine Guide: {label}"
        category = "Buying Guides"
    return topic, category


def _seo_run_job_pipeline(job_ids: list[str], action: str) -> None:
    for job_id in job_ids:
        if action in ("generate", "publish"):
            try:
                with db_connect(DB_PATH) as conn:
                    conn.execute(
                        "UPDATE seo_jobs SET status='GENERATING', error=NULL, updated_at=? WHERE id=?",
                        (utcnow_iso(), job_id),
                    )

                res = generate(job_id)
                generated_ok = True
                if isinstance(res, JSONResponse):
                    generated_ok = False
                    try:
                        payload = json.loads(res.body.decode("utf-8"))
                        generated_ok = bool(payload.get("success"))
                    except Exception:
                        generated_ok = False
                elif isinstance(res, dict):
                    generated_ok = bool(res.get("success", True))

                if not generated_ok:
                    with db_connect(DB_PATH) as conn:
                        conn.execute(
                            "UPDATE seo_jobs SET status='ERROR', error=?, updated_at=? WHERE id=?",
                            ("generate returned unsuccessful result", utcnow_iso(), job_id),
                        )
                    continue

                with db_connect(DB_PATH) as conn:
                    conn.execute(
                        "UPDATE seo_jobs SET status='GENERATED', error=NULL, updated_at=? WHERE id=?",
                        (utcnow_iso(), job_id),
                    )
            except Exception as gen_err:
                with db_connect(DB_PATH) as conn:
                    conn.execute(
                        "UPDATE seo_jobs SET status='ERROR', error=?, updated_at=? WHERE id=?",
                        (str(gen_err), utcnow_iso(), job_id),
                    )
                continue

        if action == "publish":
            try:
                with db_connect(DB_PATH) as conn:
                    conn.execute(
                        "UPDATE seo_jobs SET status='PUBLISHING', error=NULL, updated_at=? WHERE id=?",
                        (utcnow_iso(), job_id),
                    )

                pub = publish(job_id)
                pub_ok = True
                if isinstance(pub, dict):
                    pub_ok = bool(pub.get("success", False))

                if pub_ok:
                    output_path = None
                    with db_connect(DB_PATH) as conn:
                        row = conn.execute("SELECT published_url, slug FROM jobs WHERE id=?", (job_id,)).fetchone()
                        if row:
                            output_path = row[0] or (f"/blog/{row[1]}/" if row[1] else None)
                        conn.execute(
                            "UPDATE seo_jobs SET status='PUBLISHED', error=NULL, output_path=?, updated_at=? WHERE id=?",
                            (output_path, utcnow_iso(), job_id),
                        )
                else:
                    msg = str(pub.get("error") or "publish failed") if isinstance(pub, dict) else "publish failed"
                    with db_connect(DB_PATH) as conn:
                        conn.execute(
                            "UPDATE seo_jobs SET status='ERROR', error=?, updated_at=? WHERE id=?",
                            (msg, utcnow_iso(), job_id),
                        )
            except Exception as pub_err:
                with db_connect(DB_PATH) as conn:
                    conn.execute(
                        "UPDATE seo_jobs SET status='ERROR', error=?, updated_at=? WHERE id=?",
                        (str(pub_err), utcnow_iso(), job_id),
                    )


@app.post("/api/seo/pages/run")
async def seo_pages_run(request: Request):
    if not _seo_enabled():
        raise HTTPException(status_code=404, detail="SEO module disabled")

    body = {}
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}

    entity_type = str(body.get("entityType") or request.query_params.get("entityType") or "").strip().lower()
    if entity_type not in ("country", "region"):
        raise HTTPException(status_code=400, detail="entityType must be country|region")

    action = str(body.get("action") or request.query_params.get("action") or "queue").strip().lower()
    if action not in ("queue", "generate", "publish"):
        raise HTTPException(status_code=400, detail="action must be queue|generate|publish")

    try:
        limit = int(body.get("limit") or request.query_params.get("limit") or 10)
    except Exception:
        limit = 10
    limit = max(1, min(100, limit))

    try:
        min_score_raw = body.get("minScore") if body.get("minScore") is not None else request.query_params.get("minScore")
        min_score = float(min_score_raw if min_score_raw is not None else 0)
    except Exception:
        min_score = 0.0

    with db_connect(DB_PATH) as conn:
        region_country_filter = ""
        if entity_type == "region":
            region_country_filter = """
              AND LOWER(TRIM(COALESCE(e.country,''))) IN (
                SELECT DISTINCT LOWER(TRIM(COALESCE(c.country,'')))
                FROM seo_entities c
                JOIN seo_jobs sjc ON sjc.entity_id=c.id AND sjc.entity_type='country'
                LEFT JOIN jobs jc ON jc.id=sjc.id
                WHERE c.entity_type='country'
                  AND LOWER(TRIM(COALESCE(c.country,'')))<>''
                  AND (
                    UPPER(COALESCE(sjc.status,''))='PUBLISHED'
                    OR UPPER(COALESCE(jc.status,''))='PUBLISHED'
                    OR COALESCE(jc.published_url,'') LIKE '%/wine-countries/%'
                  )
              )
            """

        q = f"""
            SELECT e.id, e.entity_type, e.entity_key, e.slug, e.title, e.country, e.region, e.status, e.score
            FROM seo_entities e
            WHERE e.entity_type=?
              AND e.indexable=1
              AND e.score>=?
              {region_country_filter}
              AND NOT EXISTS (
                SELECT 1 FROM seo_jobs sj
                WHERE sj.entity_id=e.id AND sj.entity_type=e.entity_type
              )
            ORDER BY e.score DESC, e.updated_at DESC
            LIMIT ?
        """
        entities = conn.execute(q, (entity_type, min_score, limit)).fetchall()

    if not entities:
        return {
            "ok": True,
            "queued": 0,
            "started": 0,
            "skipped": 0,
            "errors": [],
            "message": "No SEO entities found for selected type.",
        }

    queued = 0
    skipped = 0
    errors = []
    job_ids = []
    process_ids = []

    for e in entities:
        entity_id, et, entity_key, slug, title, country, region, estatus, score = e
        seo_slug = (slug or "").strip() or _slugify(title or entity_key or f"{et}-{entity_id}")
        topic, category = _seo_topic_for_entity(et, title or "", country or "", region or "")

        try:
            with db_connect(DB_PATH) as conn:
                # 1) First, bind by SEO entity (stable), to avoid duplicate jobs for same country/region.
                ex = conn.execute(
                    """
                    SELECT sj.id
                    FROM seo_jobs sj
                    JOIN jobs j ON j.id = sj.id
                    WHERE sj.entity_id=? AND sj.entity_type=?
                    ORDER BY sj.updated_at DESC
                    LIMIT 1
                    """,
                    (entity_id, et),
                ).fetchone()

                # 2) Fallback by canonical slug (legacy compatibility).
                if not ex:
                    ex = conn.execute(
                        "SELECT id FROM jobs WHERE slug=? ORDER BY created_at DESC LIMIT 1",
                        (seo_slug,),
                    ).fetchone()

                should_process = False
                if ex:
                    job_id = ex[0]
                    skipped += 1
                else:
                    job_id = secrets.token_hex(12)
                    now = utcnow_iso()
                    conn.execute(
                        """
                        INSERT INTO jobs (
                            id, topic, slug, status, category, visibility,
                            product_mode, engagement_mode, lead_magnet_mode, created_at, updated_at
                        )
                        VALUES (?, ?, ?, 'NEW', ?, 'public', 0, 0, 0, ?, ?)
                        """,
                        (job_id, topic, seo_slug, category, now, now),
                    )
                    queued += 1
                    should_process = True

                now = utcnow_iso()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO seo_jobs (id, entity_id, entity_type, status, error, output_path, created_at, updated_at)
                    VALUES (
                        ?, ?, ?,
                        ?,
                        NULL,
                        COALESCE((SELECT output_path FROM seo_jobs WHERE id=?), NULL),
                        COALESCE((SELECT created_at FROM seo_jobs WHERE id=?), ?),
                        ?
                    )
                    """,
                    (job_id, entity_id, et, "QUEUED", job_id, job_id, now, now),
                )
        except Exception as q_err:
            errors.append({"entityId": entity_id, "step": "queue", "error": str(q_err)})
            continue

        job_ids.append(job_id)
        if action in ("generate", "publish") and should_process:
            process_ids.append(job_id)

    started = 0
    if process_ids:
        t = threading.Thread(target=_seo_run_job_pipeline, args=(process_ids, action), daemon=True)
        t.start()
        started = len(process_ids)

    return {
        "ok": True,
        "entityType": entity_type,
        "action": action,
        "queued": queued,
        "started": started,
        "skipped": skipped,
        "jobIds": job_ids,
        "startedJobIds": process_ids,
        "errors": errors,
    }




@app.get("/seo/preview/{job_id}", response_class=HTMLResponse)
def seo_preview(job_id: str):
    return preview(job_id)


@app.post("/api/seo/jobs/{job_id}/generate")
def seo_generate_job(job_id: str):
    if not _seo_enabled():
        raise HTTPException(status_code=404, detail="SEO module disabled")

    with db_connect(DB_PATH) as conn:
        exists = conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Job not found")
        seo_row = conn.execute("SELECT id FROM seo_jobs WHERE id=?", (job_id,)).fetchone()
        now = utcnow_iso()
        if seo_row:
            conn.execute("UPDATE seo_jobs SET status='GENERATING', error=NULL, updated_at=? WHERE id=?", (now, job_id))

    ok = True
    err = None
    try:
        res = generate(job_id)
        if isinstance(res, JSONResponse):
            ok = False
            try:
                payload = json.loads(res.body.decode("utf-8"))
                ok = bool(payload.get("success"))
                if not ok:
                    err = payload.get("error") or "generate failed"
            except Exception:
                err = "generate returned invalid response"
        elif isinstance(res, dict):
            ok = bool(res.get("success", True))
            if not ok:
                err = res.get("error") or "generate failed"
    except Exception as e:
        ok = False
        err = str(e) or "generate exception"

    with db_connect(DB_PATH) as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        job_status = row[0] if row else "UNKNOWN"
        if conn.execute("SELECT 1 FROM seo_jobs WHERE id=?", (job_id,)).fetchone():
            if ok:
                conn.execute("UPDATE seo_jobs SET status='GENERATED', error=NULL, updated_at=? WHERE id=?", (utcnow_iso(), job_id))
            else:
                conn.execute("UPDATE seo_jobs SET status='ERROR', error=?, updated_at=? WHERE id=?", (err or "generate failed", utcnow_iso(), job_id))

    if (not ok) and not err:
        err = "generate failed"
    return {"ok": ok, "jobId": job_id, "jobStatus": job_status, "error": err}


def _cleanup_partial_section_publish(section: str, slug: str, job_id: str, reason: str = "", locales: tuple[str, ...] | None = None) -> None:
    reason = (reason or "").strip()
    publish_locales = tuple(locales or LOCALES)
    files_to_remove = [os.path.join(LANDING_DIR, section, slug, "index.html")]
    for loc in publish_locales:
        files_to_remove.append(os.path.join(LANDING_DIR, loc, section, slug, "index.html"))

    for fp in files_to_remove:
        try:
            if os.path.exists(fp):
                os.remove(fp)
        except Exception:
            pass

    try:
        remove_sitemap_url(SITEMAP_PATH, url=_section_url(section, slug, "en"))
        for loc in publish_locales:
            remove_sitemap_url(_locale_sitemap_path(loc), url=_section_url(section, slug, loc))
    except Exception:
        pass

    try:
        _refresh_seo_section_indexes(section)
    except Exception as e:
        if job_id:
            log_event(DB_PATH, job_id, "WARN", f"Section index refresh after cleanup failed: {e}")

    if job_id:
        msg = f"Rolled back partial section publish for /{section}/{slug}/"
        if reason:
            msg += f" ({reason})"
        log_event(DB_PATH, job_id, "WARN", msg)


def _find_missing_section_locales(section: str, slug: str, locales: tuple[str, ...] | None = None) -> list[str]:
    missing: list[str] = []
    publish_locales = tuple(locales or LOCALES)
    en_path = os.path.join(LANDING_DIR, section, slug, "index.html")
    if not os.path.exists(en_path):
        missing.append("en")
    for loc in publish_locales:
        loc_path = os.path.join(LANDING_DIR, loc, section, slug, "index.html")
        if not os.path.exists(loc_path):
            missing.append(loc)
    return missing


@app.post("/api/seo/jobs/{job_id}/publish")
def seo_publish_job(job_id: str):
    if not _seo_enabled():
        raise HTTPException(status_code=404, detail="SEO module disabled")

    with db_connect(DB_PATH) as conn:
        exists = conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Job not found")
        if conn.execute("SELECT 1 FROM seo_jobs WHERE id=?", (job_id,)).fetchone():
            conn.execute("UPDATE seo_jobs SET status='PUBLISHING', error=NULL, updated_at=? WHERE id=?", (utcnow_iso(), job_id))

    ok = True
    err = None
    published_url = None
    try:
        pub = publish(job_id)
        if isinstance(pub, dict):
            ok = bool(pub.get("success", False))
            published_url = pub.get("url")
            if not ok:
                err = pub.get("error") or "publish failed"
    except Exception as e:
        ok = False
        err = str(e) or "publish exception"

    section = _seo_section_for_job(job_id)
    publish_locales = _seo_publish_locales()

    with db_connect(DB_PATH) as conn:
        row = conn.execute("SELECT status,published_url,slug FROM jobs WHERE id=?", (job_id,)).fetchone()
        job_status = row[0] if row else "UNKNOWN"
        published_url = (row[1] if row else None) or published_url
        slug = row[2] if row else None

    if section and slug:
        if not ok:
            _cleanup_partial_section_publish(section, slug, job_id, reason="publish failed", locales=publish_locales)
        else:
            missing_locales = _find_missing_section_locales(section, slug, locales=publish_locales)
            if missing_locales:
                ok = False
                err = f"incomplete localization publish: missing {','.join(missing_locales)}"
                _cleanup_partial_section_publish(section, slug, job_id, reason=err, locales=publish_locales)
                published_url = None
                job_status = "ERROR"

    if not ok:
        with db_connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE jobs SET status='ERROR', error=?, published_url=NULL, updated_at=? WHERE id=?",
                (err or "publish failed", utcnow_iso(), job_id),
            )

    output_path = published_url or (f"/blog/{slug}/" if slug else None)
    with db_connect(DB_PATH) as conn:
        if conn.execute("SELECT 1 FROM seo_jobs WHERE id=?", (job_id,)).fetchone():
            if ok:
                conn.execute("UPDATE seo_jobs SET status='PUBLISHED', error=NULL, output_path=?, updated_at=? WHERE id=?", (output_path, utcnow_iso(), job_id))
            else:
                conn.execute("UPDATE seo_jobs SET status='ERROR', error=?, output_path=NULL, updated_at=? WHERE id=?", (err or "publish failed", utcnow_iso(), job_id))

    if (not ok) and not err:
        err = "publish failed"
    return {"ok": ok, "jobId": job_id, "jobStatus": job_status, "publishedUrl": published_url, "error": err}

@app.get("/api/jobs")
def list_jobs():
    # Keep UI responsive: clear stale async statuses on polling.
    try:
        _mark_stale_social_postings(max_age_min=12)
        _mark_stale_generating_jobs(max_age_min=60)
        _mark_stale_seo_jobs(max_age_min=60)
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
                   tumblr_status, tumblr_post_url, tumblr_posted_at, tumblr_error,
                   product_mode, engagement_mode, lead_magnet_mode
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
                "tumblrStatus": r[28],
                "tumblrPostUrl": r[29],
                "tumblrPostedAt": r[30],
                "tumblrError": r[31],
                "productMode": bool(r[32]),
                "engagementMode": bool(r[33]),
                "leadMagnetMode": bool(r[34]),
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
    engagement_mode = bool(body.get("engagementMode", False))
    lead_magnet_mode = bool(body.get("leadMagnetMode", False))

    job_id = secrets.token_hex(12)
    now = utcnow_iso()

    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, topic, slug, status, category, hero_image, visibility, product_mode, engagement_mode, lead_magnet_mode, created_at, updated_at)
            VALUES (?, ?, ?, 'NEW', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, topic, slug, category, hero_image, visibility, 1 if product_mode else 0, 1 if engagement_mode else 0, 1 if lead_magnet_mode else 0, now, now),
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
            SELECT enabled, timezone, run_hour, direction, category_hint, per_run_limit, min_score, top_n, product_mode, engagement_mode, lead_magnet_mode, last_run_key, last_run_at
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
            "productMode": False,
            "engagementMode": False,
            "leadMagnetMode": False,
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
        "productMode": bool(r[8]),
        "engagementMode": bool(r[9]),
        "leadMagnetMode": bool(r[10]),
        "lastRunKey": r[11],
        "lastRunAt": r[12],
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
    product_mode: bool,
    engagement_mode: bool,
    lead_magnet_mode: bool,
    last_run_key: str | None = None,
    last_run_at: str | None = None,
) -> None:
    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE topic_discovery_settings
            SET enabled=?, timezone=?, run_hour=?, direction=?, category_hint=?,
                per_run_limit=?, min_score=?, top_n=?,
                product_mode=?, engagement_mode=?, lead_magnet_mode=?,
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
                1 if product_mode else 0,
                1 if engagement_mode else 0,
                1 if lead_magnet_mode else 0,
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

        cfg_product_mode = bool(cfg.get("productMode", False))
        cfg_engagement_mode = bool(cfg.get("engagementMode", False))
        cfg_lead_magnet_mode = bool(cfg.get("leadMagnetMode", False))

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
                    INSERT INTO jobs (id, topic, slug, status, category, visibility, product_mode, engagement_mode, lead_magnet_mode, created_at, updated_at)
                    VALUES (?, ?, ?, 'NEW', ?, 'public', ?, ?, ?, ?, ?)
                    """,
                    (job_id, topic, slug, category, 1 if cfg_product_mode else 0, 1 if cfg_engagement_mode else 0, 1 if cfg_lead_magnet_mode else 0, now, now),
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

    product_mode = bool(body.get("productMode", False))
    engagement_mode = bool(body.get("engagementMode", False))
    lead_magnet_mode = bool(body.get("leadMagnetMode", False))

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
        product_mode=product_mode,
        engagement_mode=engagement_mode,
        lead_magnet_mode=lead_magnet_mode,
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
        "productMode": body.get("productMode") if isinstance(body, dict) else None,
        "engagementMode": body.get("engagementMode") if isinstance(body, dict) else None,
        "leadMagnetMode": body.get("leadMagnetMode") if isinstance(body, dict) else None,
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
        raise HTTPException(status_code=404, detail=f"Not found: /blog/{slug}/.{hint}")

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
    hero = hero or ""
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

    log_event(DB_PATH, job_id, "READY", f"Imported from /blog/{slug}/")
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
            "SELECT id, topic, slug, status, category, hero_image, draft_html, product_mode, engagement_mode, lead_magnet_mode FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    _id, topic, slug, status, category, hero_image, draft_html, product_mode, engagement_mode, lead_magnet_mode = job

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
            _active_gemini_api_key()
            draft = generate_draft(
                topic=topic,
                existing_posts=existing,
                category=category,
                hero_image=hero_image,
                slug_hint=slug,
                source_html=_sanitize_source_html(draft_html) if (draft_html and status != "NEW") else None,
                product_mode=bool(product_mode),
                engagement_mode=bool(engagement_mode),
                lead_magnet_mode=bool(lead_magnet_mode),
                previous=draft,
                problems=problems if attempt > 1 else None,
            )
        except Exception as e:
            if _is_gemini_runtime_error(e) and _switch_gemini_to_backup(str(e), job_id=job_id):
                try:
                    _active_gemini_api_key()
                    draft = generate_draft(
                        topic=topic,
                        existing_posts=existing,
                        category=category,
                        hero_image=hero_image,
                        slug_hint=slug,
                        source_html=_sanitize_source_html(draft_html) if (draft_html and status != "NEW") else None,
                        product_mode=bool(product_mode),
                        engagement_mode=bool(engagement_mode),
                        lead_magnet_mode=bool(lead_magnet_mode),
                        previous=draft,
                        problems=problems if attempt > 1 else None,
                    )
                except Exception as e2:
                    msg = f"Generation failed: {e2}"
                    log_event(DB_PATH, job_id, "WARN", msg)
                    problems = [msg]
                    continue
            else:
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
        api_key = _active_gemini_api_key()
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
        if _is_gemini_runtime_error(e) and _switch_gemini_to_backup(str(e), job_id=job_id):
            try:
                api_key = _active_gemini_api_key()
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
                    log_event(DB_PATH, job_id, "INFO", f"Generated {len(generated)} image files for preview (backup key)")
            except Exception as e2:
                log_event(DB_PATH, job_id, "WARN", f"Image generation during generate failed: {e2}")
        else:
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
        hero_image=hero or "",
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

    section = _seo_section_for_job(job_id)
    is_section_page = bool(section)
    log_event(DB_PATH, job_id, "INFO", f"Publishing (visibility={visibility}, section={section or 'blog'})")

    if is_section_page:
        # SEO landings must have richer visual density than regular blog posts.
        content_html = _ensure_min_inline_placeholders(content_html, slug=slug, min_images=3)

    # Auto-generate hero + inline images into configured BLOG_DIR
    api_key = _active_gemini_api_key()
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
        if _is_gemini_runtime_error(e) and _switch_gemini_to_backup(str(e), job_id=job_id):
            try:
                api_key = _active_gemini_api_key()
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
                if hero and os.path.exists(os.path.join(BLOG_DIR, os.path.basename(hero))):
                    image_paths.append(os.path.join("blog", os.path.basename(hero)))
                log_event(DB_PATH, job_id, "INFO", "Image generation recovered with backup Gemini key")
            except Exception as e2:
                log_event(DB_PATH, job_id, "WARN", f"Image generation skipped/failed: {e2}")
        else:
            log_event(DB_PATH, job_id, "WARN", f"Image generation skipped/failed: {e}")

    if not is_section_page:
        hero_base = os.path.basename((hero or "").strip())
        if (not hero_base) or (hero_base.lower() == "logo.png"):
            msg = "Publish blocked: missing real hero image (logo fallback is forbidden)"
            log_event(DB_PATH, job_id, "ERROR", msg)
            with db_connect(DB_PATH) as conn:
                conn.execute("UPDATE jobs SET status='ERROR', error=?, updated_at=? WHERE id=?", (msg, utcnow_iso(), job_id))
            raise HTTPException(status_code=500, detail=msg)

    if is_section_page:
        html = _render_seo_section_html(
            title=title or "",
            description=desc or "",
            section=section,
            slug=slug,
            hero_image=hero or "",
            content_html=content_html,
            updated_at=updated_at or utcnow_iso(),
            locale="en",
            noindex=noindex,
        )
    else:
        content_html = _rewrite_blog_inline_img_srcs(content_html)
        html = render_post_html(
            blog_dir=BLOG_DIR,
            title=title or "",
            description=desc or "",
            category=cat or "Buying Guides",
            slug=slug,
            hero_image=hero or "",
            content_html=content_html,
            faq=faq,
            sources=sources,
            updated_at=updated_at or utcnow_iso(),
            noindex=noindex,
        )

    if is_section_page:
        section_path = f"/{section}/{slug}/"
        html = _apply_hreflang_block_for_path(html, section_path, "en")
        out_abs = os.path.join(LANDING_DIR, section, slug, "index.html")
        out_rel = os.path.join(section, slug, "index.html")
        url = _section_url(section, slug, "en")
    else:
        html = _apply_hreflang_block(html, slug, "en")
        out_abs = os.path.join(BLOG_DIR, f"{slug}.html")
        out_rel = os.path.join("blog", f"{slug}.html")
        url = f"{_site_origin()}/blog/{slug}/"

    os.makedirs(os.path.dirname(out_abs), exist_ok=True)
    with open(out_abs, "w", encoding="utf-8") as f:
        f.write(html)

    # Update indexes/sitemaps according to visibility.
    if is_section_page:
        if noindex:
            remove_sitemap_url(SITEMAP_PATH, url=url)
        else:
            upsert_sitemap_url(SITEMAP_PATH, url=url)
        paths = [out_rel, "sitemap-en.xml"] + (image_paths or [])
    else:
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
                hero_image=os.path.basename(hero) if hero else "",
            )
            upsert_sitemap_url(SITEMAP_PATH, url=url)

        _rebuild_blog_feed_from_index(os.path.join(BLOG_DIR, "index.html"), os.path.join(BLOG_DIR, "feed.json"))
        paths = [out_rel, os.path.join("blog", "index.html"), os.path.join("blog", "feed.json"), "sitemap-en.xml"] + (image_paths or [])

    # Publish localized versions (ru/es/de/fr) in the same publish action.
    text_api_key = _active_gemini_api_key()
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
    locale_retry_attempts = max(1, min(3, int(os.environ.get("LOCALE_PUBLISH_RETRY_ATTEMPTS", "2") or "2")))
    publish_locales = _seo_publish_locales()
    if job_id:
        log_event(DB_PATH, job_id, "INFO", f"Publishing locales: {','.join(publish_locales)}")
    for loc in publish_locales:
        _ensure_sitemap(_locale_sitemap_path(loc))
        loc_sitemap = _locale_sitemap_path(loc)

        if is_section_page:
            loc_out_abs = os.path.join(LANDING_DIR, loc, section, slug, "index.html")
            loc_out_rel = os.path.join(loc, section, slug, "index.html")
            loc_url = _section_url(section, slug, loc)
            loc_blog_dir = None
            loc_idx_rel = None
        else:
            loc_blog_dir = _locale_blog_dir(loc)
            loc_out_abs = os.path.join(loc_blog_dir, f"{slug}.html")
            loc_out_rel = os.path.join(loc, "blog", f"{slug}.html")
            loc_url = f"{_site_origin()}/{loc}/blog/{slug}/"
            loc_idx_rel = os.path.join(loc, "blog", "index.html")

        loc_title = title or ""
        loc_desc = desc or ""
        loc_cat = cat or "Buying Guides"
        loc_content = content_html
        loc_faq = faq
        loc_cat = _localize_category(_pick_category_from_content(topic=topic, title=loc_title, description=loc_desc, category_hint=cat, content_html=loc_content), loc)

        if text_api_key:
            tr = None
            tr_error = None
            for attempt in range(1, locale_retry_attempts + 1):
                try:
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
                    if attempt > 1:
                        log_event(DB_PATH, job_id, "INFO", f"Localization {loc} succeeded on retry {attempt}/{locale_retry_attempts}")
                    tr_error = None
                    break
                except Exception as tr_err:
                    tr_error = tr_err
                    switched = False
                    if _is_gemini_runtime_error(tr_err):
                        switched = _switch_gemini_to_backup(str(tr_err), job_id=job_id)
                        if switched:
                            text_api_key = _active_gemini_api_key()
                            log_event(DB_PATH, job_id, "INFO", f"Localization {loc}: switched Gemini key after runtime error")

                    if attempt < locale_retry_attempts:
                        log_event(DB_PATH, job_id, "WARN", f"Localization {loc} attempt {attempt}/{locale_retry_attempts} failed: {tr_err}")
                        time.sleep(min(2.0, 0.5 * attempt))
                        continue

                    if switched:
                        log_event(DB_PATH, job_id, "WARN", f"Localization {loc} failed after key switch: {tr_err}")
                    raise

            if tr is None:
                raise tr_error or HTTPException(status_code=500, detail=f"Localization {loc} failed")

            loc_title = tr["title"]
            loc_desc = tr["description"]
            loc_cat = _localize_category(_pick_category_from_content(topic=topic, title=loc_title, description=loc_desc, category_hint=tr.get("category"), content_html=loc_content), loc)
            loc_content = tr["contentHtml"]
            loc_faq = tr["faq"]
        else:
            raise HTTPException(status_code=500, detail=f"Localization {loc} failed: no GEMINI_API_KEY/GOOGLE_API_KEY")

        if is_section_page:
            loc_html = _render_seo_section_html(
                title=loc_title,
                description=loc_desc,
                section=section,
                slug=slug,
                hero_image=hero or "",
                content_html=loc_content,
                updated_at=updated_at or utcnow_iso(),
                locale=loc,
                noindex=noindex,
            )
        else:
            loc_content = _rewrite_blog_inline_img_srcs(loc_content)
            loc_html = render_post_html(
                blog_dir=BLOG_DIR,
                title=loc_title,
                description=loc_desc,
                category=loc_cat,
                slug=slug,
                hero_image=hero or "",
                content_html=loc_content,
                faq=loc_faq,
                sources=sources,
                updated_at=updated_at or utcnow_iso(),
                noindex=noindex,
                toc_title=toc_titles.get(loc, "On this page"),
            )
            loc_html = re.sub(r'(?is)<html\s+lang="[^"]+"', f'<html lang="{loc}"', loc_html, count=1)
        if is_section_page:
            loc_html = _apply_hreflang_block_for_path(loc_html, f"/{section}/{slug}/", loc)
        else:
            loc_html = _apply_hreflang_block(loc_html, slug, loc)

        os.makedirs(os.path.dirname(loc_out_abs), exist_ok=True)
        with open(loc_out_abs, "w", encoding="utf-8") as f:
            f.write(loc_html)

        if is_section_page:
            if noindex:
                remove_sitemap_url(loc_sitemap, url=loc_url)
            else:
                upsert_sitemap_url(loc_sitemap, url=loc_url)
            paths.extend([loc_out_rel, f"sitemap-{loc}.xml"])
        else:
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
                    hero_image=(f"/blog/{os.path.basename(hero)}" if hero else ""),
                    href_prefix=f"/{loc}/blog",
                    marker_prefix=f"FACTORY-{loc.upper()}",
                )
                upsert_sitemap_url(loc_sitemap, url=loc_url)

            _rebuild_blog_feed_from_index(os.path.join(loc_blog_dir, "index.html"), os.path.join(loc_blog_dir, "feed.json"))
            paths.extend([loc_out_rel, loc_idx_rel, os.path.join(loc, "blog", "feed.json"), f"sitemap-{loc}.xml"])

    # Keep /wine-countries/ and /wine-regions/ index pages in sync with newly published section pages.
    if is_section_page:
        try:
            paths.extend(_refresh_seo_section_indexes(section))
        except Exception as e:
            log_event(DB_PATH, job_id, "WARN", f"Section index refresh failed: {e}")

        # Rebuild blog feeds as combined home feed (blog + seo pages), so homepage hero sees new pages immediately.
        try:
            _rebuild_blog_feed_from_index(os.path.join(BLOG_DIR, "index.html"), os.path.join(BLOG_DIR, "feed.json"))
            paths.append(os.path.join("blog", "feed.json"))
            for loc in LOCALES:
                loc_blog_dir = os.path.join(LANDING_DIR, loc, "blog")
                _rebuild_blog_feed_from_index(os.path.join(loc_blog_dir, "index.html"), os.path.join(loc_blog_dir, "feed.json"))
                paths.append(os.path.join(loc, "blog", "feed.json"))
        except Exception as e:
            log_event(DB_PATH, job_id, "WARN", f"Combined feed refresh failed: {e}")

    # Run global image optimization only for regular blog posts.
    # For SEO country/region pages this is very expensive and not required per publish.
    if not is_section_page:
        _optimize_site_images()

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
            (url, (os.path.basename(hero) if hero else ""), content_html, utcnow_iso(), job_id),
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
                   tumblr_status, tumblr_post_url, tumblr_posted_at, tumblr_error,
                   product_mode, engagement_mode, lead_magnet_mode
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
            "tumblrStatus": r[28],
            "tumblrPostUrl": r[29],
            "tumblrPostedAt": r[30],
            "tumblrError": r[31],
            "productMode": bool(r[32]),
            "engagementMode": bool(r[33]),
            "leadMagnetMode": bool(r[34]),
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
    if isinstance(body.get("engagementMode"), bool):
        set_if("engagement_mode", 1 if body.get("engagementMode") else 0)
    if isinstance(body.get("leadMagnetMode"), bool):
        set_if("lead_magnet_mode", 1 if body.get("leadMagnetMode") else 0)

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

    section = _seo_section_for_job(job_id)
    is_section_page = bool(section)

    if is_section_page:
        out_rel = os.path.join(section, slug, "index.html")
        out_abs = os.path.join(LANDING_DIR, section, slug, "index.html")
        url = _section_url(section, slug, "en")
        remove_paths = [out_rel]
        add_paths = ["sitemap-en.xml"]

        if os.path.exists(out_abs):
            os.remove(out_abs)

        remove_sitemap_url(SITEMAP_PATH, url=url)

        for loc in LOCALES:
            loc_abs = os.path.join(root, loc, section, slug, "index.html")
            loc_rel = os.path.join(loc, section, slug, "index.html")
            loc_url = _section_url(section, slug, loc)
            if os.path.exists(loc_abs):
                os.remove(loc_abs)
            remove_sitemap_url(_locale_sitemap_path(loc), url=loc_url)
            remove_paths.append(loc_rel)
            add_paths.append(f"sitemap-{loc}.xml")
    else:
        out_rel = os.path.join("blog", f"{slug}.html")
        out_abs = os.path.join(BLOG_DIR, f"{slug}.html")
        url = f"{_site_origin()}/blog/{slug}/"
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
            loc_url = f"{_site_origin()}/{loc}/blog/{slug}/"
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

    section = _seo_section_for_job(job_id)
    is_section_page = bool(section)

    if slug:
        if is_section_page:
            out_rel = os.path.join(section, slug, "index.html")
            out_abs = os.path.join(LANDING_DIR, section, slug, "index.html")
            url = _section_url(section, slug, "en")

            if os.path.exists(out_abs):
                os.remove(out_abs)
                removed_paths.append(out_rel)

            remove_sitemap_url(SITEMAP_PATH, url=url)

            for loc in LOCALES:
                loc_abs = os.path.join(root, loc, section, slug, "index.html")
                loc_rel = os.path.join(loc, section, slug, "index.html")
                loc_url = _section_url(section, slug, loc)

                if os.path.exists(loc_abs):
                    os.remove(loc_abs)
                    removed_paths.append(loc_rel)

                remove_sitemap_url(_locale_sitemap_path(loc), url=loc_url)
        else:
            out_rel = os.path.join("blog", f"{slug}.html")
            out_abs = os.path.join(BLOG_DIR, f"{slug}.html")
            url = f"{_site_origin()}/blog/{slug}/"

            if os.path.exists(out_abs):
                os.remove(out_abs)
                removed_paths.append(out_rel)

            remove_blog_index_card(BLOG_DIR, slug=slug)
            remove_sitemap_url(SITEMAP_PATH, url=url)

            for loc in LOCALES:
                loc_blog_dir = _locale_blog_dir(loc)
                loc_abs = os.path.join(loc_blog_dir, f"{slug}.html")
                loc_rel = os.path.join(loc, "blog", f"{slug}.html")
                loc_url = f"{_site_origin()}/{loc}/blog/{slug}/"

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
        if is_section_page:
            add_paths = ["sitemap-en.xml"]
            for loc in LOCALES:
                add_paths.append(f"sitemap-{loc}.xml")
        else:
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
        conn.execute("DELETE FROM seo_jobs WHERE id=?", (job_id,))

    return {"success": True}


# --- Auto Publish Scheduler ---

def _ap_read_settings() -> dict[str, Any]:
    with db_connect(DB_PATH) as conn:
        r = conn.execute(
            """
            SELECT enabled, times_per_day, channels_json, timezone, start_hour, end_hour, linkedin_include_link, telegram_include_link, tumblr_include_link, last_slot_key, last_run_at
            FROM autopublish_settings
            WHERE id=1
            """
        ).fetchone()

    if not r:
        return {
            "enabled": False,
            "times_per_day": 3,
            "channels": ["linkedin", "telegram", "twitter", "tumblr"],
            "timezone": "UTC",
            "start_hour": 9,
            "end_hour": 21,
            "linkedin_include_link": False,
            "telegram_include_link": False,
            "tumblr_include_link": False,
            "last_slot_key": None,
            "last_run_at": None,
        }

    channels = []
    try:
        parsed = json.loads(r[2] or "[]")
        if isinstance(parsed, list):
            channels = [str(x).strip().lower() for x in parsed if str(x).strip().lower() in ("linkedin", "telegram", "twitter", "tumblr")]
    except Exception:
        channels = []
    return {
        "enabled": bool(r[0]),
        "times_per_day": int(r[1] or 3),
        "channels": channels,
        "timezone": (r[3] or "UTC").strip() or "UTC",
        "start_hour": int(r[4] if r[4] is not None else 9),
        "end_hour": int(r[5] if r[5] is not None else 21),
        "linkedin_include_link": bool(r[6]),
        "telegram_include_link": bool(r[7]),
        "tumblr_include_link": bool(r[8]),
        "last_slot_key": r[9],
        "last_run_at": r[10],
    }


def _ap_write_settings(*, enabled: bool, times_per_day: int, channels: list[str], timezone_name: str, start_hour: int, end_hour: int, linkedin_include_link: bool = False, telegram_include_link: bool = False, tumblr_include_link: bool = False, last_slot_key: str | None = None, last_run_at: str | None = None) -> None:
    ch_json = json.dumps(channels)
    with db_connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE autopublish_settings
            SET enabled=?, times_per_day=?, channels_json=?, timezone=?, start_hour=?, end_hour=?,
                linkedin_include_link=?, telegram_include_link=?, tumblr_include_link=?,
                last_slot_key=COALESCE(?, last_slot_key),
                last_run_at=COALESCE(?, last_run_at),
                updated_at=?
            WHERE id=1
            """,
            (1 if enabled else 0, times_per_day, ch_json, timezone_name, start_hour, end_hour, 1 if linkedin_include_link else 0, 1 if telegram_include_link else 0, 1 if tumblr_include_link else 0, last_slot_key, last_run_at, utcnow_iso()),
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
            result = {"success": False, "status": "NO_CHANNELS", "message": "no autopublish channels selected"}
            _ap_log_run(started, utcnow_iso(), trigger, None, "NO_CHANNELS", result)
            return result

        with db_connect(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT id, slug, published_url,
                       COALESCE(linkedin_status, ''),
                       COALESCE(telegram_status, ''),
                       COALESCE(twitter_status, ''),
                       COALESCE(tumblr_status, '')
                FROM jobs
                WHERE status='READY'
                ORDER BY created_at ASC
                LIMIT 300
                """
            ).fetchall()

        selected = None
        for r in rows:
            jid, slug, published_url, li_st, tg_st, tw_st, tu_st = r
            st_map = {
                "linkedin": (li_st or "").upper().strip(),
                "telegram": (tg_st or "").upper().strip(),
                "twitter": (tw_st or "").upper().strip(),
                "tumblr": (tu_st or "").upper().strip(),
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
                    "COALESCE(twitter_status, ''), "
                    "COALESCE(tumblr_status, '') "
                    "FROM jobs WHERE status='READY' ORDER BY created_at ASC LIMIT 300"
                )
                with db_connect(DB_PATH) as conn:
                    rows = conn.execute(sql).fetchall()

                for r in rows:
                    jid, slug, published_url, li_st, tg_st, tw_st, tu_st = r
                    st_map = {
                        'linkedin': (li_st or '').upper().strip(),
                        'telegram': (tg_st or '').upper().strip(),
                        'twitter': (tw_st or '').upper().strip(),
                        'tumblr': (tu_st or '').upper().strip(),
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
                "SELECT COALESCE(linkedin_status,''), COALESCE(telegram_status,''), COALESCE(twitter_status,''), COALESCE(tumblr_status,'') FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        st_map = {
            "linkedin": (st[0] if st else "").upper().strip(),
            "telegram": (st[1] if st else "").upper().strip(),
            "twitter": (st[2] if st else "").upper().strip(),
            "tumblr": (st[3] if st else "").upper().strip(),
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
                    twitter_publish(job_id, {"includeLink": bool(settings.get("telegram_include_link"))})
                elif ch == "tumblr":
                    tumblr_publish(job_id, {"includeLink": bool(settings.get("tumblr_include_link"))})
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
                            channels=list(st.get("channels") or ["linkedin", "telegram", "twitter", "tumblr"]),
                            timezone_name=(st.get("timezone") or "UTC"),
                            start_hour=int(st.get("start_hour") or 9),
                            end_hour=int(st.get("end_hour") or 21),
                            linkedin_include_link=bool(st.get("linkedin_include_link")),
                            telegram_include_link=bool(st.get("telegram_include_link")),
                            tumblr_include_link=bool(st.get("tumblr_include_link")),
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
                            product_mode=bool(td.get("productMode", False)),
                            engagement_mode=bool(td.get("engagementMode", False)),
                            lead_magnet_mode=bool(td.get("leadMagnetMode", False)),
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

    channels = body.get("channels", [])
    if not isinstance(channels, list):
        raise HTTPException(status_code=400, detail="channels must be list")
    channels = [str(x).strip().lower() for x in channels if str(x).strip().lower() in ("linkedin", "telegram", "twitter", "tumblr")]

    timezone_name = (body.get("timezone") or "UTC").strip() or "UTC"
    linkedin_include_link = bool(body.get("linkedinIncludeLink", body.get("linkedin_include_link", False)))
    telegram_include_link = bool(body.get("telegramIncludeLink", body.get("telegram_include_link", False)))
    tumblr_include_link = bool(body.get("tumblrIncludeLink", body.get("tumblr_include_link", False)))
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
        if key == "GEMINI_ACTIVE_KEY":
            vv = val.lower()
            if vv and vv not in ("primary", "backup"):
                raise HTTPException(status_code=400, detail="GEMINI_ACTIVE_KEY must be primary|backup")
            val = vv
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
    if "GEMINI_API_KEY_BACKUP" in updates:
        updates["GOOGLE_API_KEY_BACKUP"] = updates["GEMINI_API_KEY_BACKUP"]
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
    if "GEMINI_API_KEY_BACKUP" in clears:
        clears.add("GOOGLE_API_KEY_BACKUP")
    if "GEMINI_TEXT_MODEL" in clears:
        clears.add("GEMINI_MODEL_TEXT")
    if "GEMINI_IMAGE_MODEL" in clears:
        clears.add("GEMINI_MODEL_IMAGE")

    # clear has priority over update when both are provided
    for k in list(updates.keys()):
        if k in clears:
            updates.pop(k, None)

    # Normalize Gemini active key and keep effective env key pinned to active selection.
    current = _gemini_key_settings()
    final_primary = updates.get("GEMINI_API_KEY", current.get("primary", ""))
    final_backup = updates.get("GEMINI_API_KEY_BACKUP", current.get("backup", ""))
    if "GEMINI_API_KEY" in clears:
        final_primary = ""
    if "GEMINI_API_KEY_BACKUP" in clears:
        final_backup = ""
    final_active = (updates.get("GEMINI_ACTIVE_KEY") or current.get("active") or "primary").lower()
    if final_active not in ("primary", "backup"):
        final_active = "primary"
    if final_active == "backup" and not final_backup:
        raise HTTPException(status_code=400, detail="Cannot switch to backup: GEMINI_API_KEY_BACKUP is empty")

    updates["GEMINI_ACTIVE_KEY"] = final_active

    _env_write_updates(ENV_PATH, updates, clears)

    for k in clears:
        os.environ.pop(k, None)
    for k, v in updates.items():
        os.environ[k] = v
    _activate_gemini_key(final_active)

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
    url = (published_url or f"{_site_origin()}/blog/{slug}/").strip()


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


# --- Tumblr integration ---

@app.get("/api/tumblr/status")
def tumblr_status():
    auth = db_get_tumblr(DB_PATH) or {}
    env_blog = (os.environ.get("TUMBLR_BLOG_HOSTNAME") or "").strip()
    return {
        "success": True,
        "connected": bool((auth.get("oauth_token") or "").strip() and (auth.get("oauth_token_secret") or "").strip()),
        "blogHostname": (env_blog or (auth.get("blog_hostname") or "")).strip() or None,
    }


@app.post("/api/tumblr/disconnect")
def tumblr_disconnect():
    db_clear_tumblr(DB_PATH)
    return {"success": True}


@app.get("/tumblr/connect")
def tumblr_connect():
    consumer_key = (os.environ.get("TUMBLR_CONSUMER_KEY") or "").strip()
    consumer_secret = (os.environ.get("TUMBLR_CONSUMER_SECRET") or "").strip()
    callback_base = (os.environ.get("TUMBLR_REDIRECT_URI") or "").strip() or (_site_origin() + "/factory/tumblr/callback")

    if not consumer_key or not consumer_secret:
        raise HTTPException(status_code=500, detail="Missing TUMBLR_CONSUMER_KEY / TUMBLR_CONSUMER_SECRET")

    state = secrets.token_urlsafe(24)
    db_create_state(DB_PATH, provider="tumblr", state=state)

    sep = '&' if ('?' in callback_base) else '?'
    callback_url = f"{callback_base}{sep}state={state}"

    token_data = tumblr_request_token(consumer_key=consumer_key, consumer_secret=consumer_secret, callback_url=callback_url)
    oauth_token = (token_data.get("oauth_token") or "").strip()
    oauth_token_secret = (token_data.get("oauth_token_secret") or "").strip()
    if not oauth_token or not oauth_token_secret:
        raise HTTPException(status_code=400, detail="Tumblr request_token failed")

    db_put_tumblr_temp(DB_PATH, oauth_token=oauth_token, oauth_token_secret=oauth_token_secret, state=state)
    return RedirectResponse(url=tumblr_build_auth_url(oauth_token), status_code=302)


@app.get("/tumblr/callback", response_class=HTMLResponse)
def tumblr_callback(oauth_token: str | None = None, oauth_verifier: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return HTMLResponse(content=f"<h3>Tumblr OAuth error: {error}</h3><p><a href='/factory/'>Back to Factory</a></p>", status_code=400)

    if not oauth_token or not oauth_verifier:
        raise HTTPException(status_code=400, detail="Missing oauth_token/oauth_verifier")

    temp = db_pop_tumblr_temp(DB_PATH, oauth_token=oauth_token)
    if not temp:
        raise HTTPException(status_code=400, detail="Unknown/expired oauth token")

    cb_state = (state or temp.get("state") or "").strip()
    if not cb_state or not db_consume_state(DB_PATH, provider="tumblr", state=cb_state, max_age_min=20):
        raise HTTPException(status_code=400, detail="Invalid/expired state")

    consumer_key = (os.environ.get("TUMBLR_CONSUMER_KEY") or "").strip()
    consumer_secret = (os.environ.get("TUMBLR_CONSUMER_SECRET") or "").strip()
    if not consumer_key or not consumer_secret:
        raise HTTPException(status_code=500, detail="Missing TUMBLR_CONSUMER_KEY / TUMBLR_CONSUMER_SECRET")

    access = tumblr_exchange_access_token(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        request_token=oauth_token,
        request_token_secret=(temp.get("oauth_token_secret") or ""),
        oauth_verifier=oauth_verifier,
    )

    access_token = (access.get("oauth_token") or "").strip()
    access_secret = (access.get("oauth_token_secret") or "").strip()
    if not access_token or not access_secret:
        raise HTTPException(status_code=400, detail="Tumblr access_token exchange failed")

    blog_hostname = ((os.environ.get("TUMBLR_BLOG_HOSTNAME") or "").strip() or (access.get("blog_hostname") or "").strip() or None)

    db_set_tumblr(DB_PATH, oauth_token=access_token, oauth_token_secret=access_secret, blog_hostname=blog_hostname)
    return RedirectResponse(url="/factory/", status_code=302)


@app.post("/api/jobs/{job_id}/tumblr/publish")
def tumblr_publish(job_id: str, payload: dict[str, Any] | None = None):
    payload = payload or {}

    consumer_key = (os.environ.get("TUMBLR_CONSUMER_KEY") or "").strip()
    consumer_secret = (os.environ.get("TUMBLR_CONSUMER_SECRET") or "").strip()
    if not consumer_key or not consumer_secret:
        raise HTTPException(status_code=500, detail="Missing TUMBLR_CONSUMER_KEY / TUMBLR_CONSUMER_SECRET")

    include_link = bool(payload.get("includeLink", True))

    auth = db_get_tumblr(DB_PATH) or {}
    oauth_token = (auth.get("oauth_token") or "").strip()
    oauth_token_secret = (auth.get("oauth_token_secret") or "").strip()
    blog_hostname = ((os.environ.get("TUMBLR_BLOG_HOSTNAME") or "").strip() or (auth.get("blog_hostname") or "").strip())

    if not oauth_token or not oauth_token_secret:
        raise HTTPException(status_code=400, detail="Tumblr not connected")
    if not blog_hostname:
        raise HTTPException(status_code=400, detail="Set TUMBLR_BLOG_HOSTNAME in social settings")

    with db_connect(DB_PATH) as conn:
        job = conn.execute(
            "SELECT topic, slug, title, description, draft_html, status, published_url, tumblr_status FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    topic, slug, title, description, draft_html, status, published_url, tu_status = job

    if tu_status == "POSTING":
        return {"success": True, "status": "POSTING"}

    if not slug:
        raise HTTPException(status_code=400, detail="Missing slug")

    url = (published_url or f"{_site_origin()}/blog/{slug}/").strip()

    with db_connect(DB_PATH) as conn:
        conn.execute("UPDATE jobs SET tumblr_status='POSTING', tumblr_error=NULL, updated_at=? WHERE id=?", (utcnow_iso(), job_id))

    log_event(DB_PATH, job_id, "INFO", f"Tumblr posting started: {blog_hostname}")

    import threading

    def _worker():
        try:
            post_html = build_tumblr_post_html(
                title=title or topic or slug,
                description=description or "",
                content_html=draft_html or "",
                url=url,
                include_link=include_link,
            )

            out = tumblr_publish_text_post(
                consumer_key=consumer_key,
                consumer_secret=consumer_secret,
                oauth_token=oauth_token,
                oauth_token_secret=oauth_token_secret,
                blog_hostname=blog_hostname,
                title=(title or topic or slug),
                body_html=post_html,
                tags=[(topic or ""), (slug or "")],
            )

            post_id = (out.get("post_id") or "").strip()
            post_url = (out.get("post_url") or "").strip()

            with db_connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE jobs SET tumblr_status='POSTED', tumblr_post_url=?, tumblr_posted_at=?, tumblr_error=NULL, updated_at=? WHERE id=?",
                    (post_url or post_id or None, utcnow_iso(), utcnow_iso(), job_id),
                )

            _save_social_post(job_id=job_id, channel="tumblr", content_text=post_html, content_json=out, remote_url=post_url or post_id or None, status="POSTED")
            log_event(DB_PATH, job_id, "READY", "Posted to Tumblr")
        except Exception as e:
            msg = f"Tumblr publish failed: {e}"
            with db_connect(DB_PATH) as conn:
                conn.execute("UPDATE jobs SET tumblr_status='ERROR', tumblr_error=?, updated_at=? WHERE id=?", (msg, utcnow_iso(), job_id))
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

    url = (published_url or f"{_site_origin()}/blog/{slug}/").strip()

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
            rejected = getattr(e, "rejected_text", "") or ""
            if rejected:
                compact = " ".join(rejected.split())[:420]
                msg = f"{msg} | rejected: {compact}"
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

    url = (published_url or f"{_site_origin()}/blog/{slug}/").strip()
    include_link = bool(payload.get("includeLink", False))

    with db_connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE jobs SET twitter_status='POSTING', twitter_error=NULL, updated_at=? WHERE id=?",
            (utcnow_iso(), job_id),
        )
    log_event(DB_PATH, job_id, "INFO", "X/Twitter posting started")

    import threading

    def _worker():
        try:
            tweets = build_twitter_thread_ru(
                title=title or topic,
                description=description or "",
                content_html=draft_html or "",
                url=url,
                include_link=include_link,
                max_posts=1,
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
            log_event(DB_PATH, job_id, "READY", "Posted to X/Twitter")
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

    if "SEO_PUBLISH_LOCALES" in updates:
        raw_publish_locales = updates.get("SEO_PUBLISH_LOCALES", "")
        publish_locales = _normalize_publish_locales(raw_publish_locales)
        if raw_publish_locales and not publish_locales:
            raise HTTPException(status_code=400, detail="SEO_PUBLISH_LOCALES must contain only ru, es, de, fr")
        publish_locales_csv = ",".join(publish_locales)
        updates["SEO_PUBLISH_LOCALES"] = publish_locales_csv
        os.environ["SEO_PUBLISH_LOCALES"] = publish_locales_csv
        _env_write_updates(ENV_PATH, {"SEO_PUBLISH_LOCALES": publish_locales_csv}, set())

    out = {"success": True, "values": {k: (os.environ.get(k) or "").strip() for k in SITE_ENV_KEYS}}
    if theme_result is not None:
        out["theme_apply"] = theme_result
    if langs_result is not None:
        out["languages_apply"] = langs_result
    if pulse_result is not None:
        out["pulse_apply"] = pulse_result
    return out


def _safe_json_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    return {}


def _extract_year(text: str) -> str:
    m = re.search(r"\b(19|20)\d{2}\b", text or "")
    return m.group(0) if m else ""


@app.post("/api/detect-wine")
async def api_detect_wine(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    image_url = str((body or {}).get("imageUrl") or "").strip()
    page_url = str((body or {}).get("pageUrl") or "").strip()
    text_hint = str((body or {}).get("textHint") or "").strip()

    api_key = (_active_gemini_api_key() or "").strip()
    if not api_key:
        return JSONResponse(status_code=500, content={"error": "GEMINI_API_KEY is not configured."})

    prompt = "\n".join([
        "You extract wine fields for product matching.",
        "Return JSON only with keys:",
        "wine_name, grape, country, region, year, confidence, notes",
        "Rules:",
        "- grape should be canonical style (e.g. Chardonnay, Pinot Noir, Cabernet Sauvignon).",
        "- year must be 4 digits if known, else empty string.",
        "- if unknown keep empty string.",
        f"Page URL: {page_url}",
        f"Text hint: {text_hint}",
        f"Image URL: {image_url}",
    ])

    parts = [{"text": prompt}]

    if image_url:
        try:
            with urllib.request.urlopen(image_url, timeout=8) as r:
                img_bytes = r.read()
                content_type = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip() or "image/jpeg"
            import base64
            parts.append({
                "inline_data": {
                    "mime_type": content_type,
                    "data": base64.b64encode(img_bytes).decode("ascii")
                }
            })
        except Exception:
            pass

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1
        }
    }

    preferred_model = (
        os.environ.get("GEMINI_TEXT_MODEL")
        or os.environ.get("GEMINI_MODEL_TEXT")
        or "gemini-2.5-flash"
    ).strip()
    models_to_try = [preferred_model, "gemini-2.5-flash", "gemini-2.0-flash-lite"]

    def _request_with_key(key: str):
        data_local = None
        last_local = None
        for model in models_to_try:
            if not model:
                continue
            gemini_url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + model
                + ":generateContent?key="
                + urllib.parse.quote(key)
            )
            try:
                req = urllib.request.Request(
                    gemini_url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                data_local = json.loads(raw)
                break
            except Exception as e:
                last_local = e
                continue
        return data_local, last_local

    data, last_error = _request_with_key(api_key)
    if data is None and _is_gemini_runtime_error(last_error) and _switch_gemini_to_backup(str(last_error)):
        api_key = (_active_gemini_api_key() or "").strip()
        data, last_error = _request_with_key(api_key)

    if data is None:
        return JSONResponse(status_code=502, content={"error": f"Gemini request failed: {last_error}"})

    model_text = ""
    try:
        model_text = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
    except Exception:
        model_text = ""

    parsed = _safe_json_dict({})
    if model_text:
        try:
            parsed = _safe_json_dict(json.loads(model_text))
        except Exception:
            parsed = {}

    year = str(parsed.get("year") or _extract_year(model_text) or "").strip()
    if year and not re.match(r"^(19|20)\d{2}$", year):
        year = _extract_year(year)

    raw_conf = parsed.get("confidence")
    conf = 0.0
    try:
        conf = float(raw_conf)
    except Exception:
        t = str(raw_conf or "").strip().lower()
        if t in ("high", "very high"):
            conf = 0.9
        elif t in ("medium", "mid"):
            conf = 0.6
        elif t in ("low", "very low"):
            conf = 0.3

    out = {
        "wine_name": str(parsed.get("wine_name") or "").strip(),
        "grape": str(parsed.get("grape") or "").strip(),
        "country": str(parsed.get("country") or "").strip(),
        "region": str(parsed.get("region") or "").strip(),
        "year": year,
        "confidence": conf,
        "notes": str(parsed.get("notes") or "").strip(),
    }

    return JSONResponse(content=out, headers={"Access-Control-Allow-Origin": "*"})
