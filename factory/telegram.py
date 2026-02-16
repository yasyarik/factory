import os
import re
from datetime import datetime, timezone
from typing import Any

import requests


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _strip_html_to_text(html: str) -> str:
    s = (html or "")
    s = re.sub(r"(?is)<script.*?</script>", " ", s)
    s = re.sub(r"(?is)<style.*?</style>", " ", s)
    s = re.sub(r"(?is)<br\\s*/?>", "\n", s)
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


def build_telegram_post_ru(*, title: str, description: str, content_html: str, url: str, include_link: bool = True) -> str:
    body = _strip_html_to_text(content_html)
    body = _truncate(body, 7000)

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    if api_key:
        prompt = (
            "Ты редактор Telegram-канала про маркетинг, UGC и AI. "
            "Перепиши материал на РУССКОМ в формате поста Telegram, сохраняя факты и практическую пользу. "
            "Стиль: живой, экспертный, без воды. Без выдуманных фактов. "
            "Требования: "
            "1) Короткий хук 1-2 строки. "
            "2) 4-8 буллетов с ключевыми идеями. "
            "3) Короткий вывод + CTA. "
            "4) 2-5 релевантных хэштегов. "
            "5) Четкие абзацы и читаемость. "
            "6) Если контекст статьи требует упоминания SaaS-инструмента для UGC/автоматизации, используй бренд My UGC Studio (не выдуманные названия). "
            "Верни только готовый текст поста."
        )

        user = (
            f"TITLE: {title}\n"
            f"DESCRIPTION: {description}\n"
            f"SOURCE:\n{body}\n"
        )

        gen_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt + "\n\n" + user}]}],
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": 1200},
        }
        try:
            r = requests.post(gen_url, json=payload, timeout=60)
            if r.status_code < 400:
                data = r.json()
                txt = data["candidates"][0]["content"]["parts"][0]["text"]
                txt = txt.replace("\r\n", "\n").replace("\r", "\n")
                txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
                if txt:
                    out = _truncate(txt, 3900)
                    if include_link and url:
                        out = _truncate(out, 3800) + "\n\n" + url
                    return out
        except Exception:
            pass

    # fallback
    post = (
        f"{title}\n\n"
        f"{description}\n\n"
        "Коротко по делу:\n"
        f"{_truncate(body, 2600)}\n\n"
        "#маркетинг #ugc #ai #контент"
    )
    post = _truncate(post, 3900)
    if include_link and url:
        post = _truncate(post, 3800) + "\n\n" + url
    return post


def telegram_send(*, bot_token: str, chat_id: str, text: str, photo_abs_path: str | None = None) -> dict[str, Any]:
    base = f"https://api.telegram.org/bot{bot_token}"
    sent_photo = None

    if photo_abs_path:
        try:
            with open(photo_abs_path, "rb") as f:
                files = {"photo": f}
                data = {"chat_id": chat_id}
                rp = requests.post(base + "/sendPhoto", data=data, files=files, timeout=90)
            if rp.status_code >= 400:
                raise RuntimeError(f"sendPhoto failed: {rp.status_code} {rp.text}")
            sent_photo = rp.json()
        except Exception:
            sent_photo = None

    rm = requests.post(base + "/sendMessage", data={"chat_id": chat_id, "text": text, "disable_web_page_preview": False}, timeout=60)
    if rm.status_code >= 400:
        raise RuntimeError(f"sendMessage failed: {rm.status_code} {rm.text}")
    msg = rm.json()

    return {"photo": sent_photo, "message": msg}


def telegram_message_url(chat_id: str, message_id: int | None) -> str | None:
    if not message_id:
        return None
    cid = (chat_id or "").strip()
    if cid.startswith("@"):
        return f"https://t.me/{cid[1:]}/{message_id}"
    return None
