import base64
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Any




FALLBACK_MODELS = [
    'gemini-2.5-flash-image',
    'gemini-2.0-flash-preview-image-generation',
    'gemini-2.0-flash-exp-image-generation',
]

@dataclass
class GeneratedImage:
    filename: str
    abs_path: str


def _ext_from_mime(mime: str) -> str:
    mime = (mime or "").lower().strip()
    if mime == "image/png":
        return "png"
    if mime == "image/jpeg" or mime == "image/jpg":
        return "jpg"
    if mime == "image/webp":
        return "webp"
    # Default: most Gemini image responses are PNG.
    return "png"


def _slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s[:120].strip("-") or "post"


def _model_id(model: str) -> str:
    m = (model or "").strip()
    if m.startswith("models/"):
        return m[len("models/"):]
    return m


def gemini_generate_image(*, api_key: str, model: str, prompt: str, timeout_s: int = 180) -> tuple[bytes, str]:
    # Gemini API: models/{model}:generateContent
    mid = _model_id(model)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{mid}:generateContent?key={api_key}"

    payload: dict[str, Any] = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]},
        ],
        # Request image output. Some models require TEXT+IMAGE.
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    try:
        cand = data["candidates"][0]
        parts = cand["content"]["parts"]
    except Exception:
        raise RuntimeError(f"Unexpected Gemini image response: {data}")

    for part in parts:
        if not isinstance(part, dict):
            continue

        inline = None
        if isinstance(part.get("inlineData"), dict):
            inline = part.get("inlineData")
        elif isinstance(part.get("inline_data"), dict):
            inline = part.get("inline_data")

        if not inline:
            continue

        mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
        b64 = inline.get("data")
        if not b64:
            continue
        return base64.b64decode(b64), mime

    raise RuntimeError(f"No inline image data in response: {data}")


def ensure_hero_and_inline_images(
    *,
    api_key: str | None,
    image_model: str,
    blog_dir: str,
    slug: str,
    topic: str,
    title: str,
    category: str,
    hero_image_hint: str | None,
    content_html: str,
) -> tuple[str, str, list[GeneratedImage]]:
    """Ensures hero image exists and generates local <img src="..."> assets.

    Returns: (hero_filename, rewritten_content_html, generated_files)
    """

    generated: list[GeneratedImage] = []

    # Repair legacy drafts that accidentally stored escaped quotes in HTML (e.g. src=\"file\").
    content_html = (content_html or "").replace("\\\"", "\"")

    os.makedirs(blog_dir, exist_ok=True)

    # 1) Hero
    hero_hint = os.path.basename(hero_image_hint) if hero_image_hint else ""
    if hero_hint and os.path.exists(os.path.join(blog_dir, hero_hint)):
        hero_filename = hero_hint
    else:
        # Deterministic hero name.
        hero_basename = f"{_slugify(slug)}-hero"

        # If no API key, fall back to existing assets (logo.png) without failing publish.
        if not api_key:
            hero_filename = hero_hint or "logo.png"
        else:
            prompt = (
                "Create a photorealistic hero image for a blog article. "
                "Style: modern, cinematic lighting, clean corporate, UGC-advertising vibe, no text, no logos, no watermarks. "
                "Aspect ratio: 16:9. "
                f"Topic: {topic}. Title: {title}. Category: {category}."
            )
            last_err = None
            for mname in ([image_model] + FALLBACK_MODELS):
                try:
                    img_bytes, mime = gemini_generate_image(api_key=api_key, model=mname, prompt=prompt)
                    image_model = mname
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
            if last_err is not None:
                raise last_err
            ext = _ext_from_mime(mime)
            hero_filename = f"{hero_basename}.{ext}"
            hero_path = os.path.join(blog_dir, hero_filename)
            if not os.path.exists(hero_path):
                with open(hero_path, "wb") as f:
                    f.write(img_bytes)
                generated.append(GeneratedImage(filename=hero_filename, abs_path=hero_path))

    # 2) Inline images referenced in HTML
    # We only generate for relative filenames (no http(s), no /, no ../)
    img_srcs: list[str] = []
    for m in re.finditer(r"<img\b[^>]*?\bsrc=(?:\"([^\"]+)\"|'([^']+)')", content_html or "", flags=re.IGNORECASE):
        src = (m.group(1) or m.group(2) or "").strip()
        if not src:
            continue
        if src.startswith("http://") or src.startswith("https://"):
            continue
        # Allow /images/... placeholders too (model sometimes outputs absolute paths).
        if src.startswith("/images/") or src.startswith("/blog/"):
            img_srcs.append(src)
            continue
        if src.startswith("/") or src.startswith("../"):
            continue
        img_srcs.append(src)

    # Find alt text near each src (best-effort)
    for idx, src in enumerate(img_srcs, start=1):
        abs_existing = os.path.join(blog_dir, os.path.basename(src))
        if os.path.exists(abs_existing):
            continue

        if not api_key:
            # Can't generate without key; skip.
            continue

        # Extract the <img ...> tag for alt
        alt = ""
        mtag = re.search(r"<img\b[^>]*?\bsrc=(?:\"" + re.escape(src) + r"\"|'" + re.escape(src) + r"')[^>]*?>", content_html or "", flags=re.IGNORECASE)
        if mtag:
            malt = re.search(r"\balt=(?:\"([^\"]*)\"|'([^']*)')", mtag.group(0), flags=re.IGNORECASE)
            alt = (malt.group(1) or malt.group(2) or "").strip() if malt else ""

        prompt = (
            "Create a photorealistic supporting image for a blog article section. "
            "Style: modern, cinematic lighting, clean corporate, no text, no logos, no watermarks. "
            "Aspect ratio: 16:9. "
            f"Context topic: {topic}. "
            + (f"Image description: {alt}." if alt else "")
        )

        last_err = None
        for mname in ([image_model] + FALLBACK_MODELS):
            try:
                img_bytes, mime = gemini_generate_image(api_key=api_key, model=mname, prompt=prompt)
                image_model = mname
                last_err = None
                break
            except Exception as e:
                last_err = e
        if last_err is not None:
            raise last_err
        ext = _ext_from_mime(mime)
        new_name = f"{_slugify(slug)}-img-{idx}.{ext}"
        out_path = os.path.join(blog_dir, new_name)
        with open(out_path, "wb") as f:
            f.write(img_bytes)
        generated.append(GeneratedImage(filename=new_name, abs_path=out_path))

        # Replace src in HTML to point to generated file.
        content_html = re.sub(
            r"(<img\b[^>]*?\bsrc=)(\"" + re.escape(src) + r"\"|'" + re.escape(src) + r"')",
            "\\1\"" + new_name + "\"",
            content_html,
            flags=re.IGNORECASE,
        )

    return hero_filename, content_html, generated
