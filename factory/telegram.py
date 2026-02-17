import html
import os
import re
from datetime import datetime, timezone
from typing import Any

import requests


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


def _generate_ru_post(api_key: str, model: str, *, title: str, description: str, body: str) -> str:
    prompt = (
        "Ты опытный редактор русскоязычного Telegram-канала про маркетинг, UGC и AI. "
        "Сделай качественный пост по статье: живой, логичный, с естественными переходами между блоками. "
        "Это НЕ дословный перевод, а осмысленная адаптация под Telegram. "
        "Сохрани все ключевые факты и практическую пользу из источника. "
        "Обязательная структура: сильный хук, зачем это важно, конкретные тезисы в буллетах, пошаговые действия, вывод и CTA, затем хэштеги. "
        "Используй нормальные абзацы и читаемый формат. "
        "Название бренда My UGC Studio никогда не переводи и не искажай. "
        "Не добавляй объяснений вне поста. Верни только готовый текст поста."
    )

    user = f"TITLE: {title}\nDESCRIPTION: {description}\nSOURCE:\n{body}\n"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    last_err = None
    for _ in range(3):
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt + "\n\n" + user}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2600},
        }
        try:
            r = requests.post(url, json=payload, timeout=60)
            if r.status_code >= 400:
                last_err = RuntimeError(f"Gemini telegram generate failed: {r.status_code} {r.text}")
                continue
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if not text:
                last_err = RuntimeError("Gemini returned empty telegram post")
                continue
            return text
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(str(last_err) if last_err else "Gemini telegram generation failed")


def build_telegram_post_ru(*, title: str, description: str, content_html: str, url: str, include_link: bool = True) -> str:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY/GOOGLE_API_KEY for Telegram generation")

    body = _strip_html_to_text(content_html)
    body = _truncate(body, 7000)

    draft = _generate_ru_post(api_key, model, title=title, description=description, body=body)
    out = (draft or "").strip()
    if include_link and url and (url not in out):
        out = out + "\n\n" + url
    return out


def telegram_send(*, bot_token: str, chat_id: str, text: str, photo_abs_path: str | None = None, hero_public_url: str | None = None) -> dict[str, Any]:
    base = f"https://api.telegram.org/bot{bot_token}"

    full_text = (text or "").strip()
    if not full_text:
        raise RuntimeError("Telegram text is empty")

    chunks = _split_text_for_telegram(full_text, 3900)
    if not chunks:
        raise RuntimeError("Telegram text is empty after split")

    # Always send full post text via sendMessage (caption mode truncates/looks cut).
    # Photo preview is attached via hidden link when hero_public_url is available.
    sent_messages: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks):
        html_text = _to_telegram_html(chunk)
        if idx == 0 and hero_public_url:
            hidden = f'<a href="{html.escape(hero_public_url)}">&#8205;</a>\n'
            html_text = hidden + html_text
        rm = requests.post(
            base + "/sendMessage",
            data={
                "chat_id": chat_id,
                "text": html_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": (False if idx == 0 else True),
            },
            timeout=60,
        )
        if rm.status_code >= 400:
            raise RuntimeError(f"sendMessage failed: {rm.status_code} {rm.text}")
        sent_messages.append(rm.json())

    first_msg = sent_messages[0] if sent_messages else None
    return {
        "photo": None,
        "message": first_msg,
        "messages": sent_messages,
        "mode": ("text_single" if len(sent_messages) == 1 else "text_multi"),
        "sent_text": full_text,
    }


def telegram_message_url(chat_id: str, message_id: int | None) -> str | None:
    if not message_id:
        return None
    cid = (chat_id or "").strip()
    if cid.startswith("@"):
        return f"https://t.me/{cid[1:]}/{message_id}"
    return None
