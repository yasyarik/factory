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

    # Meta requirement: description 145-160 chars.
    desc_len = len(desc) if desc else 0
    if not desc or desc_len < 145 or desc_len > 160:
        problems.append(f"description length must be 145-160 chars (got {desc_len})")

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
            problems.append("lead paragraph must start with a direct <strong> statement")

    h2 = _count(r"<h2\b", html)
    if h2 < 6 or h2 > 9:
        problems.append(f"expected 6-9 H2, got {h2}")

    h3 = _count(r"<h3\b", html)
    if h3 > 16:
        problems.append(f"too many H3 sections (max 16, got {h3})")

    h2_texts = re.findall(r"<h2[^>]*>(.*?)</h2>", html, flags=re.IGNORECASE | re.DOTALL)
    # Question-style H2 is optional: no hard failure here.

    if _count(r"<table\b", html) < 1:
        problems.append("missing <table>")
    if _count(r"<ol\b", html) < 1:
        problems.append("missing step-by-step ordered list (<ol>)")

    img_tags = re.findall(r"<img\b([^>]*)>", html, flags=re.IGNORECASE)
    if len(img_tags) < 2 or len(img_tags) > 3:
        problems.append(f"images must be 2-3 (got {len(img_tags)})")
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

    if not isinstance(faq, list) or len(faq) < 5 or len(faq) > 7:
        problems.append("faq must contain 5-7 items")

    # myugc guardrail: reject wine/off-topic narratives
    plain = _strip_tags(html).lower()
    forbidden_literals = [
        "wine",
        "wineries",
        "vineyard",
        "sommelier",
        "terroir",
        "grape varieties",
    ]
    for lit in forbidden_literals:
        if lit in plain:
            problems.append(f"myugc guardrail: forbidden term {lit}")
            break

    if re.search(r"how\s+to\s+build\b", plain) and re.search(r"\b(matrix|automation|system|workflow|pipeline|stack)\b", plain):
        problems.append("myugc guardrail: technical build-guide content is not allowed")

    # Ensure each H2 has a following paragraph (answer-first), but do not enforce rigid H3 template.
    blocks = re.split(r"(<h2[^>]*>.*?</h2>)", html, flags=re.IGNORECASE | re.DOTALL)
    for i in range(1, len(blocks), 2):
        after = blocks[i + 1] if i + 1 < len(blocks) else ""
        m = re.search(r"<p[^>]*>\s*(.*?)</p>", after, flags=re.IGNORECASE | re.DOTALL)
        if not m:
            problems.append("answer-first: missing paragraph after a H2")
            break

    forbidden_patterns = (
        "answer:",
        "reasoning:",
        "framework:",
        "decision layer",
        "execution layer",
        "scenario deep dive",
        "progress tracking",
        "comparison loop",
        "workflow logic",
    )
    for pat in forbidden_patterns:
        if pat in plain:
            problems.append(f"forbidden wording in article: {pat}")
            break

    return problems
