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




def _country_hint_from_text(topic: str, title: str, category: str = "") -> str:
    text = f"{topic} {title} {category}".lower()
    countries = [
        "argentina", "australia", "austria", "brazil", "chile", "france", "georgia", "germany",
        "greece", "hungary", "italy", "mexico", "new zealand", "portugal", "romania",
        "south africa", "spain", "switzerland", "united states", "moldova", "usa"
    ]
    for c in countries:
        if c in text:
            return "United States" if c == "usa" else c.title()
    return ""


def _country_visual_hint(topic: str, title: str, category: str = "") -> str:
    country = _country_hint_from_text(topic, title, category)
    if not country:
        return ""
    return (
        f"Country identity: {country}. Include subtle, realistic regional cues relevant to {country} "
        "(landscape, vineyard architecture, terroir textures, table setting, climate mood), "
        "while staying photorealistic and documentary-style. "
    )

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
    # Root cause fix: if article already has a real image, reuse it as hero.
    # Do NOT require square ratio for existing local images.
    hero_hint = os.path.basename(hero_image_hint) if hero_image_hint else ""

    # Collect candidate hero filenames from hint + first local inline image.
    hero_candidates: list[str] = []
    if hero_hint:
        hero_candidates.append(hero_hint)

    for m in re.finditer(r"<img\b[^>]*?\bsrc=(?:\"([^\"]+)\"|'([^']+)')", content_html or "", flags=re.IGNORECASE):
        src = (m.group(1) or m.group(2) or "").strip()
        if not src:
            continue
        if src.startswith("http://") or src.startswith("https://"):
            continue
        if src.startswith("/"):
            src_name = os.path.basename(src)
        else:
            src_name = os.path.basename(src)
        if src_name and src_name not in hero_candidates:
            hero_candidates.append(src_name)

    hero_filename = ""
    hero_from_inline_src = ""
    # Also check live mirror directory where older generated images may exist.
    fallback_dirs = [blog_dir, "/var/www/yaswine/blog"]

    for cand in hero_candidates:
        src_abs = ""
        for d in fallback_dirs:
            test_abs = os.path.join(d, cand)
            if os.path.exists(test_abs):
                src_abs = test_abs
                break
        if not src_abs:
            continue

        # If hero came from inline candidates (not explicit hint), mark it to remove duplicate inline image later.
        if cand != hero_hint:
            hero_from_inline_src = cand

        if cand.lower().endswith(".webp"):
            hero_filename = os.path.basename(cand)
            # Copy from fallback location if needed
            target_abs = os.path.join(blog_dir, hero_filename)
            if src_abs != target_abs and not os.path.exists(target_abs):
                from shutil import copyfile
                copyfile(src_abs, target_abs)
                generated.append(GeneratedImage(filename=hero_filename, abs_path=target_abs))
        else:
            if src_abs.startswith(blog_dir.rstrip('/') + '/'):
                hero_filename = _convert_file_to_webp(src_abs)
                generated.append(GeneratedImage(filename=hero_filename, abs_path=os.path.join(blog_dir, hero_filename)))
            else:
                # Convert into target blog_dir
                from shutil import copyfile
                tmp_target = os.path.join(blog_dir, os.path.basename(cand))
                if not os.path.exists(tmp_target):
                    copyfile(src_abs, tmp_target)
                hero_filename = _convert_file_to_webp(tmp_target)
                generated.append(GeneratedImage(filename=hero_filename, abs_path=os.path.join(blog_dir, hero_filename)))
        break

    if not hero_filename:
        # Deterministic hero name.
        hero_basename = f"{_slugify(slug)}-hero"

        if not api_key:
            raise RuntimeError("Hero image generation blocked: no existing local image and missing Gemini API key")
        else:
            prompt = (
                "Create a photorealistic hero image for a blog article. "
                "Style: modern, cinematic lighting, natural texture, photographic grain, high-detail gradients, UGC-advertising vibe, no text, no logos, no watermarks, no illustration, no vector, no posterized look. Fill the entire frame edge-to-edge; no borders, no frames, no letterboxing, no pillarboxing, no padding, no margins, no blank strips. "
                "Aspect ratio: 1:1 (square). "
                f"Topic: {topic}. Title: {title}. Category: {category}. "
                + _country_visual_hint(topic, title, category)
                + "Avoid generic wine clipart look; prefer authentic place-specific visual storytelling."
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

    # If we reused an inline image as hero, remove only its first occurrence from article body
    # to avoid duplicate hero + identical first figure.
    if hero_from_inline_src:
        patt = re.compile(r"<figure[^>]*>\s*<img\b[^>]*?\bsrc=(?:\"([^\"]+)\"|'([^']+)')[^>]*>.*?</figure>", re.IGNORECASE | re.DOTALL)
        removed = False
        def _rm_first(m):
            nonlocal removed
            if removed:
                return m.group(0)
            src = (m.group(1) or m.group(2) or "").strip()
            if os.path.basename(src) == hero_from_inline_src:
                removed = True
                return ""
            return m.group(0)
        content_html = patt.sub(_rm_first, content_html or "")
        if not removed:
            patt2 = re.compile(r"<img\b[^>]*?\bsrc=(?:\"([^\"]+)\"|'([^']+)')[^>]*>", re.IGNORECASE)
            removed2 = False
            def _rm_img(m):
                nonlocal removed2
                if removed2:
                    return m.group(0)
                src = (m.group(1) or m.group(2) or "").strip()
                if os.path.basename(src) == hero_from_inline_src:
                    removed2 = True
                    return ""
                return m.group(0)
            content_html = patt2.sub(_rm_img, content_html or "")

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
            "Style: modern, cinematic lighting, natural texture, photographic grain, high-detail gradients, no text, no logos, no watermarks, no illustration, no vector, no posterized look. "
            "Aspect ratio: 1:1 (square). "
            f"Context topic: {topic}. "
            + _country_visual_hint(topic, title, category)
            + "Avoid generic wine clipart look; keep scene grounded in real place context. "
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
