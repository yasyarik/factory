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
        "Ты редактор Telegram-канала про маркетинг, UGC и AI. "
        "Напиши пост на русском языке по материалу статьи. "
        "Сохрани суть, факты и практические шаги из источника. "
        "Не используй шаблонные пустые фразы. "
        "Структура: хук, почему это важно, ключевые тезисы (буллеты), практические шаги (буллеты), вывод+CTA, хэштеги. "
        "Длина 1200-3500 символов. "
        "Используй переносы строк и читабельные абзацы. "
        "Если по контексту нужен SaaS для UGC/автоматизации, упоминай My UGC Studio. "
        "Верни только готовый текст поста, без объяснений."
    )

    user = f"TITLE: {title}\nDESCRIPTION: {description}\nSOURCE:\n{body}\n"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    last_err = None
    for _ in range(3):
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt + "\n\n" + user}]}],
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": 1800},
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
    out = _truncate(draft, 3900)
    if include_link and url:
        out = _truncate(out, 3800) + "\n\n" + url
    return out


def telegram_send(*, bot_token: str, chat_id: str, text: str, photo_abs_path: str | None = None, hero_public_url: str | None = None) -> dict[str, Any]:
    base = f"https://api.telegram.org/bot{bot_token}"

    full_text = _truncate((text or "").strip(), 3900)
    html_text = _to_telegram_html(full_text)

    if photo_abs_path and len(full_text) <= 1000:
        try:
            with open(photo_abs_path, "rb") as f:
                files = {"photo": f}
                data = {
                    "chat_id": chat_id,
                    "caption": html_text,
                    "parse_mode": "HTML",
                }
                rp = requests.post(base + "/sendPhoto", data=data, files=files, timeout=90)
            if rp.status_code >= 400:
                raise RuntimeError(f"sendPhoto failed: {rp.status_code} {rp.text}")
            sent_photo = rp.json()
            return {"photo": sent_photo, "message": sent_photo, "mode": "photo_caption", "sent_text": full_text}
        except Exception:
            pass

    if hero_public_url:
        hidden = f'<a href="{html.escape(hero_public_url)}">&#8205;</a>\n'
        html_text = hidden + html_text

    rm = requests.post(
        base + "/sendMessage",
        data={"chat_id": chat_id, "text": html_text, "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=60,
    )
    if rm.status_code >= 400:
        raise RuntimeError(f"sendMessage failed: {rm.status_code} {rm.text}")
    msg = rm.json()
    return {"photo": None, "message": msg, "mode": "text", "sent_text": full_text}


def telegram_message_url(chat_id: str, message_id: int | None) -> str | None:
    if not message_id:
        return None
    cid = (chat_id or "").strip()
    if cid.startswith("@"):
        return f"https://t.me/{cid[1:]}/{message_id}"
    return None
