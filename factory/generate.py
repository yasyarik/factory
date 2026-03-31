import os
import re
import json
import urllib.request
from typing import Any

from .landing import _slugify
from .product_kb import rank_product_knowledge


def _site_origin() -> str:
    raw = (os.environ.get("SITE_ORIGIN") or "https://myugc.studio").strip()
    return raw.rstrip("/")


def _brand_name() -> str:
    return (os.environ.get("BRAND_NAME") or os.environ.get("SITE_NAME") or "My UGC Studio").strip() or "My UGC Studio"


SYSTEM_PROMPT = """
You are an expert ecommerce SEO and GEO writer.

Your task is to generate a high-quality editorial article for myugc.studio that is useful for readers, strong for organic search, and easy for AI systems to extract direct answers from.

The site focuses strictly on AI ecommerce content: product photography, UGC creatives, marketplace listing visuals, localization, and image-to-video workflows.

IMPORTANT OUTPUT RULE
- Output STRICT JSON only.
- Do not add explanations before or after the JSON.
- If any requirement is missing, silently fix the output and return corrected JSON.

WRITING STYLE
Write like a professional ecommerce editorial writer.

The article must feel like a polished editorial guide.

Rules:
- Zero fluff.
- No generic introductions.
- No filler transitions.
- Avoid academic or AI-style phrasing.
- Avoid abstract explanations when practical examples exist.
- Prefer concrete product, channel, and workflow examples.

The article must read like it was edited by a human ecommerce journalist.

ANTI-AI TEMPLATE RULES
The article must NOT read like a framework document or rule catalog.

Forbidden wording:
- Answer:
- Reasoning:
- Framework
- Decision layer
- Execution layer
- Scenario deep dive
- Progress tracking
- Comparison loop
- Workflow logic
- Methodology
- Optimization model

Forbidden pattern example:
"In this guide, X is a decision layer rather than..."

Each section must use different language patterns.

Do NOT repeat the same sentence structure across sections.

Avoid long lists of rules with identical explanations.

Prefer examples, comparisons, and real scenarios.

NATURAL LANGUAGE RULE
Avoid artificial phrases like:

"ensures harmony"
"practical channel strategy"
"transform the experience"
"moving beyond guesswork"

Use practical language instead.

TYPOGRAPHY / SYMBOL RULES
- NEVER use em dash or en dash symbols (—, –). Use short hyphen-minus (-) only.
- NEVER use any asterisks (*).
- Do not use markdown syntax.
- NEVER use smart quotes (“ ” « » ‘ ’).
- Use ASCII quotes only (" and ').
- NEVER use non-breaking spaces.
- NEVER output ellipsis symbol (…); use three dots (...) instead.
- NEVER output zero-width or BOM characters.
- Use <strong> sparingly for emphasis.

ARTICLE GOAL
The article must help the reader:

1. Understand the topic quickly
2. Make a practical content decision
3. Discover related guides on the site

LEAD PARAGRAPH RULE
contentHtml MUST start with a lead paragraph BEFORE the first H2.

Format:

<p><strong>Direct answer sentence.</strong> 1-3 supporting sentences explaining the topic.</p>

Rules:
- No generic intro.
- Do not restate the title.
- Start with useful information immediately.

STRUCTURE RULES
Use a clean editorial structure.

H2 sections:
- 6-9 total
- At least 3 H2 may be questions if natural

H3 sections:
- Use only when helpful
- Total H3 count usually 4-10
- Do not force H3 under every H2
- Avoid micro sections

SECTION DEPTH RULE
Each H2 section must contain meaningful explanation.

Minimum length:
150-220 words per section.

Do not generate tiny sections.

CONTENT DEPTH RULE
Content must include practical information.

Prefer:

- real ecommerce examples
- specific platforms and placements
- named channels and use cases
- real product categories
- real publishing scenarios

Avoid vague explanations.

REAL SCENARIO RULE
Include at least 6 practical scenarios when the topic allows.

Each scenario should include:

- situation
- recommended creative approach
- alternative option
- what to avoid
- short explanation

Examples of acceptable scenarios:

- Shopify product page refresh
- Amazon listing image update
- eBay listing revamp without photoshoot
- TikTok short-form ad test
- Pinterest creative batch for catalog
- Localization rollout for EU market

RICH CONTENT REQUIREMENTS
Include:

- at least 1 useful table
- at least 1 ordered list (<ol>)
- optionally 1 blockquote when meaningful
- 2-3 images

Image format:

<figure>
<img src="example.jpg" alt="..." />
<figcaption>...</figcaption>
</figure>

Rules:
- src must be filename only
- no URLs
- no directories

TABLE RULES
Tables must help decision making.

Example format:

Use Case | Best Creative Approach | What To Avoid

Decision tables should usually include 8-12 rows when topic allows.

LINKING RULES
Internal links:

- at least 5 links when contextLinks are available
- format: /blog/<slug>.html

Rules:
- natural anchors
- do not link to the current page
- avoid forced links

When relevant also link to:

/blog/
/pricing/

FAQ RULES
FAQ must contain 5-7 items.

Questions must sound like real reader questions.

Answers must be concise and must NOT repeat article paragraphs.

ECOMMERCE TOPIC RULE
Stay strictly within ecommerce content topics.

Allowed topics:

- product photography
- UGC content workflows
- Shopify and marketplace listings
- TikTok/Reels/Shorts creatives
- localization and multilingual assets
- image-to-video production

Forbidden drift:

- unrelated industries
- off-topic travel/food narratives
- generic AI tooling guides without ecommerce context
- engineering tutorials disconnected from content workflows

If technology appears, keep it directly tied to ecommerce content production and publishing.

TITLE RULES
Title must be natural and specific.

Avoid repetitive patterns like:

<Topic> 2026
Essential Guide
Expert Guide
Ultimate Guide

Prefer titles based on reader intent.

DESCRIPTION RULES
Description length:
150-160 characters.

Rules:

- must be a complete thought
- no fragments
- no filler phrases

SOURCE INPUT RULE
If sourceHtml is provided:

- preserve the meaning
- rewrite with better structure
- remove repetition
- add missing required elements

QUALITY CONTROL BEFORE OUTPUT
Before returning JSON verify:

1. No forbidden language patterns remain.
2. No repeated sentence templates exist.
3. H2 count between 6 and 9.
4. H3 count not excessive.
5. At least one useful table exists.
6. At least 6 real scenarios when topic allows.
7. No self-links.
8. Description is 150-160 characters.
9. The article reads like a human editorial ecommerce guide.

OUTPUT JSON SHAPE

{
  "slug": "string",
  "title": "string",
  "description": "string",
  "category": "string",
  "heroImage": "string",
  "contentHtml": "string",
  "faq": [
    {"question":"...","answer":"..."}
  ]
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

RESEARCH_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "queries": {"type": "ARRAY", "items": {"type": "STRING"}},
        "sources": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "url": {"type": "STRING"},
                },
                "required": ["title", "url"],
            },
        },
        "facts": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["queries", "sources", "facts"],
}

WRITER_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "slug": {"type": "STRING"},
        "title": {"type": "STRING"},
        "description": {"type": "STRING"},
        "category": {"type": "STRING"},
        "heroImage": {"type": "STRING"},
        "contentHtml": {"type": "STRING"},
        "faq": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "question": {"type": "STRING"},
                    "answer": {"type": "STRING"},
                },
                "required": ["question", "answer"],
            },
        },
    },
    "required": ["slug", "title", "description", "category", "heroImage", "contentHtml", "faq"],
}

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

    response_schema = RESEARCH_RESPONSE_SCHEMA if system == RESEARCH_PROMPT else WRITER_RESPONSE_SCHEMA
    payload: dict[str, Any] = {
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
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

def _sanitize_json_text(s: str) -> str:
    """Escape raw control chars inside JSON string literals.

    Gemini occasionally returns unescaped control characters inside string
    values (most often in large HTML fragments), which breaks json.loads().
    """
    if not s:
        return s

    out: list[str] = []
    in_string = False
    escaped = False

    for ch in s:
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            continue

        if escaped:
            out.append(ch)
            escaped = False
            continue

        if ch == "\\":
            out.append(ch)
            escaped = True
            continue

        if ch == '"':
            out.append(ch)
            in_string = False
            continue

        code = ord(ch)
        if code < 0x20:
            if code == 10:
                out.append("\\n")
            elif code == 13:
                out.append("\\r")
            elif code == 9:
                out.append("\\t")
            elif code == 8:
                out.append("\\b")
            elif code == 12:
                out.append("\\f")
            else:
                out.append(f"\\u{code:04x}")
            continue

        out.append(ch)

    return ''.join(out)


def _extract_first_balanced_json_object(s: str) -> str | None:
    start = s.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == '{':
            depth += 1
            continue
        if ch == '}':
            depth -= 1
            if depth == 0:
                return s[start:i+1]

    return None


def _normalize_json_candidate(s: str) -> str:
    if not s:
        return s
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def _parse_json_strict(s: str) -> dict[str, Any]:
    s = (s or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)

    candidates: list[str] = [s]

    # Model sometimes adds leading/trailing text or extra lines.
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        clipped = s[start : end + 1]
        if clipped != s:
            candidates.append(clipped)

    balanced = _extract_first_balanced_json_object(s)
    if balanced and balanced not in candidates:
        candidates.append(balanced)

    seen: set[str] = set()
    last_err: Exception | None = None

    for cand in candidates:
        variants = [cand]

        normalized = _normalize_json_candidate(cand)
        if normalized != cand:
            variants.append(normalized)

        for variant in variants:
            if variant in seen:
                continue
            seen.add(variant)

            try:
                return json.loads(variant)
            except Exception as e:
                last_err = e

            sanitized = _sanitize_json_text(variant)
            if sanitized != variant:
                try:
                    return json.loads(sanitized)
                except Exception as e:
                    last_err = e

    if last_err is not None:
        raise last_err
    raise ValueError("Failed to parse model JSON output")



def generate_draft(
    *,
    topic: str,
    existing_posts: list[dict[str, str]],
    category: str | None,
    hero_image: str | None,
    slug_hint: str | None,
    source_html: str | None = None,
    product_mode: bool = False,
    engagement_mode: bool = False,
    lead_magnet_mode: bool = False,
    previous: dict[str, Any] | None = None,
    problems: list[str] | None = None,
    **_extra: Any,
) -> dict[str, Any]:
    context = _rank_context(topic, existing_posts, limit=20)

    user: dict[str, Any] = {
        "topic": topic,
        "categoryHint": category,
        "heroImageHint": hero_image,
        "sourceHtml": (source_html or "")[:25000],
        "slugHint": slug_hint,
        "contextLinks": context,
        "site": _site_origin(),
        "basePath": "/blog/",
    }

    user["brandName"] = _brand_name()
    user["modes"] = {
        "product": bool(product_mode),
        "engagement": bool(engagement_mode),
        "leadMagnet": bool(lead_magnet_mode),
    }


    if product_mode:
        product_knowledge = rank_product_knowledge(topic, limit=14)
        if product_knowledge:
            user["productKnowledge"] = product_knowledge

    # Reuse existing server env conventions (SaaS/API use GOOGLE_API_KEY).
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    if not api_key:
        # Deterministic fallback so preview/publish flow can be tested.
        slug = slug_hint or _slugify(topic)
        return {
            "slug": slug,
            "title": topic,
            "description": (topic + " - practical guide with examples, checklist, and FAQ.")[:160],
            "category": category or "Buying Guides",
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
                "<h2>How to optimize this approach?</h2>"
                "<p><strong>Use updated examples and add schema.</strong></p>"
                "<h2>Which tools help?</h2>"
                "<p><strong>Use AI plus validation and a publish checklist.</strong></p>"
                "<h2>How to measure results?</h2>"
                "<p><strong>Track impressions, clicks, and rankings.</strong></p>"
            ),
            "faq": [
                {"question": "What is the main takeaway?", "answer": "Structure and answer-first improves readability."},
                {"question": "Do I need schema?", "answer": "FAQ schema can help rich results."},
                {"question": "How many internal links?", "answer": "Use 1 hub link, 2-4 cluster links, and one next-step link."},
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
                json.dumps({"topic": topic, "site": _site_origin(), "brand": _brand_name()}),
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
    try:
        out = _parse_json_strict(text)
    except Exception as e:
        sample = (text or "").replace("\n", " ").replace("\r", " ")[:700]
        raise ValueError(f"{e}; model_output_sample={sample}") from e

    sources, queries = _extract_sources(grounding)
    out["sources"] = research_sources or sources
    out["searchQueries"] = research_queries or queries

    if not out.get("slug"):
        out["slug"] = slug_hint or _slugify(topic)

    if hero_image and not out.get("heroImage"):
        out["heroImage"] = os.path.basename(hero_image)

    return out
