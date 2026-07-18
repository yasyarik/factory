import os
import re
import json
import html as htmlmod
import subprocess
from datetime import datetime, timezone



def _site_origin() -> str:
    raw = (os.environ.get('SITE_ORIGIN') or 'https://myugc.studio').strip()
    return raw.rstrip('/')


def _site_name() -> str:
    return (os.environ.get('SITE_NAME') or os.environ.get('BRAND_NAME') or 'My UGC Studio').strip() or 'My UGC Studio'

def _cta_box_html() -> str:
    enabled = (os.environ.get('SITE_CTA_ENABLED') or '').strip().lower()
    if enabled not in ('1', 'true', 'yes', 'on'):
        return ''
    title = (os.environ.get('SITE_CTA_TITLE') or '').strip() or 'Ready to take the next step?'
    text = (os.environ.get('SITE_CTA_TEXT') or '').strip()
    btn_text = (os.environ.get('SITE_CTA_BUTTON_TEXT') or '').strip() or 'Learn more'
    btn_url = (os.environ.get('SITE_CTA_BUTTON_URL') or '').strip() or '/'
    out = '<div class=\"cta-box\">'
    out += f'<h2>{_html_escape(title)}</h2>'
    if text:
        out += f'<p>{_html_escape(text)}</p>'
    out += f'<a href=\"{_html_escape(btn_url)}\" class=\"btn btn-primary\" style=\"padding: 16px 40px; font-size: 18px;\">{_html_escape(btn_text)}</a>'
    out += '</div>'
    return out



def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _html_escape(s: str) -> str:
    return htmlmod.escape(s, quote=True)


def _html_unescape(s: str) -> str:
    return htmlmod.unescape(s or "")


def _html_unescape_deep(s: str, rounds: int = 3) -> str:
    out = s or ""
    for _ in range(max(1, rounds)):
        nxt = htmlmod.unescape(out)
        if nxt == out:
            break
        out = nxt
    return out


def _slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s[:120].strip("-") or "post"


def list_existing_posts(blog_dir: str) -> list[dict[str, str]]:
    posts: list[dict[str, str]] = []
    for name in os.listdir(blog_dir):
        if not name.endswith(".html"):
            continue
        if name in ("index.html", "template.html"):
            continue
        path = os.path.join(blog_dir, name)
        try:
            src = _read(path)
        except Exception:
            continue

        slug = name[:-5]
        m_title = re.search(r"<h1[^>]*>(.*?)</h1>", src, flags=re.IGNORECASE | re.DOTALL)
        title = re.sub(r"<[^>]+>", "", m_title.group(1)).strip() if m_title else slug

        m_desc = re.search(
            r"<meta\s+name=\"description\"\s+content=\"(.*?)\"",
            src,
            flags=re.IGNORECASE | re.DOTALL,
        )
        desc = (m_desc.group(1) or "").strip() if m_desc else ""

        m_cat = re.search(
            r"class=\"post-category\"[^>]*>(.*?)</",
            src,
            flags=re.IGNORECASE | re.DOTALL,
        )
        cat = re.sub(r"<[^>]+>", "", m_cat.group(1)).strip() if m_cat else ""

        posts.append(
            {
                "slug": slug,
                "url": f"/blog/{slug}/",
                "title": title,
                "description": desc,
                "category": cat,
            }
        )

    return posts


def _prefer_webp_url(image_url: str, blog_dir: str) -> str:
    u = (image_url or "").strip()
    if not u:
        return u
    if u.startswith("http://") or u.startswith("https://"):
        return u

    # Normalize bare local filenames to absolute /blog/ paths for stable rendering
    # from /blog/ and /blog/index/ routes.
    q = ""
    if "?" in u:
        raw, q = u.split("?", 1)
        q = "?" + q
    else:
        raw = u
    fname = os.path.basename(raw)
    if not u.startswith("/") and fname and os.path.exists(os.path.join(blog_dir, fname)):
        u = f"/blog/{fname}{q}"
        raw = f"/blog/{fname}"

    raw_no_q = u.split("?", 1)[0]
    fname = os.path.basename(raw_no_q)
    root_dir = os.path.dirname(blog_dir)
    base, ext = os.path.splitext(fname)
    if ext.lower() not in (".png", ".jpg", ".jpeg"):
        return u

    webp_name = base + ".webp"
    if os.path.exists(os.path.join(blog_dir, webp_name)) or os.path.exists(os.path.join(root_dir, webp_name)):
        return u.replace(fname, webp_name)
    return u


def _build_breadcrumbs(title: str) -> str:
    return (
        '<nav class="breadcrumbs" aria-label="Breadcrumb">'
        '<a href="/">Home</a>'
        '<span class="sep">›</span>'
        '<a href="/blog/">Blog</a>'
        '<span class="sep">›</span>'
        f'<span class="current">{_html_escape(title)}</span>'
        "</nav>"
    )


def _blog_ui_strings(locale: str) -> dict[str, str]:
    loc = (locale or "en").strip().lower()
    base = {
        "home": "Home",
        "blog": "Blog",
        "countries": "Countries",
        "all_countries": "All Countries",
        "back": "Back to Resources",
        "published": "Published",
        "min_read": "min read",
        "share": "Share this article",
        "toc": "On this page",
        "toc_aria": "Table of contents",
        "terms": "Terms of Service",
        "privacy": "Privacy Policy",
        "copied": "Link copied to clipboard!",
        "rights": "All rights reserved.",
    }
    table = {
        "ru": {
            "home": "Главная",
            "blog": "Блог",
            "countries": "Страны",
            "all_countries": "Все страны",
            "back": "Назад к материалам",
            "published": "Опубликовано",
            "min_read": "мин чтения",
            "share": "Поделиться статьей",
            "toc": "На этой странице",
            "toc_aria": "Содержание",
            "terms": "Условия использования",
            "privacy": "Политика конфиденциальности",
            "copied": "Ссылка скопирована в буфер обмена!",
            "rights": "Все права защищены.",
        },
        "es": {
            "home": "Inicio",
            "blog": "Blog",
            "countries": "Países",
            "all_countries": "Todos los países",
            "back": "Volver a los recursos",
            "published": "Publicado",
            "min_read": "min de lectura",
            "share": "Compartir este artículo",
            "toc": "En esta página",
            "toc_aria": "Tabla de contenido",
            "terms": "Términos del servicio",
            "privacy": "Política de privacidad",
            "copied": "Enlace copiado al portapapeles!",
            "rights": "Todos los derechos reservados.",
        },
        "de": {
            "home": "Startseite",
            "blog": "Blog",
            "countries": "Länder",
            "all_countries": "Alle Länder",
            "back": "Zurück zu den Artikeln",
            "published": "Veröffentlicht",
            "min_read": "Min. Lesezeit",
            "share": "Diesen Artikel teilen",
            "toc": "Auf dieser Seite",
            "toc_aria": "Inhaltsverzeichnis",
            "terms": "Nutzungsbedingungen",
            "privacy": "Datenschutzerklärung",
            "copied": "Link in die Zwischenablage kopiert!",
            "rights": "Alle Rechte vorbehalten.",
        },
        "fr": {
            "home": "Accueil",
            "blog": "Blog",
            "countries": "Pays",
            "all_countries": "Tous les pays",
            "back": "Retour aux articles",
            "published": "Publié",
            "min_read": "min de lecture",
            "share": "Partager cet article",
            "toc": "Sur cette page",
            "toc_aria": "Table des matières",
            "terms": "Conditions d'utilisation",
            "privacy": "Politique de confidentialité",
            "copied": "Lien copié dans le presse-papiers !",
            "rights": "Tous droits réservés.",
        },
    }
    return table.get(loc, base)


def _localize_rendered_blog_html(out: str, locale: str, date_iso: str | None = None, toc_title: str | None = None) -> str:
    loc = (locale or "en").strip().lower()
    if not loc or loc == "en":
        return out

    s = _blog_ui_strings(loc)
    locale_prefix = f"/{loc}"
    if not date_iso:
        m_date = re.search(r'<time[^>]*datetime="([^"]+)"', out or "", flags=re.IGNORECASE)
        date_iso = (m_date.group(1) if m_date else "") or _utc_date()
    toc_text = (toc_title or "").strip() or s["toc"]

    out = out.replace('href="/"', f'href="{locale_prefix}/"')
    out = out.replace('href="/blog/"', f'href="{locale_prefix}/blog/"')
    out = out.replace('href="/wine-countries/"', f'href="{locale_prefix}/wine-countries/"')
    out = out.replace('href="/wine-regions/"', f'href="{locale_prefix}/wine-regions/"')
    out = out.replace('href="/policy/terms/"', f'href="{locale_prefix}/policy/terms/"')
    out = out.replace('href="/policy/privacy/"', f'href="{locale_prefix}/policy/privacy/"')

    out = out.replace(">Home<", f">{_html_escape(s['home'])}<")
    out = out.replace(">Blog<", f">{_html_escape(s['blog'])}<")
    out = out.replace(">Countries<", f">{_html_escape(s['countries'])}<")
    out = out.replace(">All Countries<", f">{_html_escape(s['all_countries'])}<")
    out = out.replace("← Back to Resources", "← " + s["back"])
    out = out.replace(">Share this article<", f">{_html_escape(s['share'])}<")
    out = out.replace(">Terms of Service<", f">{_html_escape(s['terms'])}<")
    out = out.replace(">Privacy Policy<", f">{_html_escape(s['privacy'])}<")
    out = out.replace(">All rights reserved.<", f">{_html_escape(s['rights'])}<")
    out = out.replace("alert('Link copied to clipboard!');", "alert(" + json.dumps(s["copied"], ensure_ascii=False) + ");")
    out = out.replace("t.textContent = 'On this page';", "t.textContent = " + json.dumps(toc_text, ensure_ascii=False) + ";")
    out = out.replace("aside.setAttribute('aria-label', 'Table of contents');", "aside.setAttribute('aria-label', " + json.dumps(s["toc_aria"], ensure_ascii=False) + ");")

    out = re.sub(
        r'<time([^>]*)datetime="[^"]+"[^>]*>Published\s+[^<]+</time>',
        lambda m: f'<time{m.group(1)}datetime="{_html_escape(date_iso)}">{_html_escape(s["published"])} {date_iso}</time>',
        out,
        count=1,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r'>\s*(\d+)\s*min read\s*<',
        lambda m: ">" + m.group(1) + " " + _html_escape(s["min_read"]) + "<",
        out,
        count=1,
        flags=re.IGNORECASE,
    )
    return out


def _ensure_heading_ids(content_html: str) -> tuple[str, list[dict[str, str]]]:
    # Adds id attributes to h2/h3 and returns a toc model.
    # Important: include headings that already have id="..." so TOC is never lost.
    toc: list[dict[str, str]] = []

    def repl(m: re.Match[str]) -> str:
        tag = m.group(1).lower()
        attrs = m.group(2) or ""
        inner = m.group(3) or ""
        text = re.sub(r"<[^>]+>", "", inner).strip()
        id_match = re.search(r'\bid\s*=\s*"([^"]+)"', attrs, flags=re.IGNORECASE)
        hid = (id_match.group(1).strip() if id_match else "") or _slugify(text)
        if text and hid:
            toc.append({"level": tag, "id": hid, "text": text})
        if id_match:
            return m.group(0)
        return f"<{tag}{attrs} id=\"{hid}\">{inner}</{tag}>"

    out = re.sub(
        r"<(h2|h3)([^>]*)>(.*?)</\1>",
        repl,
        content_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return out, toc


def _collect_toc_from_existing_headings(content_html: str) -> list[dict[str, str]]:
    toc: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r"<(h2|h3)([^>]*)>(.*?)</\1>", content_html or "", flags=re.IGNORECASE | re.DOTALL):
        tag = (m.group(1) or "").lower()
        attrs = m.group(2) or ""
        inner = m.group(3) or ""
        text = re.sub(r"<[^>]+>", "", inner).strip()
        if not text:
            continue
        id_match = re.search(r'\bid\s*=\s*"([^"]+)"', attrs, flags=re.IGNORECASE)
        hid = (id_match.group(1).strip() if id_match else "") or _slugify(text)
        if not hid or hid in seen:
            continue
        seen.add(hid)
        toc.append({"level": tag, "id": hid, "text": text})
    return toc


def _render_toc(toc: list[dict[str, str]], title: str = "On this page") -> str:
    if not toc:
        return ""

    # Keep the TOC compact: prefer H2 only (the spec already enforces 8-12 H2).
    filtered = [it for it in toc if (it.get("level") or "").lower() == "h2"]
    if not filtered:
        filtered = toc

    items: list[str] = []
    for it in filtered:
        hid = (it.get("id") or "").strip()
        text = (it.get("text") or "").strip()
        if not hid or not text:
            continue
        cls = "toc-h3" if (it.get("level") or "").lower() == "h3" else "toc-h2"
        items.append('<li><a class="' + cls + '" href="#' + _html_escape(hid) + '">' + _html_escape(text) + '</a></li>')

    if not items:
        return ""

    return (
        "<aside class=\"toc-box\">"
        "<div class=\"toc-title\">" + _html_escape(title or "On this page") + "</div>"
        "<ul class=\"toc-items\">"
        + "".join(items)
        + "</ul></aside>"
    )


def _faq_json_ld(url: str, faq: list[dict[str, str]]) -> str:
    main = []
    for qa in faq:
        q = (qa.get("question") or "").strip()
        a = (qa.get("answer") or "").strip()
        if not q or not a:
            continue
        main.append(
            {
                "@type": "Question",
                "name": q,
                "acceptedAnswer": {"@type": "Answer", "text": a},
            }
        )
    if not main:
        return ""
    payload = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": main}
    return f'<script type="application/ld+json">{json.dumps(payload, ensure_ascii=False)}</script>'


def _render_sources(sources: list[dict[str, str]] | None) -> str:
    if not sources:
        return ""

    items = []
    for s in sources:
        url = (s.get("url") or "").strip()
        title = (s.get("title") or url).strip()
        if not url:
            continue
        items.append(
            f"<li><a href=\"{_html_escape(url)}\" target=\"_blank\" rel=\"noreferrer nofollow\">{_html_escape(title)}</a></li>"
        )

    if not items:
        return ""

    return (
        "<section class=\"sources\" style=\"margin-top:36px;\">"
        "<h2 style=\"margin:0 0 10px;\">Sources</h2>"
        "<ul style=\"margin:0; padding-left:18px;\">"
        + "".join(items)
        + "</ul></section>"
    )



def render_post_html(
    *,
    blog_dir: str,
    title: str,
    description: str,
    category: str,
    slug: str,
    hero_image: str,
    content_html: str,
    faq: list[dict[str, str]],
    sources: list[dict[str, str]] | None,
    updated_at: str,
    noindex: bool,
    toc_title: str = "On this page",
) -> str:
    template_path = (os.environ.get("FACTORY_ARTICLE_TEMPLATE_PATH") or "").strip()
    if not template_path or not os.path.exists(template_path):
        template_path = os.path.join(blog_dir, "template.html")
    if not os.path.exists(template_path):
        template_path = os.path.join(os.path.dirname(os.path.dirname(blog_dir)), "blog", "template.html")
    src = _read(template_path)

    hero_file = os.path.basename(hero_image)
    parent_dir = os.path.dirname(blog_dir)
    parent_name = os.path.basename(parent_dir)
    hero_in_blog = os.path.join(blog_dir, hero_file)
    hero_in_root = os.path.join(parent_dir, hero_file)
    hero_in_shared_blog = os.path.join(os.path.dirname(parent_dir), "blog", hero_file) if parent_name in {"ru", "es", "de", "fr"} else ""
    if hero_file and os.path.exists(hero_in_blog):
        image_placeholder = _prefer_webp_url(f"/blog/{hero_file}", blog_dir)
    elif hero_file and os.path.exists(hero_in_root):
        image_placeholder = _prefer_webp_url(f"/{hero_file}", blog_dir)
    elif hero_file and hero_in_shared_blog and os.path.exists(hero_in_shared_blog):
        image_placeholder = _prefer_webp_url(f"/blog/{hero_file}", os.path.join(os.path.dirname(parent_dir), "blog"))
    else:
        m_first = re.search(r"<img\b[^>]*\bsrc=(?:\"([^\"]+)\"|'([^']+)')", content_html or "", flags=re.IGNORECASE)
        src = (m_first.group(1) or m_first.group(2) or "").strip() if m_first else ""
        image_placeholder = src if src else "/hero_ai.jpg"
    # PDF targets: title 55-60 chars, description 155-160 chars.
    meta_title = re.sub(r"\s+", " ", (title or "").strip())
    if len(meta_title) > 60:
        meta_title = meta_title[:60].rsplit(" ", 1)[0].rstrip("-:|,")

    meta_desc = re.sub(r"\s+", " ", (description or "").strip())
    if len(meta_desc) > 160:
        meta_desc = meta_desc[:160].rsplit(" ", 1)[0].rstrip("-:|,")
    if len(meta_desc) < 155:
        tail = " Get the checklist."
        if len(meta_desc) + len(tail) <= 160:
            meta_desc = meta_desc + tail
        if len(meta_desc) < 155:
            meta_desc = (meta_desc + " Learn the steps.")[:160].rstrip("-:|,")

    # Extract lead paragraph before the first H2 to place it right after H1.
    lead_p = ""
    m_lead = re.search(r"(?is)^\s*(<p[^>]*>.*?</p>)\s*(?=<h2\b)", content_html or "")
    if m_lead:
        lead_p = m_lead.group(1)
        content_html = (content_html or "")[m_lead.end():]

    # Add ids + TOC model.
    content_html, toc_model = _ensure_heading_ids(content_html)

    # Defensive: strip any share blocks if they leak into content_html (template already renders share UI).
    content_html = re.sub(r'(?is)<div[^>]+class=\"share-section\".*?</div>', '', content_html or '')
    content_html = re.sub(r'(?is)<script[^>]*>\s*function\s+copyUrl\s*\(.*?</script>', '', content_html or '')

    # Wrap each H2 block into <section> (semantic HTML).
    parts = re.split(r"(<h2[^>]*>.*?</h2>)", content_html, flags=re.IGNORECASE | re.DOTALL)
    if len(parts) > 1:
        out_parts = [parts[0]]
        sec_idx = 0
        for i in range(1, len(parts), 2):
            h2 = parts[i]
            after = parts[i + 1] if i + 1 < len(parts) else ""
            sec_idx += 1
            m_id = re.search(r"\bid=\"(.*?)\"", h2, flags=re.IGNORECASE)
            sid = (m_id.group(1) if m_id else f"section-{sec_idx}")
            out_parts.append(f'<section id="{_html_escape(sid)}">' + h2 + after + "</section>")
        content_html = "".join(out_parts)

    toc_html = _render_toc(toc_model, toc_title)
    if not toc_html:
        # Safety net for legacy/edited HTML where headings already had ids.
        fallback_toc = _collect_toc_from_existing_headings(content_html)
        toc_html = _render_toc(fallback_toc, toc_title)
    sources_html = _render_sources(sources) if noindex else ""

    origin = _site_origin().rstrip("/")
    parent = os.path.basename(os.path.dirname(blog_dir))
    locale_prefix = f"/{parent}" if parent in {"de", "es", "fr", "ru"} else ""
    url = f"{origin}{locale_prefix}/blog/{slug}/"
    date_iso = (updated_at or "").split("T", 1)[0] or _utc_date()

    if noindex or locale_prefix:
        # Locale pages (/ru,/es,/de,/fr) and preview pages need absolute blog asset paths.
        def _abs_img(m: re.Match[str]) -> str:
            src = (m.group(1) or m.group(2) or '').strip()
            if not src:
                return m.group(0)
            low = src.lower()
            if low.startswith('http://') or low.startswith('https://') or low.startswith('data:'):
                return m.group(0)
            if src.startswith('/') or src.startswith('../'):
                return m.group(0)
            fname = src.lstrip('./').split('/')[-1]
            return m.group(0).replace(src, '/blog/' + fname)

        content_html = re.sub(
            r"<img\b[^>]*?\bsrc=(?:\"([^\"]+)\"|'([^']+)')",
            _abs_img,
            content_html or '',
            flags=re.IGNORECASE,
        )

    json_ld_faq = _faq_json_ld(url, faq)

    json_ld_bc = (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Home", "item": origin + locale_prefix + "/"},
                    {"@type": "ListItem", "position": 2, "name": "Blog", "item": origin + locale_prefix + "/blog/"},
                    {"@type": "ListItem", "position": 3, "name": title, "item": url},
                ],
            },
            ensure_ascii=False,
        )
        + '</script>'
    )

    img_url = f"{origin}{_prefer_webp_url(f'/blog/{hero_file}', blog_dir)}" if hero_file else ""
    json_ld_post = (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "BlogPosting",
                "mainEntityOfPage": {"@type": "WebPage", "@id": url},
                "headline": title,
                "description": meta_desc,
                "image": [img_url] if img_url else [],
                "datePublished": date_iso,
                "dateModified": date_iso,
                "author": {"@type": "Organization", "name": _site_name()},
                "publisher": {"@type": "Organization", "name": _site_name()},
            },
            ensure_ascii=False,
        )
        + '</script>'
    )

    head_inject = [
        f'<link rel="canonical" href="{url}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{_html_escape(title)}">',
        f'<meta name="twitter:description" content="{_html_escape(meta_desc)}">',
        f'<meta name="twitter:image" content="{img_url}">' if img_url else '',
        json_ld_post,
        json_ld_bc,
    ]

    if noindex:
        head_inject.append('<meta name="robots" content="noindex, nofollow, noarchive">')

    if json_ld_faq:
        head_inject.append(json_ld_faq)

    inject_block = "\n".join([x for x in head_inject if x]) + "\n"
    if "</head>" in src:
        src = src.replace("</head>", inject_block + "</head>")

    out = src
    out = out.replace("{{TITLE}}", _html_escape(title))
    out = out.replace("{{DESC}}", _html_escape(meta_desc))
    out = out.replace("{{SLUG}}", _html_escape(slug))
    out = out.replace("{{CAT}}", _html_escape(category))
    out = out.replace(
        "{{CONTENT}}",
        f"{_build_breadcrumbs(title)}{toc_html}{content_html}{sources_html}",
    )
    out = out.replace("{{IMAGE}}", image_placeholder)
    out = out.replace("{{CTA}}", _cta_box_html())

    # Force SEO <title> without the template suffix.
    out = re.sub(r"<title>.*?</title>", f"<title>{_html_escape(meta_title)}</title>", out, flags=re.IGNORECASE | re.DOTALL)

    # Semantic HTML tweaks per PDF.
    out = out.replace("<article>", '<article itemscope itemtype="https://schema.org/BlogPosting">', 1)
    out = out.replace('<div class="post-header">', '<header class="post-header">', 1)
    out = out.replace("</div>\n\n            <div class=\"post-hero\"", "</header>\n\n            <div class=\"post-hero\"", 1)
    out = out.replace("<h1>", '<h1 itemprop="headline">', 1)

    if lead_p:
        out = out.replace("</h1>", f"</h1>\n                <div class=\"post-intro\" style=\"margin-top:16px;\">{lead_p}</div>", 1)

    out = re.sub(
        r"<span>Published[^<]*</span>",
        f'<time itemprop="datePublished" datetime="{_html_escape(date_iso)}">Published {date_iso}</time>',
        out,
        count=1,
        flags=re.IGNORECASE,
    )

    # Replace the legacy CSS background hero with a real image element so localized
    # blog pages do not depend on background rendering quirks.
    hero_markup = (
        '<figure class="post-hero">'
        f'<img class="post-hero-image" src="{_html_escape(image_placeholder)}" alt="{_html_escape(title)}" loading="eager" decoding="async" fetchpriority="high"/>'
        '</figure>'
    )
    out = re.sub(
        r'(?is)<div\s+class="post-hero"[^>]*style="[^\"]*background-image:\s*url\([^)]+\)[^\"]*"[^>]*></div>',
        hero_markup,
        out,
        count=1,
    )
    if ".post-hero-image" not in out and "</style>" in out:
        hero_css = """
        .post-hero { overflow: hidden; }
        .post-hero-image {
            display: block;
            width: 100%;
            min-height: 450px;
            height: auto;
            object-fit: cover;
            background: rgba(255,255,255,0.02);
        }
        """
        out = out.replace("</style>", hero_css + "\n    </style>", 1)

    # Fix hero relative path.
    out = out.replace("url('blog/", "url('")
    out = out.replace('url("blog/', 'url("')

    out = _localize_rendered_blog_html(out, parent, date_iso=date_iso, toc_title=toc_title)

    return out




def _normalize_blog_index_seo(src: str, href_prefix: str = "/blog") -> str:
    origin = _site_origin()
    clean_prefix = "/" + href_prefix.strip("/")

    canonical = f"{origin}{clean_prefix}/"
    alts = {
        "en": f"{origin}/blog/",
        "ru": f"{origin}/ru/blog/",
        "es": f"{origin}/es/blog/",
        "de": f"{origin}/de/blog/",
        "fr": f"{origin}/fr/blog/",
    }

    src = re.sub(r'(?is)<link\s+[^>]*rel=["\']canonical["\'][^>]*>', '', src)
    src = re.sub(r'(?is)<link\s+[^>]*hreflang=["\'][^"\']+["\'][^>]*rel=["\']alternate["\'][^>]*>', '', src)
    src = re.sub(r'(?is)<link\s+[^>]*rel=["\']alternate["\'][^>]*hreflang=["\'][^"\']+["\'][^>]*>', '', src)
    src = re.sub(
        r'(?is)<meta\s+[^>]*property=["\']og:url["\'][^>]*>',
        f'<meta content="{canonical}" property="og:url"/>',
        src,
        count=1,
    )

    block = (
        f'<link href="{canonical}" rel="canonical"/>'
        + ''.join([f'<link href="{u}" hreflang="{k}" rel="alternate"/>' for k, u in alts.items()])
        + f'<link href="{alts["en"]}" hreflang="x-default" rel="alternate"/>'
    )
    if '</head>' in src:
        src = src.replace('</head>', block + '</head>', 1)
    return src


_BAD_EXCERPT_RE = re.compile(
    r"(?i)\b(answer-first|answer-firs|answer-fir|answe\w*|quick-answer|factory|no posts yet|create your first article|load more|showing\s+\d+)\b"
)
_FALLBACK_EXCERPT = "A practical wine guide from YAS Wine with clear tips for choosing, serving and enjoying wine."


def _clean_card_excerpt(value: str) -> str:
    out = _html_unescape_deep(value or "")
    out = re.sub(r"<[^>]+>", " ", out)
    out = re.sub(r"(?i)\b(?:short\s+)?answer\s*:\s*", "", out)
    out = _BAD_EXCERPT_RE.sub("", out)
    out = re.sub(r"\s+([.,;:!?])", r"\1", out)
    out = re.sub(r"(?:^|\s)[.]+(?=\s|$)", " ", out)
    out = re.sub(r"\s+", " ", out).strip(" -:|,;.")
    if len(out) < 95 or _BAD_EXCERPT_RE.search(out):
        out = _FALLBACK_EXCERPT
    if len(out) > 180:
        cut = out[:180]
        pos = cut.rfind(" ")
        if pos >= 95:
            cut = cut[:pos]
        out = cut.rstrip(" -:|,;.")
    return out


def upsert_blog_index_card(
    blog_dir: str,
    *,
    slug: str,
    title: str,
    description: str,
    category: str,
    hero_image: str,
    href_prefix: str = "/blog",
    marker_prefix: str = "FACTORY",
) -> None:
    index_path = os.path.join(blog_dir, "index.html")
    src = _read(index_path)

    clean_prefix = "/" + href_prefix.strip("/")
    href = f"{clean_prefix}/{slug}/"
    marker = f"<!-- {marker_prefix}:{slug} -->"

    if marker in src:
        pattern = re.compile(
            re.escape(marker)
            + r'\s*<a[^>]+href="'
            + re.escape(href)
            + r'"[\s\S]*?</a>\s*',
            flags=re.IGNORECASE,
        )
        src = pattern.sub("", src, count=1)
    elif href in src:
        return

    title = _html_unescape_deep(title).strip()
    category = _html_unescape_deep(category).strip()
    description = _html_unescape_deep(description).strip()
    excerpt = _clean_card_excerpt(description)

    card = (
        f'\n            <a href="{href}" class="blog-card">\n'
        f'                <div class="card-image" style="background-image: url(\'{_html_escape(_prefer_webp_url(hero_image, blog_dir))}\');"></div>\n'
        f'                <div class="card-content">\n'
        f'                    <span class="category">{_html_escape(category)}</span>\n'
        f'                    <h3 class="card-title">{_html_escape(title)}</h3>\n'
        f'                    <p class="card-excerpt">{_html_escape(excerpt)}</p>\n'
        f'                </div>\n'
        f'            </a>\n'
    )

    needle = '<div class="blog-grid">'
    idx = src.find(needle)
    if idx < 0:
        raise RuntimeError("blog index: blog-grid not found")

    insert_at = idx + len(needle)
    out = src[:insert_at] + card + src[insert_at:]
    out = _normalize_blog_index_seo(out, href_prefix=clean_prefix)
    _write(index_path, out)





def remove_blog_index_card(
    blog_dir: str,
    *,
    slug: str,
    href_prefix: str = "/blog",
    marker_prefix: str = "FACTORY",
) -> None:
    index_path = os.path.join(blog_dir, "index.html")
    try:
        src = _read(index_path)
    except Exception:
        return

    clean_prefix = "/" + href_prefix.strip("/")
    href = f"{clean_prefix}/{slug}/"
    marker = f"<!-- {marker_prefix}:{slug} -->"
    if marker in src:
        pattern = re.compile(
            re.escape(marker)
            + r'\s*<a[^>]+href="'
            + re.escape(href)
            + r'"[\s\S]*?</a>\s*',
            flags=re.IGNORECASE,
        )
        src = pattern.sub("", src, count=1)
    elif href in src:
        pattern = re.compile(
            r'\s*<a[^>]+href="'
            + re.escape(href)
            + r'"[\s\S]*?</a>\s*',
            flags=re.IGNORECASE,
        )
        src = pattern.sub("", src, count=1)
    else:
        return

    _write(index_path, _normalize_blog_index_seo(src, href_prefix=clean_prefix))




def upsert_sitemap_url(sitemap_path: str, *, url: str) -> None:
    src = _read(sitemap_path)
    if f"<loc>{url}</loc>" in src:
        return

    entry = (
        "  <url>\n"
        f"    <loc>{url}</loc>\n"
        f"    <lastmod>{_utc_date()}</lastmod>\n"
        "    <changefreq>monthly</changefreq>\n"
        "    <priority>0.8</priority>\n"
        "  </url>\n"
    )

    out = src.replace("</urlset>", entry + "</urlset>")
    _write(sitemap_path, out)


def remove_sitemap_url(sitemap_path: str, *, url: str) -> None:
    try:
        src = _read(sitemap_path)
    except Exception:
        return

    if f"<loc>{url}</loc>" not in src:
        return

    pattern = re.compile(
        r"\s*<url>\s*<loc>" + re.escape(url) + r"</loc>[\s\S]*?</url>\s*",
        flags=re.IGNORECASE,
    )
    out = pattern.sub("\n", src, count=1)
    if out != src:
        _write(sitemap_path, out)


def _git_push_best_effort(repo_dir: str) -> None:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        subprocess.run(
            ["git", "-C", repo_dir, "push", "origin", "main"],
            check=False,
            timeout=25,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        # Do not block site publication by git transport failures/timeouts.
        return


def git_commit_push_with_remove(*, repo_dir: str, message: str, add_paths: list[str], remove_paths: list[str]) -> None:
    for p in remove_paths:
        subprocess.check_call(["git", "-C", repo_dir, "rm", "-f", "--ignore-unmatch", p])

    for p in add_paths:
        subprocess.check_call(["git", "-C", repo_dir, "add", p])

    try:
        subprocess.check_call(["git", "-C", repo_dir, "commit", "-m", message])
    except subprocess.CalledProcessError:
        return

    _git_push_best_effort(repo_dir)


def git_commit_push(*, repo_dir: str, message: str, paths: list[str]) -> None:
    for p in paths:
        subprocess.check_call(["git", "-C", repo_dir, "add", p])

    try:
        subprocess.check_call(["git", "-C", repo_dir, "commit", "-m", message])
    except subprocess.CalledProcessError:
        return

    _git_push_best_effort(repo_dir)
