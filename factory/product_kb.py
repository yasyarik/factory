from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


def _tokens(s: str) -> set[str]:
    s = re.sub(r"[^a-z0-9\\s]", " ", (s or "").lower())
    return {p for p in s.split() if len(p) > 2}


@lru_cache(maxsize=1)
def load_product_kb() -> list[dict[str, Any]]:
    candidates = [
        os.environ.get("PRODUCT_KB_PATH", "").strip(),
        "/var/www/my-ugc-studio-saas/api/tenants/web.myugc.studio/faq.json",
        "/var/www/my-ugc-studio-saas/src/lib/ai/tenants/saas/faq.json",
        "/var/www/my-ugc-studio-saas/api/public/faq.json",
    ]
    for p in candidates:
        if not p:
            continue
        fp = Path(p)
        if not fp.exists():
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except Exception:
            continue
    return []


def extract_en_answer(answer: str) -> str:
    a = (answer or "").strip()
    if not a:
        return ""
    m = re.search(r"EN:\\s*(.+?)(?:\\s*/\\s*RU:|$)", a, flags=re.I | re.S)
    if m:
        a = m.group(1).strip()
    # Fallback split for mixed bilingual formats.
    a = re.split(r"\\s*/\\s*RU:\\s*", a, maxsplit=1, flags=re.I)[0].strip()
    a = re.sub(r"\\[COMMAND:[^\\]]+\\]", "", a).strip()
    a = re.sub(r"\\s+", " ", a)
    return a


def rank_product_knowledge(topic: str, limit: int = 12) -> list[dict[str, Any]]:
    kb = load_product_kb()
    if not kb:
        return []

    tt = _tokens(topic)
    ranked: list[tuple[int, dict[str, Any]]] = []

    for node in kb:
        kws = node.get("keywords") or []
        if not isinstance(kws, list):
            continue
        kw_text = " ".join(str(k) for k in kws)
        score = len(tt & _tokens(kw_text))
        fact = extract_en_answer(str(node.get("answer") or ""))
        if not fact:
            continue
        ranked.append((score, {"keywords": kws[:12], "fact": fact}))

    ranked.sort(key=lambda x: x[0], reverse=True)
    out = [item for score, item in ranked if score > 0][:limit]
    if out:
        return out

    # Fallback for very broad topic seeds.
    fallback: list[dict[str, Any]] = []
    for _, item in ranked[:limit]:
        if item.get("fact"):
            fallback.append(item)
    return fallback
