import html
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests


class TelegramPostValidationError(RuntimeError):
    def __init__(self, message: str, rejected_text: str = ""):
        super().__init__(message)
        self.rejected_text = (rejected_text or "").strip()


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


def _truncate_sentence(s: str, max_len: int) -> str:
    t = (s or "").strip()
    if len(t) <= max_len:
        return t
    cut = t[:max_len]
    # Prefer complete sentence boundary
    sent = max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '))
    if sent >= int(max_len * 0.55):
        return cut[: sent + 1].strip()
    sp = cut.rfind(' ')
    if sp >= int(max_len * 0.7):
        cut = cut[:sp]
    cut = cut.rstrip(' ,;:-')
    if cut and cut[-1] not in '.!?':
        cut += '.'
    return cut


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


def _ru_article_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return raw
    p = urlparse(raw)
    path = p.path or "/"
    path = re.sub(r"//+", "/", path)
    if not path.startswith("/"):
        path = "/" + path
    if path.startswith("/ru/") or path == "/ru":
        ru_path = "/ru/" if path == "/ru" else path
    elif re.match(r"^/(en|es|de|fr)(/|$)", path):
        ru_path = re.sub(r"^/(en|es|de|fr)(?=/|$)", "/ru", path, count=1)
    else:
        ru_path = "/ru" + path
    ru_path = re.sub(r"//+", "/", ru_path)
    return urlunparse((p.scheme, p.netloc, ru_path, p.params, p.query, p.fragment))


def _append_ru_site_footer(text: str, url: str) -> str:
    out = (text or "").strip()
    ru_url = _ru_article_url(url)
    if not ru_url:
        return out
    footer = f"С полной версией статьи можно ознакомиться на сайте: {ru_url}"
    if footer in out:
        return out
    return (out + "\n\n" + footer).strip()


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


def _first_sentence(text: str) -> str:
    t = _cleanup_post_text(text)
    if not t:
        return t
    m = re.search(r"(.+?[.!?])(?:\s|$)", t)
    if m:
        return m.group(1).strip()
    # fallback: comma/semicolon boundary if no sentence punctuation
    m2 = re.search(r"(.+?[,;:])(?:\s|$)", t)
    if m2 and len(m2.group(1)) >= 40:
        return m2.group(1).strip()
    return t


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


def _compact_ru_post_for_limit(text: str, max_len: int = 930) -> str:
    t = _cleanup_post_text(text)
    lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
    if not lines:
        return t

    title = lines[0][:120]
    why = next((ln for ln in lines if ln.lower().startswith("почему это важно:")), "Почему это важно: Практические выводы из статьи для быстрого применения.")

    bullets = re.findall(r"(?m)^\s*([1-9])\.\s*(.+)$", t)
    bullet_texts = [b[1].strip() for b in bullets][:5]
    while len(bullet_texts) < 3:
        bullet_texts.append("Примените рекомендацию на практике и проверьте результат на своих задачах.")

    concl = "Системный подход и проверка на практике дают лучший результат, чем разовые действия."
    m = re.search(r"(?is)###\s*Вывод\s*(.+?)(?:\n\s*Хэштеги\s*:|$)", t)
    if m:
        c = _cleanup_post_text(m.group(1))
        if c:
            concl = c

    cta = "Поделитесь в комментариях, какой подход сработал у вас лучше всего."
    m2 = re.search(r"(?im)^(?!Хэштеги:)(.+\?)$", t)
    if m2:
        cta = m2.group(1).strip()

    tags = [f"#{x}" for x in re.findall(r"#([\w\-]+)", t)]
    if len(tags) < 5:
        tags = ["#вино", "#винныйгид", "#сомелье", "#винныесоветы", "#дегустация"]
    hashtags = "Хэштеги: " + " ".join(list(dict.fromkeys(tags))[:8])

    bw, bb, bc, bt = 210, 160, 170, 120
    def build():
        body = [
            title,
            "",
            _truncate_sentence(_first_sentence(why), bw),
            "",
            f"1. {_truncate_sentence(_first_sentence(bullet_texts[0]), bb)}",
            f"2. {_truncate_sentence(_first_sentence(bullet_texts[1]), bb)}",
            f"3. {_truncate_sentence(_first_sentence(bullet_texts[2]), bb)}",
        ]
        if len(bullet_texts) > 3:
            body.append(f"4. {_truncate_sentence(_first_sentence(bullet_texts[3]), bb)}")
        body += [
            "",
            "### Вывод",
            _truncate_sentence(_first_sentence(concl), bc),
            "",
            _truncate_sentence(_first_sentence(cta), bt),
            "",
            hashtags,
        ]
        return "\n".join(body).strip()

    out = build()
    while len(out) > max_len and (bb > 95 or bc > 120 or bw > 150 or bt > 80):
        bb = max(95, bb - 10)
        bc = max(120, bc - 10)
        bw = max(150, bw - 10)
        bt = max(80, bt - 10)
        out = build()

    return _cleanup_post_text(out)



def _validate_ru_post(text: str, *, include_link: bool, url: str, require_footer: bool = True) -> list[str]:
    errs: list[str] = []
    t = _cleanup_post_text(text)

    if not t:
        return ["empty result"]

    if len(t) < 1400:
        errs.append(f"too short ({len(t)} chars), expected >= 1400")
    if len(t) > 3200:
        errs.append(f"too long ({len(t)} chars), expected <= 3200")

    if "—" in t or "–" in t:
        errs.append("contains long dash")

    if re.search(r"[A-Za-zА-Яа-я0-9]{90,}", t):
        errs.append("contains merged text without spaces")

    ratio = _cyrillic_ratio(t)
    if ratio < 0.55:
        errs.append(f"not russian enough (cyr ratio={ratio:.2f}, need >=0.55)")

    sentence_count = len(re.findall(r"[.!?](?:\s|$)", t))
    if sentence_count < 8:
        errs.append(f"not enough complete sentences ({sentence_count}), need >= 8")

    if require_footer:
        ru_url = _ru_article_url(url)
        footer_expected = f"С полной версией статьи можно ознакомиться на сайте: {ru_url}" if ru_url else ""
        if footer_expected and footer_expected not in t:
            errs.append("missing required RU site link footer")

    return errs


def _generate_ru_post(api_key: str, model: str, *, title: str, description: str, body: str, include_link: bool, url: str) -> str:
    base_prompt = (
        "Ты пишешь Telegram-пост по статье. Верни ТОЛЬКО готовый текст поста на русском, без пояснений. "
        "Это нейтральный образовательный материал о продукте и контенте, без призывов к употреблению алкоголя. "
        "Названия и бренды не переводи. Не используй длинное тире, только обычный дефис.\n\n"
        "ФОРМАТ И СТРУКТУРА:\n\n"
        "Мини-статья максимум на 5-6 небольших абзацев.\n\n"
        "Связный цельный текст (завязка -> ключевая суть -> вывод).\n\n"
        "Без шаблонных списков, без нумерации, без повторяющихся фраз.\n\n"
        "В конце добавь 5-8 релевантных хэштегов в одну строку.\n\n"
        "ЖЕСТКИЕ ОГРАНИЧЕНИЯ (КРИТИЧЕСКИ ВАЖНО):\n\n"
        "Итоговый объем всего текста: не более 500 слов.\n\n"
        "Выдели только самые главные мысли из SOURCE. Безжалостно удаляй всю воду, долгие вступления, "
        "лишние примеры и второстепенные детали.\n\n"
        "Текст должен быть очень сжатым, но мысль не должна обрываться."
    )

    user = f"TITLE: {title}\nDESCRIPTION: {description}\nSOURCE:\n{body}\n"
    url_api = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    feedback = ""
    last_err = None
    last_text = ""

    for attempt in range(1, 5):
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": base_prompt + "\n\n" + feedback + "\n" + user}]}],
            "generationConfig": {"temperature": 0.35, "thinkingConfig": {"thinkingBudget": 0}},
        }
        try:
            r = requests.post(url_api, json=payload, timeout=55)
            if r.status_code >= 400:
                last_err = RuntimeError(f"Gemini telegram generate failed: {r.status_code} {r.text}")
                continue
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            text = _cleanup_post_text(text)
            last_text = text
            errors = _validate_ru_post(text, include_link=include_link, url=url, require_footer=False)
            if not errors:
                return text

            feedback = (
                "Исправь полностью. Ошибки в прошлом варианте:\n- "
                + "\n- ".join(errors)
                + "\nСделай новый вариант как цельную мини-статью без повторов."
            )
            last_err = RuntimeError("; ".join(errors))
        except Exception as e:
            last_err = e
            continue

    if last_err:
        extra = ""
        if last_text.strip():
            extra = " | last_text=" + " ".join(last_text.split())[:420]
        raise RuntimeError(str(last_err) + extra)
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
    out = _append_ru_site_footer(out, url)
    if len(out) > 3600:
        # Keep message within Telegram sendMessage hard limit with a safety margin.
        out = _truncate_sentence(out, 3600)
        out = _append_ru_site_footer(out, url)

    final_errors = _validate_ru_post(out, include_link=include_link, url=url, require_footer=True)
    if final_errors:
        raise TelegramPostValidationError('Telegram post validation failed: ' + '; '.join(final_errors), rejected_text=out)

    return out


def telegram_send(*, bot_token: str, chat_id: str, text: str, photo_abs_path: str | None = None, hero_public_url: str | None = None) -> dict[str, Any]:
    base = f"https://api.telegram.org/bot{bot_token}"

    full_text = (text or "").strip()
    if not full_text:
        raise RuntimeError("Telegram text is empty")

    # One-message text mode (no photo): keeps full article teaser and allows URL preview.
    caption_raw = _truncate(full_text, 3900)
    caption_html = _to_telegram_html(caption_raw)
    rm = requests.post(
        base + "/sendMessage",
        data={
            "chat_id": chat_id,
            "text": caption_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=60,
    )
    if rm.status_code >= 400:
        raise RuntimeError(f"sendMessage failed: {rm.status_code} {rm.text}")
    msg = rm.json()
    return {
        "photo": None,
        "message": msg,
        "messages": [msg],
        "mode": "text_single",
        "sent_text": caption_raw,
    }

def telegram_message_url(chat_id: str, message_id: int | None) -> str | None:
    if not message_id:
        return None
    cid = (chat_id or "").strip()
    if cid.startswith("@"):
        return f"https://t.me/{cid[1:]}/{message_id}"
    return None
