import re


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _truncate(s: str, max_len: int, min_len: int) -> str:
    if len(s) <= max_len:
        return s

    head = s[:max_len]

    # Prefer cutting on a word boundary, but only if we can stay >= min_len.
    last_space = head.rfind(" ")
    if last_space >= min_len:
        head = head[:last_space]

    return head.rstrip(" ,;:-")


def fit_meta_description(desc: str | None, *, fallback: str | None = None) -> str:
    """Force meta description to 155-160 chars (PDF requirement)."""

    s = _collapse_ws(_strip_tags(desc or ""))
    if not s:
        s = _collapse_ws(_strip_tags(fallback or ""))

    if not s:
        s = "Answer-first guide with benchmarks, examples, and checklists for 2026, including step-by-step tactics and common pitfalls to avoid."

    MIN_LEN = 155
    MAX_LEN = 160

    if MIN_LEN <= len(s) <= MAX_LEN:
        return s

    if len(s) > MAX_LEN:
        return _truncate(s, MAX_LEN, MIN_LEN)

    # Too short: deterministically enrich and then trim into the spec.
    base = s.rstrip(". ")

    enriched = (
        f"{base}: answer-first guide with benchmarks, examples, and checklists for 2026, including steps, pitfalls, and best practices."
    )
    enriched = _collapse_ws(enriched)

    if len(enriched) < MIN_LEN:
        enriched = _collapse_ws(enriched + " Covers creative testing, measurement, and execution.")

    # If still short, pad with a short token until we cross MIN_LEN, then trim.
    while len(enriched) < MIN_LEN:
        enriched = _collapse_ws(enriched.rstrip(". ") + " In 2026.")

    if len(enriched) > MAX_LEN:
        enriched = _truncate(enriched, MAX_LEN, MIN_LEN)

    # Ensure bounds even in worst-case edge scenarios.
    if len(enriched) < MIN_LEN:
        enriched = _collapse_ws(enriched + " In 2026.")
        if len(enriched) > MAX_LEN:
            enriched = _truncate(enriched, MAX_LEN, MIN_LEN)

    return enriched
