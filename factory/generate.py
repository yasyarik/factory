import os
import re
import json
import urllib.request
from typing import Any

from .landing import _slugify


SYSTEM_PROMPT = """
You are an expert Cyber SEO & GEO writer.
Write a blog article that ranks in Google and is easy for AI answers.

GLOBAL RULES:
- ZERO FLUFF. No generic intros.
- ANSWER-FIRST: The first sentence after EVERY H2 and H3 must be a bolded direct answer.
- Output MUST be STRICT JSON only.
- Obey ALL requirements. If something is missing, FIX it and return corrected JSON (do not explain).

STRUCTURE REQUIREMENTS:
- contentHtml MUST start with a lead paragraph BEFORE the first H2:
  - <p><strong>Direct answer...</strong> 1-2 more sentences.</p>
- H2: 8-12 sections. >=50% of H2 must be questions.
- H3: 20-40 total (roughly 2-4 H3 under each H2).
- After every H2 and H3, the next paragraph MUST start with <strong>...</strong>.

RICH CONTENT REQUIREMENTS:
- >= 1 <table>
- >= 1 <ol> step-by-step
- >= 1 <blockquote>
- >= 3 <img> tags with non-empty alt and caption.
  Use: <figure><img src="..." alt="..." /><figcaption>...</figcaption></figure>
  IMPORTANT: img src must be a relative filename only (e.g. "example.jpg"), not an absolute path. Do NOT use /images/, /blog/, or full URLs.

LINKING REQUIREMENTS:
- Internal links: at least 5 links to /blog/<slug>.html from provided contextLinks.
  Use natural anchors.

FAQ REQUIREMENTS:
- faq: 5-7 Q/A items.

SOURCE INPUT (optional):
- If user JSON includes sourceHtml, rewrite that content into a better-structured article following ALL rules.
- Preserve the core meaning and keep it consistent, but fix structure, add missing elements (links/table/FAQ/images), and tighten wording.

OUTPUT JSON SHAPE:
{
  "slug": "string",
  "title": "string",
  "description": "string (155-160 chars)",
  "category": "string",
  "heroImage": "string (filename only, e.g. scaling-ai.jpg)",
  "contentHtml": "string (HTML fragment)",
  "faq": [{"question":"...","answer":"..."}, ...]
}
""".strip()


RESEARCH_PROMPT = """
You are a research agent for Cyber SEO.
Use Google Search (grounding) to collect up-to-date facts and authoritative sources.

Output MUST be STRICT JSON only.
Return this JSON shape:
{
  "queries": ["..."],
  "sources": [{"title": "...", "url": "https://..."}],
  "facts": ["fact 1", "fact 2"]
}

Rules:
- Prefer primary/authoritative sources (docs, official sites, standards bodies, major publications).
- Avoid forums/social sources unless unavoidable.
- Keep facts concise and verifiable.
""".strip()

def _tokens(s: str) -> set[str]:
    s = re.sub(r"[^a-z0-9\\s]", " ", (s or "").lower())
    parts = [p for p in s.split() if len(p) > 2]
    return set(parts)


def _rank_context(topic: str, posts: list[dict[str, str]], limit: int = 20) -> list[dict[str, str]]:
    t = _tokens(topic)
    scored: list[tuple[int, dict[str, str]]] = []
    for p in posts:
        text = " ".join([p.get("title", ""), p.get("description", ""), p.get("category", "")])
        pt = _tokens(text)
        score = len(t & pt)
        scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = [p for score, p in scored if score > 0][:limit]
    # fallback: if nothing matched, just take first N
    return out if out else posts[:limit]


def _gemini_generate(api_key: str, model: str, system: str, user: str, *, use_grounding: bool) -> tuple[str, dict[str, Any] | None]:
    # Minimal REST call; supports optional Google Search grounding via tools.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    payload: dict[str, Any] = {
        "generationConfig": {"responseMimeType": "application/json"},
        "contents": [
            {"role": "user", "parts": [{"text": system + "\n\n" + user}]}
        ]
    }

    if use_grounding:
        payload["tools"] = [{"google_search": {}}]

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    try:
        cand = data["candidates"][0]
        text = cand["content"]["parts"][0]["text"]
        meta = cand.get("groundingMetadata")
        return text, meta
    except Exception:
        raise RuntimeError(f"Unexpected Gemini response: {data}")


def _extract_sources(meta: dict[str, Any] | None, limit: int = 10) -> tuple[list[dict[str, str]], list[str]]:
    # Extract unique web sources and queries from groundingMetadata.
    if not meta:
        return [], []

    queries: list[str] = []
    for q in (meta.get("webSearchQueries") or []):
        if isinstance(q, str) and q.strip():
            queries.append(q.strip())

    out: list[dict[str, str]] = []
    seen: set[str] = set()

    for ch in (meta.get("groundingChunks") or []):
        if not isinstance(ch, dict):
            continue

        web = ch.get("web") if isinstance(ch.get("web"), dict) else None
        url = None
        title = None

        if web:
            url = web.get("uri") or web.get("url")
            title = web.get("title")

        url = url or ch.get("uri") or ch.get("url")
        title = (title or ch.get("title") or url or "").strip()

        if not url:
            continue
        if url in seen:
            continue

        seen.add(url)
        out.append({"title": title or url, "url": url})
        if len(out) >= limit:
            break

    return out, queries


def _parse_json_strict(s: str) -> dict[str, Any]:
    s = (s or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)

    try:
        return json.loads(s)
    except Exception:
        # Model sometimes adds leading/trailing text or extra lines.
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            return json.loads(s[start : end + 1])
        raise



def generate_draft(
    *,
    topic: str,
    existing_posts: list[dict[str, str]],
    category: str | None,
    hero_image: str | None,
    slug_hint: str | None,
    source_html: str | None = None,
    previous: dict[str, Any] | None = None,
    problems: list[str] | None = None,
) -> dict[str, Any]:
    context = _rank_context(topic, existing_posts, limit=20)

    user: dict[str, Any] = {
        "topic": topic,
        "year": 2026,
        "categoryHint": category,
        "heroImageHint": hero_image,
        "sourceHtml": (source_html or "")[:25000],
        "slugHint": slug_hint,
        "contextLinks": context,
        "site": "https://myugc.studio",
        "basePath": "/blog/",
    }

    # Reuse existing server env conventions (SaaS/API use GOOGLE_API_KEY).
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    if not api_key:
        # Deterministic fallback so preview/publish flow can be tested.
        slug = slug_hint or _slugify(topic)
        return {
            "slug": slug,
            "title": topic,
            "description": (topic + " - practical guide for 2026 with examples, checklist, and FAQ.")[:160],
            "category": category or "Strategy",
            "heroImage": os.path.basename(hero_image) if hero_image else "scaling-ai.jpg",
            "contentHtml": (
                "<h2>What is this about?</h2>"
                "<p><strong>This guide explains the topic with actionable steps.</strong> You'll get a checklist, a comparison table, and FAQs.</p>"
                "<figure><img src=\"scaling-ai.jpg\" alt=\"Illustration\"><figcaption>Example hero-style illustration.</figcaption></figure>"
                "<figure><img src=\"localization.jpg\" alt=\"Illustration\"><figcaption>Example supporting image.</figcaption></figure>"
                "<figure><img src=\"cost-savings.jpg\" alt=\"Illustration\"><figcaption>Example supporting image.</figcaption></figure>"
                "<h2>How do you do it step by step?</h2>"
                "<p><strong>Follow these steps to implement it safely.</strong></p>"
                "<ol><li>Step 1</li><li>Step 2</li><li>Step 3</li></ol>"
                "<h2>How does it compare?</h2>"
                "<p><strong>A quick table helps you choose the right option.</strong></p>"
                "<table><thead><tr><th>Option</th><th>Pros</th><th>Cons</th></tr></thead><tbody><tr><td>A</td><td>Fast</td><td>Limited</td></tr></tbody></table>"
                "<h2>What do experts say?</h2>"
                "<p><strong>Experts recommend focusing on clarity and structure.</strong></p>"
                "<blockquote>Keep content structured: headings, tables, steps, internal links, and FAQs.</blockquote>"
                "<h2>Is it worth it?</h2>"
                "<p><strong>Yes, if you match the intent and keep it factual.</strong></p>"
                "<h2>What mistakes should you avoid?</h2>"
                "<p><strong>Avoid fluff and missing internal links.</strong></p>"
                "<h2>How to optimize for 2026?</h2>"
                "<p><strong>Use updated examples and add schema.</strong></p>"
                "<h2>Which tools help?</h2>"
                "<p><strong>Use AI plus validation and a publish checklist.</strong></p>"
                "<h2>How to measure results?</h2>"
                "<p><strong>Track impressions, clicks, and rankings.</strong></p>"
            ),
            "faq": [
                {"question": "What is the main takeaway?", "answer": "Structure and answer-first improves readability."},
                {"question": "Do I need schema?", "answer": "FAQ schema can help rich results."},
                {"question": "How many internal links?", "answer": "Aim for 3-5 relevant links."},
            ],
        }

    if problems:
        user["mode"] = "repair"
        user["validationProblems"] = problems
        if previous:
            user["previous"] = previous
    use_grounding = os.environ.get("GEMINI_GROUNDING", "1").strip().lower() not in ("0", "false", "no")

    research_sources: list[dict[str, str]] = []
    research_queries: list[str] = []

    # Do research once and reuse during repair attempts.
    if isinstance(previous, dict):
        research_sources = previous.get("sources") or []
        research_queries = previous.get("searchQueries") or []

    if use_grounding and (not research_sources):
        try:
            r_text, r_meta = _gemini_generate(
                api_key,
                model,
                RESEARCH_PROMPT,
                json.dumps({"topic": topic, "year": 2026, "site": "https://myugc.studio"}),
                use_grounding=True,
            )
            r_obj = _parse_json_strict(r_text)
            if isinstance(r_obj, dict):
                if isinstance(r_obj.get("sources"), list):
                    research_sources = [s for s in r_obj.get("sources") if isinstance(s, dict)]
                if isinstance(r_obj.get("queries"), list):
                    research_queries = [q for q in r_obj.get("queries") if isinstance(q, str)]

            # Fallback: extract directly from grounding metadata.
            if not research_sources:
                research_sources, _q = _extract_sources(r_meta)
            if not research_queries:
                _s, research_queries = _extract_sources(r_meta)
        except Exception:
            pass

    if research_sources or research_queries:
        user["research"] = {"sources": research_sources, "queries": research_queries}

    # Writer pass (no need to re-run search here; keep output deterministic).
    text, grounding = _gemini_generate(api_key, model, SYSTEM_PROMPT, json.dumps(user), use_grounding=False)
    out = _parse_json_strict(text)

    sources, queries = _extract_sources(grounding)
    out["sources"] = research_sources or sources
    out["searchQueries"] = research_queries or queries

    if not out.get("slug"):
        out["slug"] = slug_hint or _slugify(topic)

    if hero_image and not out.get("heroImage"):
        out["heroImage"] = os.path.basename(hero_image)

    return out
