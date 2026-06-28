from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


USER_AGENT = "myugc-content-factory-topic-discovery/1.0"

BAD_TOPIC_PHRASES = (
    "frankly shocking",
    "what kind of business model",
    "don't pay for the upgrade",
    "later addressed",
    "reversed course",
    "wine dance",
    "wine your waist",
    "while pregnant",
    "during pregnancy",
    "wine diet",
    "wine weight loss",
)



GENERIC_DIRECTION_TOKENS = {
    "content", "guide", "best", "how", "what", "why", "with", "using", "tool", "tools",
    "tips", "ideas", "strategy", "strategies", "workflow", "marketing", "article", "articles"
}

PLATFORM_DIRECTION_TOKENS = {
    "shopify", "tiktok", "youtube", "pinterest", "instagram", "facebook", "linkedin", "amazon", "ebay", "etsy"
}

BUSINESS_DIRECTION_TOKENS = {
    "ugc", "ecommerce", "dropshipping", "ads", "creative", "creatives", "product", "products", "photos", "images", "videos"
}


def _direction_anchor_tokens(direction: str) -> set[str]:
    return {t for t in _tokens(direction) if len(t) >= 4 and t not in GENERIC_DIRECTION_TOKENS}

def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())



def _key(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s



def _tokens(s: str) -> set[str]:
    s = re.sub(r"[^a-z0-9\s]", " ", (s or "").lower())
    return {w for w in s.split() if len(w) > 2}



def _http_get_json(url: str, timeout: int = 18) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)



def _clean_candidate_topic(raw_topic: str, direction: str) -> str | None:
    t = _norm_space(raw_topic)
    if not t:
        return None

    # Remove markdown/noise artifacts.
    t = t.replace("*", " ").replace("`", " ")
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip(' -|"\'[]{}')

    if any(ch in t for ch in "[]{}"):
        return None
    if "', '" in t or '", "' in t or "\"" in t:
        return None

    # Fix obvious broken auto-suggest fragments.
    t = re.sub(r"(?i)^what i\b", "What is", t)
    t = re.sub(r"(?i)\bwhat i\b", "what is", t)

    lo = t.lower()
    if any(b in lo for b in BAD_TOPIC_PHRASES):
        return None
    if "be like" in lo or re.search(r"@\w+", t) or re.search(r"\bpt\s*\d+\b", lo):
        return None
    if re.search(r"\b(racist|publisher|celebrity|movie|actor|actress|singer|gaming|streamer|drama|gossip|scandal)\b", lo):
        return None
    if re.search(r"\b(night in the woods|tunic|doctored|lying)\b", lo):
        return None
    if re.match(r"^\s*\d+\s+(of\s+)?(the\s+)?best\b", lo):
        return None
    if re.search(r"\bbest\s+\w+\s+(bar|bars|restaurant|restaurants|festival|festivals)\b", lo):
        return None

    # Too many sentence fragments usually means forum rant, not article title.
    if len(re.findall(r"[.!?]", t)) > 2:
        parts = [x.strip() for x in re.split(r"[.!?]+\s+", t) if x.strip()]
        t = parts[0] if parts else t

    # Drop explicit first-person complaint style lines.
    if lo.startswith(("i ", "we ", "my ", "our ")) and len(t) > 70:
        return None

    # Keep clean length for title candidates.
    if len(t) > 100:
        t = t[:100].rsplit(" ", 1)[0].strip()

    t = _to_topic_phrase(t)
    t = _normalize_topic_case(t)

    # Basic quality gates.
    if len(t) < 14 or len(t) > 100:
        return None
    if len(re.findall(r"[A-Za-z]", t)) < 10:
        return None

    # Ensure topic still relates to direction.
    dt = _tokens(direction)
    tt = _tokens(t)
    if dt and not (dt & tt):
        return None

    anchors = _direction_anchor_tokens(direction)
    if anchors and not (anchors & tt):
        return None

    # Enforce stronger direction relevance for platform/business intents.
    dlow = (direction or "").lower()
    platform_in_direction = {tok for tok in PLATFORM_DIRECTION_TOKENS if tok in dlow}
    if platform_in_direction and not (platform_in_direction & tt):
        return None

    business_in_direction = {tok for tok in BUSINESS_DIRECTION_TOKENS if tok in dlow}
    if business_in_direction and not (business_in_direction & tt):
        return None

    # For long directions, require at least 2 overlaps with meaningful tokens.
    meaningful = {tok for tok in dt if tok not in GENERIC_DIRECTION_TOKENS and tok not in {"in", "for", "and", "the"}}
    if len(meaningful) >= 4 and len(meaningful & tt) < 2:
        return None

    return t


def _to_topic_phrase(q: str) -> str:
    q = _norm_space(q)
    if not q:
        return q
    q = q.rstrip(".")
    if q.endswith("?"):
        return q[0].upper() + q[1:]

    lower = q.lower()
    starters = (
        "how ",
        "why ",
        "what ",
        "when ",
        "where ",
        "which ",
        "who ",
        "is ",
        "are ",
        "can ",
        "should ",
        "do ",
        "does ",
        "best ",
    )
    if lower.startswith(starters):
        q = q + "?"
    return q[0].upper() + q[1:]



def _normalize_topic_case(topic: str) -> str:
    t = _norm_space(topic)
    letters = re.sub(r"[^A-Za-z]", "", t)
    if letters:
        upper_ratio = sum(1 for ch in letters if ch.isupper()) / max(1, len(letters))
        if upper_ratio >= 0.68:
            t = t.title()
    return t


def _infer_discovery_category(topic: str, category_hint: str | None = None) -> str:
    t = (topic or "").lower()
    h = (category_hint or "").lower()
    src = f"{t} {h}"

    if re.search(r"(winery|wineries|vineyard|travel|route|tour|bodega|viaje|weingut|reisen|винодель|путешеств)", src):
        return "Wineries & Travel"
    if re.search(r"(region|terroir|appellation|map|rioja|tuscany|bordeaux|регион|терруар)", src):
        return "Wine Regions"
    if re.search(r"(grape|variet|viticulture|uva|uvas|cépage|cepage|rebsorte|виноград|сорт)", src):
        return "Grape Varieties"
    if re.search(r"(pair|pairing|food|dish|meal|maridaje|comida|accord|еда|блюд|сочет)", src):
        return "Food Pairing"
    return "Buying Guides"


def _infer_intent(topic: str) -> str:
    t = (topic or "").lower()
    if t.startswith(("how", "best", "guide", "steps")):
        return "Informational"
    if t.startswith(("vs", "compare", "which", "what is better")) or " vs " in t:
        return "Commercial investigation"
    if any(x in t for x in ["price", "cost", "buy", "tool", "software"]):
        return "Commercial"
    return "Informational"



def _relative_fresh(created_utc: float | int | None) -> float:
    if not created_utc:
        return 0.5
    now = time.time()
    days = max(0.0, (now - float(created_utc)) / 86400.0)
    return max(0.0, min(1.0, 1.0 - (days / 90.0)))



def _fetch_reddit(direction: str, limit: int = 80) -> list[dict[str, Any]]:
    q = urllib.parse.quote(direction)
    # month keeps it current; sort=relevance+top catches recurring high-signal threads.
    urls = [
        f"https://www.reddit.com/search.json?q={q}&sort=top&t=month&limit=50",
        f"https://www.reddit.com/search.json?q={q}&sort=relevance&t=month&limit=50",
    ]

    out: list[dict[str, Any]] = []
    for url in urls:
        try:
            data = _http_get_json(url)
        except Exception:
            continue

        children = (((data or {}).get("data") or {}).get("children") or [])
        for ch in children:
            d = (ch or {}).get("data") or {}
            title = _norm_space(d.get("title") or "")
            if len(title) < 14:
                continue

            ups = int(d.get("ups") or 0)
            comments = int(d.get("num_comments") or 0)
            created_utc = d.get("created_utc")
            permalink = d.get("permalink") or ""
            source_url = f"https://www.reddit.com{permalink}" if permalink else "https://www.reddit.com"

            out.append(
                {
                    "question": _to_topic_phrase(title),
                    "source": {
                        "title": "Reddit thread",
                        "url": source_url,
                    },
                    "engagement": max(1, ups + comments * 2),
                    "fresh": _relative_fresh(created_utc),
                    "origin": "reddit",
                }
            )

            selftext = d.get("selftext") or ""
            if selftext:
                candidates = re.findall(r"([^\n\r\?]{10,220}\?)", selftext)
                for c in candidates[:2]:
                    cq = _to_topic_phrase(c)
                    if len(cq) < 16:
                        continue
                    out.append(
                        {
                            "question": cq,
                            "source": {"title": "Reddit discussion", "url": source_url},
                            "engagement": max(1, comments),
                            "fresh": _relative_fresh(created_utc),
                            "origin": "reddit",
                        }
                    )

    # hard cap to keep processing predictable
    return out[:limit]



def _normalize_source_sites(source_sites: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in (source_sites or []):
        s = (raw or "").strip().lower()
        s = re.sub(r"^https?://", "", s)
        s = s.split("/", 1)[0].strip()
        if not s or "." not in s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out[:20]


def _fetch_google_suggest(direction: str, limit: int = 80, source_sites: list[str] | None = None) -> list[dict[str, Any]]:
    source_sites = _normalize_source_sites(source_sites)
    queries = [
        direction,
        f"how to {direction}",
        f"best {direction}",
        f"why {direction}",
        f"what is {direction}",
    ]
    for d in source_sites:
        queries.extend([
            f"site:{d} {direction}",
            f"site:{d} best {direction}",
            f"site:{d} how to {direction}",
        ])

    out: list[dict[str, Any]] = []
    for q in queries:
        try:
            url = (
                "https://suggestqueries.google.com/complete/search?client=firefox&hl=en&q="
                + urllib.parse.quote(q)
            )
            data = _http_get_json(url)
            suggestions = data[1] if isinstance(data, list) and len(data) > 1 else []
        except Exception:
            suggestions = []

        for s in suggestions[:20]:
            text = _to_topic_phrase(str(s))
            if len(text) < 12:
                continue
            search_url = "https://www.google.com/search?q=" + urllib.parse.quote(str(s))
            out.append(
                {
                    "question": text,
                    "source": {"title": "Google suggest", "url": search_url},
                    "engagement": 4,
                    "fresh": 0.85,
                    "origin": "google_suggest",
                }
            )

    return out[:limit]



def _fetch_duckduckgo(direction: str, limit: int = 50, source_sites: list[str] | None = None) -> list[dict[str, Any]]:
    source_sites = _normalize_source_sites(source_sites)
    out: list[dict[str, Any]] = []
    queries = [direction]
    for d in source_sites:
        queries.append(f"site:{d} {direction}")

    for q in queries[:8]:
        try:
            url = "https://duckduckgo.com/ac/?q=" + urllib.parse.quote(q) + "&type=list"
            data = _http_get_json(url)
        except Exception:
            data = []
        items = data if isinstance(data, list) else []
        for it in items[:25]:
            raw_phrase=it.get("phrase") if isinstance(it, dict) else str(it or "")
            raw_phrase = re.sub(r'^[\[\]\(\)"\'`]+|[\[\]\(\)"\'`]+$', '', raw_phrase).strip()
            phrase = _to_topic_phrase(raw_phrase)
            if len(phrase) < 12:
                continue
            out.append(
                {
                    "question": phrase,
                    "source": {
                        "title": "DuckDuckGo suggest",
                        "url": "https://duckduckgo.com/?q=" + urllib.parse.quote(phrase),
                    },
                    "engagement": 3,
                    "fresh": 0.8,
                    "origin": "duckduckgo",
                }
            )

    return out[:limit]



def discover_topics(*, direction: str, limit: int = 20, category_hint: str | None = None, source_sites: list[str] | None = None) -> dict[str, Any]:
    direction = _norm_space(direction)
    if len(direction) < 3:
        raise ValueError("direction must be at least 3 characters")
    source_sites = _normalize_source_sites(source_sites)

    raw: list[dict[str, Any]] = []
    raw.extend(_fetch_reddit(direction))
    raw.extend(_fetch_google_suggest(direction, source_sites=source_sites))
    raw.extend(_fetch_duckduckgo(direction, source_sites=source_sites))

    if not raw:
        return {
            "direction": direction,
            "items": [],
            "diagnostics": {"rawCount": 0, "uniqueCount": 0, "sources": []},
        }

    dir_tokens = _tokens(direction)
    anchor_tokens = _direction_anchor_tokens(direction)

    agg: dict[str, dict[str, Any]] = {}
    for item in raw:
        q = _clean_candidate_topic(item.get("question") or "", direction)
        if not q:
            continue
        k = _key(q)
        if not k:
            continue

        src = item.get("source") or {}
        if k not in agg:
            agg[k] = {
                "topic": q,
                "engagement": 0.0,
                "fresh": 0.0,
                "sources": [],
                "origins": set(),
            }

        node = agg[k]
        node["engagement"] = max(float(node["engagement"]), float(item.get("engagement") or 0.0))
        node["fresh"] = max(float(node["fresh"]), float(item.get("fresh") or 0.0))

        src_url = (src.get("url") or "").strip()
        if src_url and not any((s.get("url") == src_url) for s in node["sources"]):
            node["sources"].append({"title": src.get("title") or "Source", "url": src_url})

        origin = (item.get("origin") or "").strip()
        if origin:
            node["origins"].add(origin)

    scored: list[dict[str, Any]] = []
    for _, node in agg.items():
        topic = _clean_candidate_topic(node["topic"], direction)
        if not topic:
            continue
        topic_tokens = _tokens(topic)

        # Weighted scoring (recency + engagement + cross-source + business fit)
        recency = max(0.0, min(1.0, float(node["fresh"])))
        eng = max(0.0, float(node["engagement"]))
        eng_norm = min(1.0, math.log1p(eng) / math.log1p(1500.0))
        cross = min(1.0, len(node["origins"]) / 3.0)
        fit = 0.0
        match_count = 0
        if dir_tokens:
            match_count = len(dir_tokens & topic_tokens)
            fit = match_count / max(1, len(dir_tokens))
            fit = max(0.0, min(1.0, fit))

        # Keep ideas relevant to the requested direction.
        if dir_tokens:
            required_matches = 1
            if match_count < required_matches:
                continue
            if fit < 0.08:
                continue

        score = (0.35 * recency) + (0.30 * eng_norm) + (0.20 * cross) + (0.15 * fit)

        why = []
        if recency >= 0.65:
            why.append("fresh signal")
        if eng_norm >= 0.35:
            why.append("high engagement")
        if cross >= 0.67:
            why.append("appears across sources")
        why_now = ", ".join(why) if why else "validated in current query trends"

        scored.append(
            {
                "topic": topic,
                "intent": _infer_intent(topic),
                "score": round(score * 100.0, 1),
                "whyNow": why_now,
                "category": _infer_discovery_category(topic, category_hint),
                "sourceCount": len(node["sources"]),
                "sources": node["sources"][:3],
            }
        )

    scored.sort(key=lambda x: (x.get("score") or 0), reverse=True)

    cap = max(5, min(30, int(limit or 20)))
    items = scored[:cap]

    if len(items) < max(3, cap // 2):
        # Fallback: include additional high-fit candidates even with low score.
        fallback = []
        for node in agg.values():
            topic = _clean_candidate_topic(node.get("topic") or "", direction)
            if not topic:
                continue
            fit2 = 0.0
            tt = _tokens(topic)
            match2 = 0
            if dir_tokens:
                match2 = len(dir_tokens & tt)
                fit2 = match2 / max(1, len(dir_tokens))
            required2 = 1
            has_anchor = (not anchor_tokens) or bool(anchor_tokens & tt)
            if match2 >= required2 and has_anchor and fit2 >= 0.12 and not any(x.get("topic") == topic for x in items):
                fallback.append(
                    {
                        "topic": topic,
                        "intent": _infer_intent(topic),
                        "score": round(25 + fit2 * 40, 1),
                        "whyNow": "trend-adjacent query from current sources",
                        "category": _infer_discovery_category(topic, category_hint),
                        "sourceCount": len(node.get("sources") or []),
                        "sources": (node.get("sources") or [])[:3],
                    }
                )
        fallback.sort(key=lambda x: (x.get("score") or 0), reverse=True)
        items.extend(fallback[: max(0, cap - len(items))])

    items = items[:cap]

    # Final de-duplication by normalized topic.
    dedup = []
    seen_topics = set()
    for it in items:
        k = _key(it.get("topic") or "")
        if not k or k in seen_topics:
            continue
        seen_topics.add(k)
        dedup.append(it)
    items = dedup

    # If external signals are sparse, synthesize safe topic variants from direction.
    if len(items) < cap:
        base = _normalize_topic_case(_to_topic_phrase(direction))
        variants = [
            f"{base}: best routes and wineries",
            f"How to plan a {base} weekend?",
            f"{base} budget breakdown",
            f"{base} seasonal calendar: when to go",
            f"{base} itinerary mistakes to avoid",
            f"{base} tasting checklist for beginners",
            f"{base} transport and logistics guide",
            f"{base} with food pairing stops: practical plan",
            f"{base} 3-day vs 7-day route comparison",
            f"{base} family-friendly route options",
            f"{base} hidden gems and local producers",
            f"{base} booking timeline and reservations",
        ]
        for v in variants:
            t = _clean_candidate_topic(v, direction)
            if not t:
                continue
            k = _key(t)
            if not k or any(_key(x.get("topic") or "") == k for x in items):
                continue
            items.append(
                {
                    "topic": t,
                    "intent": _infer_intent(t),
                    "score": 35.0,
                    "whyNow": "synthetic fallback from direction seed",
                    "category": _infer_discovery_category(t, category_hint),
                    "sourceCount": 0,
                    "sources": [],
                }
            )
            if len(items) >= cap:
                break

    seen_sources = []
    seen_keys = set()
    for it in items:
        for s in (it.get("sources") or []):
            u = s.get("url") or ""
            if u and u not in seen_keys:
                seen_keys.add(u)
                seen_sources.append(s)

    return {
        "direction": direction,
        "items": items,
        "diagnostics": {
            "rawCount": len(raw),
            "uniqueCount": len(scored),
            "sources": seen_sources[:10],
            "sourceSites": source_sites,
            "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        },
    }
