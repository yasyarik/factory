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


def _force_russian(api_key: str, model: str, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return text

    prompt = (
        "Перепиши текст полностью на русском языке. "
        "Сохрани факты, цифры, структуру, CTA и хэштеги. "
        "Английские слова оставляй только для брендов и общепринятых аббревиатур (например My UGC Studio, AI, UGC, Shopify, TikTok). "
        "Верни только итоговый текст."
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt + "\n\nTEXT:\n" + text}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1500},
    }
    try:
        r = requests.post(url, json=payload, timeout=60)
        if r.status_code >= 400:
            return text
        data = r.json()
        out = data["candidates"][0]["content"]["parts"][0]["text"]
        out = out.replace("\r\n", "\n").replace("\r", "\n")
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
        return out or text
    except Exception:
        return text


def _generate_ru_post(api_key: str, model: str, *, title: str, description: str, body: str) -> str | None:
    prompt = (
        "Ты редактор Telegram-канала про маркетинг, UGC и AI. "
        "Напиши пост на РУССКОМ языке по источнику, сохраняя факты и практическую пользу. "
        "Стиль: живой, экспертный, без воды, без выдуманных фактов. "
        "Требования: "
        "1) Хук 1-2 строки. "
        "2) 8-12 буллетов с конкретикой и цифрами. "
        "3) Блок Практические шаги (3-5 пунктов). "
        "4) Вывод + CTA. "
        "5) 3-6 релевантных хэштегов. "
        "6) Минимум 1000 символов. "
        "7) Если по контексту нужен SaaS для UGC/автоматизации, используй бренд My UGC Studio. "
        "Верни только готовый текст поста."
    )
    user = f"TITLE: {title}\nDESCRIPTION: {description}\nSOURCE:\n{body}\n"

    gen_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt + "\n\n" + user}]}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 1600},
    }
    try:
        r = requests.post(gen_url, json=payload, timeout=60)
        if r.status_code >= 400:
            return None
        data = r.json()
        txt = data["candidates"][0]["content"]["parts"][0]["text"]
        txt = txt.replace("\r\n", "\n").replace("\r", "\n")
        txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
        if len(txt) < 900:
            return None
        return txt
    except Exception:
        return None


def build_telegram_post_ru(*, title: str, description: str, content_html: str, url: str, include_link: bool = True) -> str:
    body = _strip_html_to_text(content_html)
    body = _truncate(body, 7000)

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    draft = None
    if api_key:
        draft = _generate_ru_post(api_key, model, title=title, description=description, body=body)

    if not draft:
        draft = (
            f"{title}\n\n"
            "Почему это важно: рынок переполнен шаблонным контентом, и конверсию получает тот, кто дает полезную структуру и быстрые шаги внедрения.\n\n"
            "Что взять в работу сегодня:\n"
            "- Сформулируйте 1 сильный хук под аудиторию.\n"
            "- Подготовьте 3-5 конкретных тезисов с цифрами/фактами.\n"
            "- Добавьте блок практических шагов, чтобы пост был применимым.\n"
            "- Проверьте читаемость: короткие абзацы и буллеты.\n"
            "- Закройте пост понятным CTA.\n\n"
            "Суть статьи в одном блоке:\n"
            f"{_truncate(body, 2200)}\n\n"
            "Вывод: системный и полезный контент выигрывает у случайных публикаций и дает стабильный рост.\n\n"
            "#маркетинг #ugc #ai #контент #автоматизация"
        )

    # Hard-enforce RU pass to avoid mixed EN/RU output.
    if api_key:
        draft = _force_russian(api_key, model, draft)

    out = _truncate(draft, 3900)
    if include_link and url:
        out = _truncate(out, 3800) + "\n\n" + url
    return out


def telegram_send(*, bot_token: str, chat_id: str, text: str, photo_abs_path: str | None = None) -> dict[str, Any]:
    base = f"https://api.telegram.org/bot{bot_token}"

    # Telegram caption limit for media posts is 1024 chars.
    caption = _truncate((text or "").strip(), 1000)

    if photo_abs_path:
        try:
            with open(photo_abs_path, "rb") as f:
                files = {"photo": f}
                data = {
                    "chat_id": chat_id,
                    "caption": caption,
                }
                rp = requests.post(base + "/sendPhoto", data=data, files=files, timeout=90)
            if rp.status_code >= 400:
                raise RuntimeError(f"sendPhoto failed: {rp.status_code} {rp.text}")
            sent_photo = rp.json()
            return {"photo": sent_photo, "message": sent_photo, "mode": "photo_caption", "sent_text": caption}
        except Exception:
            pass

    msg_text = _truncate((text or "").strip(), 3900)
    rm = requests.post(
        base + "/sendMessage",
        data={"chat_id": chat_id, "text": msg_text, "disable_web_page_preview": False},
        timeout=60,
    )
    if rm.status_code >= 400:
        raise RuntimeError(f"sendMessage failed: {rm.status_code} {rm.text}")
    msg = rm.json()
    return {"photo": None, "message": msg, "mode": "text", "sent_text": msg_text}


def telegram_message_url(chat_id: str, message_id: int | None) -> str | None:
    if not message_id:
        return None
    cid = (chat_id or "").strip()
    if cid.startswith("@"):
        return f"https://t.me/{cid[1:]}/{message_id}"
    return None
