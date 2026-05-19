# emi-sandbox:v1 — execution environment for the execute_code tool.
#
# This image is the *sandbox*, NOT Emi itself. Emi (running on the host)
# spawns one short-lived container of this image per execute_code call,
# mounts the per-call workspace, runs the user-supplied script under
# resource limits, and tears the container down.
#
# Size budget: ~1.5 GB. Pre-installed libs cover the common cases (data
# analysis, image manipulation, document processing, web HTTP). Unusual
# deps go through the tool's `requirements` arg (slow pip install on top
# of this base).
#
# Build (one-time on host):
#   docker build -t emi-sandbox:v1 docker/sandbox/
#
# Verify:
#   docker run --rm emi-sandbox:v1 python -c "import pandas; print(pandas.__version__)"

FROM python:3.11-slim

# ---- system packages -------------------------------------------------------
# - ffmpeg: audio/video transcoding (used by sandbox skills like html-to-mp4,
#   audio extraction, faster-whisper input prep)
# - tesseract-ocr: OCR backend for pytesseract (receipts, scanned PDFs)
# - libgl1, libglib2.0-0: shared libs that opencv-python-headless still needs
# - poppler-utils: PDF text extraction backend used by some PDF libs
# - build-essential: kept slim by removing apt cache; needed by a few wheels
#   that build native extensions on install
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr \
        libgl1 \
        libglib2.0-0 \
        poppler-utils \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# ---- python packages --------------------------------------------------------
# Grouped by purpose to make it obvious what each install line is for and to
# let the Docker layer cache invalidate predictably.

# Numerical + data analysis
RUN pip install --no-cache-dir \
        numpy==1.26.4 \
        pandas==2.2.3 \
        scipy==1.13.1 \
        scikit-learn==1.5.2

# Charting
RUN pip install --no-cache-dir \
        matplotlib==3.9.2 \
        seaborn==0.13.2

# Image processing
RUN pip install --no-cache-dir \
        Pillow==10.4.0 \
        opencv-python-headless==4.10.0.84 \
        pytesseract==0.3.13

# Documents (PDF, spreadsheet)
RUN pip install --no-cache-dir \
        openpyxl==3.1.5 \
        pypdf==5.1.0 \
        pymupdf==1.24.10 \
        reportlab==4.2.5

# Web / HTTP
RUN pip install --no-cache-dir \
        requests==2.32.3 \
        httpx==0.27.2 \
        beautifulsoup4==4.12.3 \
        lxml==5.3.0

# ---- workspace --------------------------------------------------------------
# The host mounts the per-call workspace at /workspace. The script lives at
# /workspace/script.py; input pods at /workspace/inputs/; output files at
# /workspace/outputs/.
WORKDIR /workspace

# Set Python to unbuffered so stdout/stderr stream out promptly.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Default entrypoint runs the script. Host overrides via `docker run` if needed
# (e.g., to install requirements before running).
ENTRYPOINT ["python", "/workspace/script.py"]
