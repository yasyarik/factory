import re
from typing import Any


def _count(pattern: str, s: str) -> int:
    return len(re.findall(pattern, s or "", flags=re.IGNORECASE | re.MULTILINE))


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _lead_paragraph_before_first_h2(html: str) -> str | None:
    h2 = re.search(r"<h2\b", html, flags=re.IGNORECASE)
    head = html if not h2 else html[: h2.start()]
    m = re.search(r"<p[^>]*>\s*(.*?)</p>", head, flags=re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else None


def validate_draft(draft: dict[str, Any]) -> list[str]:
    problems: list[str] = []

    title = (draft.get("title") or "").strip()
    desc = (draft.get("description") or "").strip()
    slug = (draft.get("slug") or "").strip()
    html = (draft.get("contentHtml") or "").strip()
    faq = draft.get("faq") or []

    if not title:
        problems.append("missing title")

    # PDF requirement: meta description 155-160.
    if not desc or len(desc) < 155 or len(desc) > 160:
        problems.append("description length must be 155-160 chars")

    if not slug:
        problems.append("missing slug")

    if not html:
        problems.append("missing contentHtml")
        return problems

    lead = _lead_paragraph_before_first_h2(html)
    if not lead:
        problems.append("missing lead paragraph before first H2")
    else:
        if not re.match(r"<strong>\s*.+?</strong>", lead.lstrip(), flags=re.IGNORECASE | re.DOTALL):
            problems.append("lead paragraph must start with <strong>answer</strong>")

    h2 = _count(r"<h2\b", html)
    if h2 < 8 or h2 > 12:
        problems.append(f"expected 8-12 H2, got {h2}")

    h3 = _count(r"<h3\b", html)
    if h3 < 20 or h3 > 40:
        problems.append(f"expected 20-40 H3, got {h3}")

    h2_texts = re.findall(r"<h2[^>]*>(.*?)</h2>", html, flags=re.IGNORECASE | re.DOTALL)
    if h2_texts:
        q = 0
        for t in h2_texts:
            if _strip_tags(t).endswith("?"):
                q += 1
        if q / max(1, len(h2_texts)) < 0.5:
            problems.append("at least 50% of H2 should be questions")

    if _count(r"<table\b", html) < 1:
        problems.append("missing <table>")
    if _count(r"<ol\b", html) < 1:
        problems.append("missing step-by-step ordered list (<ol>)")
    if _count(r"<blockquote\b", html) < 1:
        problems.append("missing expert quote (<blockquote>)")

    img_tags = re.findall(r"<img\b([^>]*)>", html, flags=re.IGNORECASE)
    if len(img_tags) < 3:
        problems.append("missing images (need >= 3 <img>)")
    else:
        for attrs in img_tags:
            m = re.search(r"\balt=\"(.*?)\"", attrs, flags=re.IGNORECASE | re.DOTALL)
            if (not m) or (not (m.group(1) or "").strip()):
                problems.append("all <img> must have non-empty alt")
                break

    hrefs = re.findall(r"<a\b[^>]*href=\"(.*?)\"", html, flags=re.IGNORECASE)
    internal = [h for h in hrefs if h.startswith("/blog/")]
    if len(internal) < 5:
        problems.append("need at least 5 internal links to /blog/")

    if not isinstance(faq, list) or len(faq) < 5:
        problems.append("missing faq array (need >= 5 Q/A)")

    for tag in ("h2", "h3"):
        blocks = re.split(rf"(<{tag}[^>]*>.*?</{tag}>)", html, flags=re.IGNORECASE | re.DOTALL)
        for i in range(1, len(blocks), 2):
            after = blocks[i + 1] if i + 1 < len(blocks) else ""
            m = re.search(r"<p[^>]*>\s*(.*?)</p>", after, flags=re.IGNORECASE | re.DOTALL)
            if not m:
                problems.append(f"answer-first: missing paragraph after a {tag.upper()}")
                break
            first_p = m.group(1).lstrip()
            if not re.match(r"<strong>\s*.+?</strong>", first_p, flags=re.IGNORECASE | re.DOTALL):
                problems.append(f"answer-first: first paragraph after a {tag.upper()} must start with <strong>answer</strong>")
                break

    return problems
