import html
import os
import re
from datetime import datetime, timezone
from typing import Any

import requests


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_dashes(text: str) -> str:
    t = text or ""
    t = t.replace("—", "-").replace("–", "-")
    return t


def _strip_html_to_text(html_text: str) -> str:
    s = (html_text or "")
    s = re.sub(r"(?is)<script.*?</script>", " ", s)
    s = re.sub(r"(?is)<style.*?</style>", " ", s)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</p>", "\n\n", s)
    s = re.sub(r"(?is)</h[23]>", "\n\n", s)
    s = re.sub(r"(?is)<li[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = _normalize_dashes(s)
    return s.strip()


def _truncate(s: str, max_len: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    sp = cut.rfind(" ")
    if sp > int(max_len * 0.7):
        cut = cut[:sp]
    return cut.rstrip(" ,;:-")


def _split_text_for_telegram(text: str, max_len: int = 3900) -> list[str]:
    s = (text or "").strip()
    if not s:
        return []
    if len(s) <= max_len:
        return [s]

    parts: list[str] = []
    while len(s) > max_len:
        cut = s[:max_len]
        bp = cut.rfind("\n\n")
        if bp < int(max_len * 0.5):
            bp = cut.rfind("\n")
        if bp < int(max_len * 0.5):
            bp = cut.rfind(" ")
        if bp < int(max_len * 0.5):
            bp = max_len
        part = s[:bp].strip()
        if part:
            parts.append(part)
        s = s[bp:].strip()
    if s:
        parts.append(s)
    return parts


def _to_telegram_html(text: str) -> str:
    src = text or ""
    out: list[str] = []
    pos = 0
    for m in re.finditer(r"\*\*([^*\n][^*]*?)\*\*", src):
        out.append(html.escape(src[pos:m.start()]))
        out.append("<b>" + html.escape(m.group(1).strip()) + "</b>")
        pos = m.end()
    out.append(html.escape(src[pos:]))
    return "".join(out)


def _cleanup_post_text(text: str) -> str:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"^```(?:text|markdown)?\s*", "", t.strip(), flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = _normalize_dashes(t)
    return t.strip()


def _append_link_before_hashtags(text: str, url: str) -> str:
    out = (text or "").strip()
    link = (url or "").strip()
    if not link or (link in out):
        return out

    m = re.search(r"(?im)^хэштеги\s*:\s*", out)
    if not m:
        return out + "\n\n" + link

    idx = m.start()
    return out[:idx].rstrip() + "\n\n" + link + "\n\n" + out[idx:].lstrip()


def _cyrillic_ratio(text: str) -> float:
    chars = re.findall(r"[A-Za-zА-Яа-яЁё]", text or "")
    if not chars:
        return 0.0
    cyr = [c for c in chars if re.match(r"[А-Яа-яЁё]", c)]
    return len(cyr) / len(chars)


def _extract_sentences(body: str, limit: int = 18) -> list[str]:
    txt = _normalize_dashes(body or "")
    txt = re.sub(r"\s+", " ", txt)
    raw = re.split(r"(?<=[.!?])\s+", txt)
    out: list[str] = []
    for s in raw:
        s = s.strip(" \n\t-•")
        if len(s) < 45:
            continue
        if s in out:
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out



def _validate_ru_post(text: str, *, include_link: bool, url: str) -> list[str]:
    errs: list[str] = []
    t = _cleanup_post_text(text)

    if not t:
        return ["empty result"]

    if len(t) < 1200:
        errs.append(f"too short ({len(t)} chars), expected >= 1200")
    if len(t) > 3600:
        errs.append(f"too long ({len(t)} chars), expected <= 3600")

    if "—" in t or "–" in t:
        errs.append("contains long dash")

    if re.search(r"[A-Za-zА-Яа-я0-9]{90,}", t):
        errs.append("contains merged text without spaces")

    ratio = _cyrillic_ratio(t)
    if ratio < 0.55:
        errs.append(f"not russian enough (cyr ratio={ratio:.2f}, need >=0.55)")

    numbered = len(re.findall(r"(?m)^\s*[1-9]\.\s+", t))
    if numbered < 6:
        errs.append(f"not enough numbered points ({numbered}), need >= 6")

    if "### Вывод" not in t:
        errs.append("missing section: ### Вывод")

    hashtags_match = re.search(r"(?im)^Хэштеги\s*:\s*(.+)$", t)
    if not hashtags_match:
        errs.append("missing hashtags line at the end")
    else:
        tags = re.findall(r"#[\w\-]+", hashtags_match.group(1))
        if len(tags) < 8:
            errs.append(f"not enough hashtags ({len(tags)}), need >= 8")
        tail = t[hashtags_match.start():].strip()
        if "\n" in tail and not tail.startswith("Хэштеги"):
            errs.append("hashtags block must be final")

    if include_link and url and (url not in t):
        errs.append("missing required article link")

    if not re.search(r"[.!?…]$", t) and not re.search(r"#[\w\-]+\s*$", t):
        errs.append("post seems truncated (bad ending)")

    return errs


def _generate_ru_post(api_key: str, model: str, *, title: str, description: str, body: str, include_link: bool, url: str) -> str:
    base_prompt = (
        "Ты редактор Telegram-канала. Нужен длинный, цельный, практичный пост по статье. "
        "Основа: текст источника ниже. Сохрани факты и суть, пиши на русском. "
        "Бренд My UGC Studio не переводи. "
        "Не используй длинное тире, используй обычный дефис.\n\n"
        "ЖЕСТКАЯ СТРУКТУРА ОТВЕТА:\n"
        "1) Заголовок.\n"
        "2) Абзац: 'Почему это важно: ...'\n"
        "3) Ровно 6 нумерованных пунктов формата '1. ...' .. '6. ...', каждый пункт 2-4 предложения, конкретика и польза.\n"
        "4) Блок '### Вывод' и 3-5 предложений.\n"
        "5) Фраза с призывом к обсуждению.\n"
        "6) Последняя строка: 'Хэштеги: ...' минимум 8 хэштегов.\n"
        "Ограничения: 1400-3200 символов, никаких обрывов, без смешения языков."
    )

    user = f"TITLE: {title}\nDESCRIPTION: {description}\nSOURCE:\n{body}\n"
    url_api = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    feedback = ""
    last_err = None
    last_text = ""

    for attempt in range(1, 5):
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": base_prompt + "\n\n" + feedback + "\n" + user}]}],
            "generationConfig": {"temperature": 0.55, "maxOutputTokens": 4096},
        }
        try:
            r = requests.post(url_api, json=payload, timeout=55)
            if r.status_code >= 400:
                last_err = RuntimeError(f"Gemini telegram generate failed: {r.status_code} {r.text}")
                continue
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            text = _cleanup_post_text(text)
            if include_link and url:
                text = _append_link_before_hashtags(text, url)

            last_text = text
            errors = _validate_ru_post(text, include_link=include_link, url=url)
            if not errors:
                return text

            feedback = (
                "Исправь полностью. Ошибки в прошлом варианте:\n- "
                + "\n- ".join(errors)
                + "\nСделай новый корректный вариант строго по структуре."
            )
            last_err = RuntimeError("; ".join(errors))
        except Exception as e:
            last_err = e
            continue

    if last_text.strip():
        return _cleanup_post_text(last_text)

    if last_err:
        raise RuntimeError(str(last_err))
    raise RuntimeError("Gemini telegram generation failed")


def build_telegram_post_ru(*, title: str, description: str, content_html: str, url: str, include_link: bool = True) -> str:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = os.environ.get("GEMINI_TEXT_MODEL") or os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY/GOOGLE_API_KEY for Telegram generation")

    body = _strip_html_to_text(content_html)
    body = _truncate(body, 13000)

    out = _generate_ru_post(
        api_key,
        model,
        title=title,
        description=description,
        body=body,
        include_link=include_link,
        url=url,
    ).strip()

    out = _cleanup_post_text(out)
    out = _normalize_dashes(out)

    final_errors = _validate_ru_post(out, include_link=include_link, url=url)
    if final_errors:
        raise RuntimeError('Telegram post validation failed: ' + '; '.join(final_errors))

    return out


def telegram_send(*, bot_token: str, chat_id: str, text: str, photo_abs_path: str | None = None, hero_public_url: str | None = None) -> dict[str, Any]:
    base = f"https://api.telegram.org/bot{bot_token}"

    full_text = (text or "").strip()
    if not full_text:
        raise RuntimeError("Telegram text is empty")

    sent_photo = None
    # Always send article image first when available.
    if photo_abs_path and os.path.exists(photo_abs_path):
        with open(photo_abs_path, "rb") as f:
            rp = requests.post(
                base + "/sendPhoto",
                data={"chat_id": chat_id},
                files={"photo": f},
                timeout=90,
            )
        if rp.status_code >= 400:
            raise RuntimeError(f"sendPhoto failed: {rp.status_code} {rp.text}")
        sent_photo = rp.json()

    chunks = _split_text_for_telegram(full_text, 3900)
    if not chunks:
        raise RuntimeError("Telegram text is empty after split")

    sent_messages: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks):
        html_text = _to_telegram_html(chunk)
        rm = requests.post(
            base + "/sendMessage",
            data={
                "chat_id": chat_id,
                "text": html_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=60,
        )
        if rm.status_code >= 400:
            raise RuntimeError(f"sendMessage failed: {rm.status_code} {rm.text}")
        sent_messages.append(rm.json())

    first_msg = sent_messages[0] if sent_messages else None
    mode = "text_single" if len(sent_messages) == 1 else "text_multi"
    if sent_photo:
        mode = "photo_plus_text_single" if len(sent_messages) == 1 else "photo_plus_text_multi"

    return {
        "photo": sent_photo,
        "message": first_msg,
        "messages": sent_messages,
        "mode": mode,
        "sent_text": full_text,
    }


def telegram_message_url(chat_id: str, message_id: int | None) -> str | None:
    if not message_id:
        return None
    cid = (chat_id or "").strip()
    if cid.startswith("@"):
        return f"https://t.me/{cid[1:]}/{message_id}"
    return None
