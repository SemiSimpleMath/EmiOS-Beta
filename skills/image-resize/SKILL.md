---
name: image-resize
description: How to resize, thumbnail, crop, or convert image format using execute_code + Pillow. Covers the four common operations (thumbnail / fit-to-box / exact-resize / aspect-crop), the EXIF-orientation gotcha that ships rotated photos, transparency handling on format conversion, and HEIC input from phones. Common litmus phrases — "resize this to 512px", "make a thumbnail", "crop to a square", "convert to JPEG".
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "resize image"
      - "resize this"
      - "thumbnail"
      - "crop image"
      - "crop to"
      - "scale image"
      - "downscale"
      - "shrink image"
      - "convert image"
      - "convert to jpeg"
      - "convert to png"
      - "convert to webp"
      - "image format"
      - "make smaller"
      - "image too big"
---

# Resize, thumbnail, crop, convert images

Image transforms run inside the sandbox via `execute_code` with Pillow (`PIL`). The image arrives as a **pod** in `input_pod_ids`, gets staged at `/workspace/inputs/`, and the transformed output is written to `/workspace/outputs/` where it auto-mints as a new pod.

If the parent skill `sandboxed-python-execution` isn't loaded, read its sections on pod-aware I/O first. This skill assumes that context.

## When this skill applies

- "Resize this to 512px" / "make it smaller" / "shrink this"
- "Thumbnail this" / "make a thumbnail" — preserve aspect, fit in box, **never upscale**
- "Crop to square" / "crop to 16:9" — pick aspect, center-crop
- "Convert PNG to JPEG" / "give me a WebP" — format change
- Watermark, basic filters, EXIF strip — same call shape, different operation

**Not this skill**:
- "Recognize objects in this image" → that's an LLM vision call, not Pillow
- "Resize 500 images" → fine, one execute_code call with a glob loop
- "Extract text from a screenshot (OCR)" → `pytesseract`, separate concern

## The four common operations

Pillow has overlapping APIs — choose by what the user actually wants:

| User intent | Pillow call | Behavior |
|---|---|---|
| "Fit in 512×512 box, keep aspect" | `img.thumbnail((512, 512))` | In-place. **Never upscales.** Result ≤ box. |
| Same, but as new object | `ImageOps.contain(img, (512, 512))` | Returns new. Same behavior. |
| "Exactly 512×512, crop excess" | `ImageOps.fit(img, (512, 512))` | Center-crop + resize. Exact output size. |
| "Exactly 512×512, may distort" | `img.resize((512, 512))` | Force size. Stretches. Rarely what's wanted. |

When the user says "resize to 512px" without context, **default to `thumbnail((512, 512))`** — preserves aspect, won't blow up tiny inputs. Only use `resize` if they specifically need exact pixel dimensions.

## Always apply EXIF orientation FIRST

Phone photos store orientation as EXIF metadata (rotated 90° / 180° / mirrored). If you skip this step, the saved thumbnail will be sideways. Pillow does **not** apply it automatically.

```python
from PIL import Image, ImageOps
img = Image.open(path)
img = ImageOps.exif_transpose(img)   # ← do this BEFORE resize/save
```

This is the #1 reason "resize" results look wrong. Make it a reflex.

## Transparency / format conversion

PNG and WebP support alpha; JPEG doesn't. If you convert PNG→JPEG, you need to flatten:

```python
if img.mode in ("RGBA", "LA", "P"):
    bg = Image.new("RGB", img.size, (255, 255, 255))
    bg.paste(img, mask=img.convert("RGBA").split()[-1])
    img = bg
img.save("outputs/out.jpg", "JPEG", quality=88, optimize=True)
```

For PNG output, just preserve mode and save:
```python
img.save("outputs/out.png", "PNG", optimize=True)
```

For WebP (smaller files, modern):
```python
img.save("outputs/out.webp", "WEBP", quality=85, method=6)  # method=6 is slowest, best
```

## HEIC files (iPhone default)

Phones default to HEIC. Pillow doesn't read HEIC out of the box — add `pillow-heif` to `requirements` and register the opener:

```python
execute_code(
    source="""
        from pillow_heif import register_heif_opener
        register_heif_opener()
        from PIL import Image, ImageOps
        img = Image.open('inputs/image_abc')   # works on .heic now
        img = ImageOps.exif_transpose(img)
        img.thumbnail((1024, 1024))
        img.convert("RGB").save('outputs/thumb.jpg', 'JPEG', quality=88)
        print(f'wrote {img.size}')
    """,
    input_pod_ids=["datapod:image:<id>"],
    requirements=["pillow-heif"],
)
```

The `requirements` install adds ~20–30s to the first call. Cache locally / build into the image if it becomes routine.

## Picking output quality

| Use | Format | Quality / settings |
|---|---|---|
| Email attachment, web preview | JPEG | `quality=85` (sweet spot) |
| Photo for storage, no transparency | JPEG | `quality=92, optimize=True, progressive=True` |
| Diagram / screenshot / logo | PNG | `optimize=True` |
| Web-modern, smaller | WebP | `quality=85, method=6` |
| Animated | original format (GIF/WebP) | Pillow loses frames on naive open; use `save_all=True, append_images=...` |

Default to JPEG q=85 for photos when the user doesn't specify. It's the right answer 80% of the time.

## Worked examples

### "Resize this to 512px, keep aspect"

```python
execute_code(
    source="""
        import os
        from PIL import Image, ImageOps
        src = next(p for p in os.listdir('inputs') if p.startswith('image'))
        img = Image.open(f'inputs/{src}')
        img = ImageOps.exif_transpose(img)
        img.thumbnail((512, 512))   # in-place, preserves aspect, no upscale
        out = 'outputs/resized.jpg'
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img.save(out, 'JPEG', quality=88, optimize=True)
        print(f'{img.size[0]}x{img.size[1]} -> {out}')
    """,
    input_pod_ids=["<image pod id>"],
)
```

### "Make a 256×256 square thumbnail"

Center-crop, then resize:

```python
execute_code(
    source="""
        import os
        from PIL import Image, ImageOps
        src = next(p for p in os.listdir('inputs') if p.startswith('image'))
        img = Image.open(f'inputs/{src}')
        img = ImageOps.exif_transpose(img)
        sq = ImageOps.fit(img, (256, 256), method=Image.LANCZOS)
        if sq.mode != 'RGB':
            sq = sq.convert('RGB')
        sq.save('outputs/thumb_256.jpg', 'JPEG', quality=88, optimize=True)
        print('256x256 written')
    """,
    input_pod_ids=["<image pod id>"],
)
```

### "Convert this PNG to JPEG"

Flatten transparency to white:

```python
execute_code(
    source="""
        import os
        from PIL import Image, ImageOps
        src = next(p for p in os.listdir('inputs') if p.startswith('image'))
        img = Image.open(f'inputs/{src}')
        img = ImageOps.exif_transpose(img)
        if img.mode in ('RGBA', 'LA', 'P'):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.convert('RGBA').split()[-1])
            img = bg
        img.save('outputs/out.jpg', 'JPEG', quality=92, optimize=True)
        print(f'wrote {img.size} JPEG')
    """,
    input_pod_ids=["<png pod id>"],
)
```

### "Make me a web-ready set: 320, 640, 1280 wide"

One call, multiple outputs (each auto-mints as a pod):

```python
execute_code(
    source="""
        import os
        from PIL import Image, ImageOps
        src = next(p for p in os.listdir('inputs') if p.startswith('image'))
        img = Image.open(f'inputs/{src}')
        img = ImageOps.exif_transpose(img)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        for w in (320, 640, 1280):
            copy = img.copy()
            copy.thumbnail((w, w * 10))   # height effectively unbounded
            copy.save(f'outputs/w{w}.jpg', 'JPEG', quality=85, optimize=True)
            print(f'w{w} -> {copy.size}')
    """,
    input_pod_ids=["<image pod id>"],
)
```

Result has three `output_pod_ids` — wire them into a downstream send-email or response.

## Anti-patterns

- **Don't `Image.open` on a hardcoded filename.** Pod files are named `image_<32 char id prefix>`, not `photo.jpg`. List the inputs dir and pick the first match.
- **Don't skip `exif_transpose`.** Phone photos look rotated without it. Always before resize/save.
- **Don't save PNG with `quality=`.** PNG ignores quality. Use `optimize=True`.
- **Don't `img.resize` when the user says "resize."** They almost always mean preserve aspect → use `thumbnail` or `ImageOps.contain`.
- **Don't read bytes from the pod URI directly in `source`.** The courier already staged them to disk; just open the file.
- **Don't write to `/workspace/inputs/`.** It's not read-only, but other skills assume input pods are immutable. Always write to `/workspace/outputs/`.
- **Don't ask for `egress_allowlist` unless the script makes HTTP calls.** Resize is pure-compute; default `--network=none` is correct.

## When to use a different tool

- **Need to see what's in the image** (describe it, detect a face, read a sign) — that's a vision LLM call (the `chat` tool with a vision model), not Pillow.
- **OCR a screenshot or scanned doc** — same sandbox tool, but `pytesseract`, different skill.
- **Compose a multi-image layout (collage, side-by-side)** — Pillow can, but for anything non-trivial layout-wise, consider `reportlab` (for PDF) or generating HTML and rendering it via `weasyprint`.
- **Generate a NEW image from a prompt** — text-to-image model API via `http_request` (e.g., fal.run), not Pillow.

## Why this is a skill, not a tool

A "resize_image" tool would hardcode one operation. Real requests are "resize, but also rotate if it's sideways, also convert to JPEG, also strip metadata." Each is a one-line Pillow change. As a skill, every new variant is a section here, not a new tool call signature. This is the same logic as the GitHub skill: the primitive (`execute_code` / `http_request`) stays generic; the *recipe* lives in the skill.

Related: [[sandboxed-python-execution]] (the parent tool), [[pod-courier]] (how pods land in the sandbox).
