import re


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


DANGLING_WORDS = {
    "and", "or", "with", "for", "to", "of", "in", "on", "at", "from", "by", "as",
    "und", "et", "y", "e", "de", "del", "la", "le", "da", "di", "con",
    "и", "с", "на", "по", "для", "а", "или",
    "perfect", "discover", "explore", "key", "top", "best", "including",
}


def _truncate(s: str, max_len: int, min_len: int) -> str:
    if len(s) <= max_len:
        return s

    # Base trim to max length.
    head = s[:max_len]

    # Prefer cutting on a word boundary, but only if we can stay >= min_len.
    last_space = head.rfind(" ")
    if last_space >= min_len:
        head = head[:last_space]

    head = head.rstrip(" ,;:-")

    # Avoid dangling conjunction/preposition at the end (e.g., "... and").
    # Guard: never let this cleanup drop below min_len.
    words = head.split()
    while len(words) > 1 and words[-1].lower() in DANGLING_WORDS:
        candidate = " ".join(words[:-1]).rstrip(" ,;:-")
        if len(candidate) < min_len:
            break
        words = words[:-1]
    out = " ".join(words).rstrip(" ,;:-")

    # Safety net: if stylistic cleanup made text too short, keep hard max trim.
    if len(out) < min_len:
        out = s[:max_len].rstrip(" ,;:-")
        if len(out) < min_len:
            out = s[:min_len].rstrip(" ,;:-")

    return out


def fit_meta_description(desc: str | None, *, fallback: str | None = None) -> str:
    """Normalize meta description for clean SEO snippet without truncation artifacts."""

    s = _collapse_ws(_strip_tags(desc or ""))
    if not s:
        s = _collapse_ws(_strip_tags(fallback or ""))

    if not s:
        s = "Practical 2026 guide covering regions, grapes, producers, and food pairings with clear recommendations for tasting and buying."

    MIN_LEN = 145
    MAX_LEN = 158

    if MIN_LEN <= len(s) <= MAX_LEN:
        return s

    if len(s) > MAX_LEN:
        return _truncate(s, MAX_LEN, MIN_LEN)

    # If shorter than target, keep original meaning (no synthetic padding).
    # This prevents template-like tails and keeps language natural.
    return s
