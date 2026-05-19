# emi-sandbox Docker image

This is the runtime container for Emi's `execute_code` tool. Emi spawns one short-lived container of this image per call.

## Build

One-time, after cloning or updating the Dockerfile:

```bash
docker build -t emi-sandbox:v1 docker/sandbox/
```

Takes 3–6 minutes (downloads python:3.11-slim base, installs system deps + ~20 Python libs). Resulting image is ~1.5 GB.

## Verify

```bash
docker run --rm emi-sandbox:v1 python -c "import pandas; print(pandas.__version__)"
# expect: 2.2.3
```

## What's pre-installed

| Category | Libraries |
|---|---|
| Numerical | numpy, pandas, scipy, scikit-learn |
| Charting | matplotlib, seaborn |
| Image | Pillow, opencv-python-headless, pytesseract (+ tesseract-ocr binary) |
| Documents | openpyxl, pypdf, pymupdf, reportlab (+ poppler-utils binary) |
| Web / HTTP | requests, httpx, beautifulsoup4, lxml |
| System | ffmpeg, build-essential |

## Adding deps not in the base image

The `execute_code` tool accepts a `requirements: list[str]` argument that pip-installs on top of the base image at call time. Slow (~15–60s per package) but covers the long tail (faster-whisper, weasyprint, HuggingFace transformers, etc.).

If a particular dep is needed often, add it to the Dockerfile and rebuild.

## What's NOT in the image

- Network access by default — Emi's `execute_code` tool sets `--network=none` unless the caller declares an `egress_allowlist`. Even when network is on, audit logs every domain hit.
- GPU access — CPU-only. GPU passthrough is a v2 feature for ML workloads.
- Filesystem outside `/workspace` — the container has no view of the host fs except what Emi explicitly bind-mounts per call.

## Resource limits (set per call by Emi, not in this image)

- CPU: 2 cores
- Memory: 2 GB
- PIDs: 512
- Timeout: 30s default, 5min hard cap

## Tearing down

Old workspaces get cleaned by a daily routine. To purge images:

```bash
docker rmi emi-sandbox:v1
docker rmi $(docker images -q python:3.11-slim)
```
