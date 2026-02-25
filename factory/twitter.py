import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qsl, quote, urljoin, urlsplit

import requests


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


def _extract_image_urls(content_html: str, page_url: str, max_images: int = 4) -> list[str]:
    html = content_html or ""
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.IGNORECASE):
        src = (m.group(1) or "").strip()
        if not src:
            continue
        abs_u = urljoin(page_url or "https://myugc.studio/", src)
        if not abs_u.startswith("http"):
            continue
        if abs_u in seen:
            continue
        seen.add(abs_u)
        out.append(abs_u)
        if len(out) >= max_images:
            break
    return out


def extract_article_image_urls_for_x(*, content_html: str, page_url: str, max_images: int = 4) -> list[str]:
    return _extract_image_urls(content_html or "", page_url or "https://myugc.studio/", max_images=max_images)


def build_twitter_thread_ru(*, title: str, description: str, content_html: str, url: str, include_link: bool = False, max_posts: int = 1) -> list[str]:
    body = _strip_html_to_text(content_html)
    body = _truncate(body, 7000)

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    if api_key:
        prompt = (
            "Сделай публикацию для X на русском по статье. "
            "Формат: РОВНО 1 пост. "
            "Сделай короткое саммари статьи до 240 символов, четкая польза и вывод. "
            "Без выдуманных брендов. Если уместно упомяни My UGC Studio. "
            "Верни JSON: {\"tweets\":[\"...\"]}."
        )
        user = f"TITLE: {title}\nDESCRIPTION: {description}\nSOURCE:\n{body}\nURL:{url}"
        gen_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt + "\n\n" + user}]}],
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 900, "responseMimeType": "application/json"},
        }
        try:
            r = requests.post(gen_url, json=payload, timeout=60)
            if r.status_code < 400:
                data = r.json()
                txt = data["candidates"][0]["content"]["parts"][0]["text"]
                import json
                obj = json.loads(txt)
                tw = obj.get("tweets") if isinstance(obj, dict) else None
                if isinstance(tw, list) and tw:
                    first = next((x for x in tw if isinstance(x, str) and x.strip()), "").strip()
                    if first:
                        first = _truncate(first.replace("\r", ""), 260)
                        if include_link and url:
                            first = _truncate(first, 230) + "\n" + url
                        return [first]
        except Exception:
            pass

        raise RuntimeError("X thread generation unavailable (fallback disabled)")

    # conservative fallback if AI key absent
    first = _truncate((title or "").strip(), 220)
    if description:
        first = _truncate(f"{first}: {description}", 240)
    return [f"{first}\n{url}"] if (include_link and url) else [first]


def _pct(s: str) -> str:
    return quote(str(s), safe="~-._")


def _oauth1_auth_header(*, method: str, url: str, consumer_key: str, consumer_secret: str, token: str, token_secret: str) -> str:
    now = str(int(time.time()))
    nonce = secrets.token_hex(12)

    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": now,
        "oauth_token": token,
        "oauth_version": "1.0",
    }

    split = urlsplit(url)
    base_url = f"{split.scheme}://{split.netloc}{split.path}"

    sig_params: list[tuple[str, str]] = []
    for k, v in parse_qsl(split.query, keep_blank_values=True):
        sig_params.append((k, v))
    for k, v in oauth_params.items():
        sig_params.append((k, v))

    sig_params.sort(key=lambda x: (_pct(x[0]), _pct(x[1])))
    normalized = "&".join([f"{_pct(k)}={_pct(v)}" for k, v in sig_params])

    base_string = "&".join([_pct(method.upper()), _pct(base_url), _pct(normalized)])
    signing_key = f"{_pct(consumer_secret)}&{_pct(token_secret)}"
    digest = hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    signature = base64.b64encode(digest).decode("ascii")

    oauth_params["oauth_signature"] = signature
    header = "OAuth " + ", ".join([f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth_params.items())])
    return header


def _upload_media_oauth1(*, media_url: str, oauth1: dict[str, str]) -> str | None:
    try:
        dl = requests.get(media_url, timeout=25)
        if dl.status_code >= 400 or not dl.content:
            return None
    except Exception:
        return None

    endpoint = "https://upload.twitter.com/1.1/media/upload.json"
    auth_header = _oauth1_auth_header(
        method="POST",
        url=endpoint,
        consumer_key=(oauth1.get("api_key") or "").strip(),
        consumer_secret=(oauth1.get("api_secret") or "").strip(),
        token=(oauth1.get("access_token") or "").strip(),
        token_secret=(oauth1.get("access_token_secret") or "").strip(),
    )
    try:
        r = requests.post(
            endpoint,
            headers={"Authorization": auth_header},
            files={"media": ("image.jpg", dl.content)},
            timeout=40,
        )
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            return None
        mid = (data.get("media_id_string") if isinstance(data, dict) else None) or ""
        return mid.strip() or None
    except Exception:
        return None


def twitter_post_thread(*, access_token: str | None = None, tweets: list[str], oauth1: dict[str, str] | None = None, media_urls: list[str] | None = None) -> dict[str, Any]:
    """Post to X/Twitter v2 using OAuth2 bearer or OAuth1. If OAuth1 is set, can attach up to 4 images to first tweet."""
    token = (access_token or "").strip()
    oauth1 = oauth1 or {}
    use_oauth1 = bool(
        (oauth1.get("api_key") or "").strip()
        and (oauth1.get("api_secret") or "").strip()
        and (oauth1.get("access_token") or "").strip()
        and (oauth1.get("access_token_secret") or "").strip()
    )
    if not token and not use_oauth1:
        raise RuntimeError("Missing X credentials")

    posts = [t.strip() for t in (tweets or []) if isinstance(t, str) and t.strip()]
    if not posts:
        raise RuntimeError("No tweets to publish")

    media_ids: list[str] = []
    if use_oauth1 and media_urls:
        for u in media_urls[:4]:
            mid = _upload_media_oauth1(media_url=str(u), oauth1=oauth1)
            if mid:
                media_ids.append(mid)

    endpoint = "https://api.twitter.com/2/tweets"
    headers = {"Content-Type": "application/json"}

    first_id = None
    prev_id = None
    for i, text in enumerate(posts):
        payload: dict[str, Any] = {"text": _truncate(text, 280)}
        if prev_id:
            payload["reply"] = {"in_reply_to_tweet_id": prev_id}
        if i == 0 and media_ids:
            payload["media"] = {"media_ids": media_ids}

        req_headers = dict(headers)
        if use_oauth1:
            req_headers["Authorization"] = _oauth1_auth_header(
                method="POST",
                url=endpoint,
                consumer_key=(oauth1.get("api_key") or "").strip(),
                consumer_secret=(oauth1.get("api_secret") or "").strip(),
                token=(oauth1.get("access_token") or "").strip(),
                token_secret=(oauth1.get("access_token_secret") or "").strip(),
            )
        else:
            req_headers["Authorization"] = f"Bearer {token}"

        r = requests.post(endpoint, headers=req_headers, json=payload, timeout=30)
        data = r.json() if r.content else {}
        if r.status_code >= 400 or not isinstance(data, dict) or not data.get("data", {}).get("id"):
            msg = ""
            if isinstance(data, dict):
                msg = data.get("detail") or data.get("title") or str(data)
            raise RuntimeError(f"X post failed ({r.status_code}): {msg or r.text}")

        tid = data["data"]["id"]
        if not first_id:
            first_id = tid
        prev_id = tid

    url = f"https://x.com/i/web/status/{first_id}" if first_id else None
    return {"ok": True, "id": first_id, "thread_url": url, "count": len(posts), "media_count": len(media_ids)}
