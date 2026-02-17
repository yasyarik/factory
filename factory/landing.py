import os
import re
import json
import html as htmlmod
import subprocess
from datetime import datetime, timezone


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
                "url": f"/blog/{slug}.html",
                "title": title,
                "description": desc,
                "category": cat,
            }
        )

    return posts


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


def _ensure_heading_ids(content_html: str) -> tuple[str, list[dict[str, str]]]:
    # Adds id attributes to h2/h3 and returns a toc model.
    toc: list[dict[str, str]] = []

    def repl(m: re.Match[str]) -> str:
        tag = m.group(1).lower()
        attrs = m.group(2) or ""
        inner = m.group(3) or ""
        text = re.sub(r"<[^>]+>", "", inner).strip()
        hid = _slugify(text)
        if re.search(r"\bid=\"", attrs, flags=re.IGNORECASE):
            return m.group(0)
        toc.append({"level": tag, "id": hid, "text": text})
        return f"<{tag}{attrs} id=\"{hid}\">{inner}</{tag}>"

    out = re.sub(
        r"<(h2|h3)([^>]*)>(.*?)</\1>",
        repl,
        content_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return out, toc


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
    template_path = os.path.join(blog_dir, "template.html")
    src = _read(template_path)

    hero_file = os.path.basename(hero_image)
    hero_in_blog = os.path.join(blog_dir, hero_file)
    hero_in_root = os.path.join(os.path.dirname(blog_dir), hero_file)
    if hero_file and os.path.exists(hero_in_blog):
        image_placeholder = f"/blog/{hero_file}"
    elif hero_file and os.path.exists(hero_in_root):
        image_placeholder = f"/{hero_file}"
    else:
        image_placeholder = "/logo.png"

    # PDF targets: title 55-60 chars, description 155-160 chars.
    meta_title = re.sub(r"\s+", " ", (title or "").strip())
    if len(meta_title) > 60:
        meta_title = meta_title[:60].rsplit(" ", 1)[0].rstrip("-:|,")
    if len(meta_title) < 55:
        if len(meta_title) + len(" (2026)") <= 60:
            meta_title = meta_title + " (2026)"
        if len(meta_title) < 55:
            meta_title = (meta_title + " - Complete Guide")[:60].rstrip("-:|,")

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
    sources_html = _render_sources(sources) if noindex else ""

    url = f"https://myugc.studio/blog/{slug}.html"
    date_iso = (updated_at or "").split("T", 1)[0] or _utc_date()

    if noindex:
        # Preview pages are served under /factory/preview/, so make asset URLs absolute.
        def _abs_img(m: re.Match[str]) -> str:
            src = (m.group(1) or m.group(2) or '').strip()
            if not src:
                return m.group(0)
            if src.startswith('http://') or src.startswith('https://'):
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
                    {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://myugc.studio/"},
                    {"@type": "ListItem", "position": 2, "name": "Blog", "item": "https://myugc.studio/blog/"},
                    {"@type": "ListItem", "position": 3, "name": title, "item": url},
                ],
            },
            ensure_ascii=False,
        )
        + '</script>'
    )

    img_url = f"https://myugc.studio/blog/{_html_escape(hero_file)}" if hero_file else ""
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
                "author": {"@type": "Organization", "name": "My UGC Studio"},
                "publisher": {"@type": "Organization", "name": "My UGC Studio"},
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

    # Fix hero relative path.
    out = out.replace("url('blog/", "url('")
    out = out.replace('url("blog/', 'url("')

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
    href = f"{clean_prefix}/{slug}.html"
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

    excerpt = description.strip()
    if len(excerpt) > 170:
        excerpt = excerpt[:167].rstrip() + "..."

    card = (
        f'\n            <!-- {marker_prefix}:{slug} -->\n'
        f'            <a href="{href}" class="blog-card">\n'
        f'                <div class="card-image" style="background-image: url(\'{_html_escape(hero_image)}\');"></div>\n'
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
    href = f"{clean_prefix}/{slug}.html"
    marker = f"<!-- {marker_prefix}:{slug} -->"
    if marker not in src:
        return

    pattern = re.compile(
        re.escape(marker)
        + r'\s*<a[^>]+href="'
        + re.escape(href)
        + r'"[\s\S]*?</a>\s*',
        flags=re.IGNORECASE,
    )
    out = pattern.sub("", src, count=1)

    if out != src:
        _write(index_path, out)


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


def git_commit_push_with_remove(*, repo_dir: str, message: str, add_paths: list[str], remove_paths: list[str]) -> None:
    for p in remove_paths:
        subprocess.check_call(["git", "-C", repo_dir, "rm", "-f", "--ignore-unmatch", p])

    for p in add_paths:
        subprocess.check_call(["git", "-C", repo_dir, "add", p])

    try:
        subprocess.check_call(["git", "-C", repo_dir, "commit", "-m", message])
    except subprocess.CalledProcessError:
        return

    subprocess.check_call(["git", "-C", repo_dir, "push", "origin", "main"])


def git_commit_push(*, repo_dir: str, message: str, paths: list[str]) -> None:
    for p in paths:
        subprocess.check_call(["git", "-C", repo_dir, "add", p])

    try:
        subprocess.check_call(["git", "-C", repo_dir, "commit", "-m", message])
    except subprocess.CalledProcessError:
        return

    subprocess.check_call(["git", "-C", repo_dir, "push", "origin", "main"])
