import base64
import json
import os
import re
import subprocess
import tempfile
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


def _image_dims(path: str) -> tuple[int, int] | None:
    try:
        p = subprocess.run(
            ["/usr/bin/identify", "-format", "%w %h", path],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if p.returncode != 0:
            return None
        out = (p.stdout or "").strip()
        w_s, h_s = out.split()
        w = int(w_s)
        h = int(h_s)
        if w <= 0 or h <= 0:
            return None
        return (w, h)
    except Exception:
        return None


def _is_square_path(path: str) -> bool:
    dims = _image_dims(path)
    if not dims:
        return False
    w, h = dims
    return abs(w - h) <= 2


def _is_square_bytes(img_bytes: bytes, mime: str) -> bool:
    ext = _ext_from_mime(mime)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(prefix="cf-img-", suffix=f".{ext}", delete=False) as f:
            f.write(img_bytes)
            tmp = f.name
        return _is_square_path(tmp)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def _to_webp_bytes(img_bytes: bytes) -> bytes:
    """Convert source image bytes to WebP via ImageMagick."""
    in_tmp = None
    out_tmp = None
    try:
        with tempfile.NamedTemporaryFile(prefix="cf-webp-in-", suffix=".img", delete=False) as f_in:
            f_in.write(img_bytes)
            in_tmp = f_in.name
        with tempfile.NamedTemporaryFile(prefix="cf-webp-out-", suffix=".webp", delete=False) as f_out:
            out_tmp = f_out.name

        p = subprocess.run(
            ["/usr/bin/convert", in_tmp, "-strip", "-quality", "86", out_tmp],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "convert failed").strip())

        with open(out_tmp, "rb") as f:
            return f.read()
    finally:
        for pth in (in_tmp, out_tmp):
            if pth and os.path.exists(pth):
                try:
                    os.remove(pth)
                except Exception:
                    pass


def _convert_file_to_webp(path: str) -> str:
    """Convert existing local image file to .webp and return new basename."""
    if not path or not os.path.exists(path):
        return os.path.basename(path or "")

    base_no_ext, ext = os.path.splitext(path)
    if ext.lower() == ".webp":
        return os.path.basename(path)

    webp_path = f"{base_no_ext}.webp"
    if not os.path.exists(webp_path):
        p = subprocess.run(
            ["/usr/bin/convert", path, "-strip", "-quality", "86", webp_path],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "convert failed").strip())

    return os.path.basename(webp_path)


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
    if hero_hint and os.path.exists(os.path.join(blog_dir, hero_hint)) and _is_square_path(os.path.join(blog_dir, hero_hint)):
        hero_abs = os.path.join(blog_dir, hero_hint)
        if hero_hint.lower().endswith(".webp"):
            hero_filename = hero_hint
        else:
            hero_filename = _convert_file_to_webp(hero_abs)
            generated.append(GeneratedImage(filename=hero_filename, abs_path=os.path.join(blog_dir, hero_filename)))
    else:
        # Deterministic hero name.
        hero_basename = f"{_slugify(slug)}-hero"

        # If no API key, fall back to existing assets without failing publish.
        if not api_key:
            hero_filename = hero_hint or "logo.png"
        else:
            prompt = (
                "Create a photorealistic hero image for a blog article. "
                "Style: modern, cinematic lighting, clean corporate, UGC-advertising vibe, no text, no logos, no watermarks. Fill the entire frame edge-to-edge; no borders, no frames, no letterboxing, no pillarboxing, no padding, no margins, no blank strips. "
                "Aspect ratio: 1:1 (square). "
                f"Topic: {topic}. Title: {title}. Category: {category}."
            )
            last_err = None
            for gen_attempt in range(1, 5):
                for mname in ([image_model] + FALLBACK_MODELS):
                    try:
                        img_bytes, mime = gemini_generate_image(api_key=api_key, model=mname, prompt=prompt + f" Return STRICTLY square output. Attempt {gen_attempt}/4.")
                        if not _is_square_bytes(img_bytes, mime):
                            raise RuntimeError("hero image is not square")
                        image_model = mname
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                if last_err is None:
                    break
            if last_err is not None:
                raise last_err

            hero_filename = f"{hero_basename}.webp"
            hero_path = os.path.join(blog_dir, hero_filename)
            if not os.path.exists(hero_path):
                img_bytes = _to_webp_bytes(img_bytes)
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
        src_name = os.path.basename(src)
        abs_existing = os.path.join(blog_dir, src_name)
        if os.path.exists(abs_existing):
            if src_name.lower().endswith(".webp"):
                continue
            new_name = _convert_file_to_webp(abs_existing)
            generated.append(GeneratedImage(filename=new_name, abs_path=os.path.join(blog_dir, new_name)))
            content_html = re.sub(
                r"(<img\b[^>]*?\bsrc=)(\"" + re.escape(src) + r"\"|'" + re.escape(src) + r"')",
                "\\1\"" + new_name + "\"",
                content_html,
                flags=re.IGNORECASE,
            )
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
            "Aspect ratio: 1:1 (square). "
            f"Context topic: {topic}. "
            + (f"Image description: {alt}." if alt else "")
        )

        last_err = None
        for gen_attempt in range(1, 5):
            for mname in ([image_model] + FALLBACK_MODELS):
                try:
                    img_bytes, mime = gemini_generate_image(api_key=api_key, model=mname, prompt=prompt + f" Return STRICTLY square output. Attempt {gen_attempt}/4.")
                    if not _is_square_bytes(img_bytes, mime):
                        raise RuntimeError("inline image is not square")
                    image_model = mname
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
            if last_err is None:
                break
        if last_err is not None:
            raise last_err

        new_name = f"{_slugify(slug)}-img-{idx}.webp"
        out_path = os.path.join(blog_dir, new_name)
        img_bytes = _to_webp_bytes(img_bytes)
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
