---
name: sandboxed-python-execution
description: When and how to use the `execute_code` tool to run Python in a Docker sandbox. Covers pod-aware input/output, sealed responses, network policy, the pre-installed library set, and the patterns where execute_code is the right answer vs the wrong one. Read this before reaching for a 30-line script when a one-line http_request would do, OR before giving up because no purpose-built tool exists.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "compute"
      - "analyze data"
      - "convert"
      - "transcode"
      - "OCR"
      - "extract text"
      - "PDF"
      - "spreadsheet"
      - "CSV"
      - "image processing"
      - "resize image"
      - "generate report"
      - "Python script"
      - "run code"
---

# Sandboxed Python execution via `execute_code`

The `execute_code` tool runs Python inside a fresh Docker container. Use it when you need a real interpreter — anything from `pandas` analysis to PDF generation to OCR — and there's no purpose-built tool for the job.

It is not a chat scratchpad. Reach for it when **the work needs files, libraries, or compute**, not for what you can do in your reasoning step.

## When to use this tool

| Situation | Right tool |
|---|---|
| Sum a CSV, group by column, plot a chart | `execute_code` (pandas + matplotlib) |
| Resize, crop, convert, watermark an image | `execute_code` (Pillow) |
| Extract text from a PDF, scan an image (OCR) | `execute_code` (pymupdf, pytesseract) |
| Build a PDF report, spreadsheet, Word doc | `execute_code` (reportlab, openpyxl) |
| Transcode audio/video, extract audio from video | `execute_code` (ffmpeg via subprocess) |
| Parse a complex JSON/HTML structure and extract a few fields | `execute_code` (json, bs4) |
| Compute statistics, fit a model, simulate | `execute_code` (scipy, scikit-learn) |
| A single HTTP GET / POST | **`http_request`** (NOT execute_code) |
| Browse a website with a session | **`playwright_manager`** |
| Read a local file the user pointed to | **`bash_manager`** |
| A one-line text transformation | **Just do it in your reasoning** |

Three negative tests for "should I really use this tool":
1. Could `http_request` do it? → use that.
2. Is the script under 3 lines of trivial logic? → just do it inline.
3. Does the user need to see exactly what ran (audit, trust)? → fine, use it.

## Pod-aware input

Pass pod URIs in `input_pod_ids=[...]`. Each one is resolved at courier scope (the highest authority band) and the bytes get written into `/workspace/inputs/<kind>_<id_prefix>`. The agent calling `execute_code` does NOT see the contents — the courier writes directly to disk.

```python
# call:
execute_code(
    source="""
        from PIL import Image
        import os
        # Files are named <kind>_<id>; list to find yours.
        photo = next(p for p in os.listdir('inputs') if 'photo' in p or 'image' in p)
        img = Image.open(f'inputs/{photo}')
        img.thumbnail((512, 512))
        img.save('outputs/thumb.png')
        print(f'thumbnail: {img.size}')
    """,
    input_pod_ids=["datapod:image:abc123..."],
)
```

If the pod has projection-style storage (identity / auth / artifact), the highest-authority projection courier can read is staged. If it's a body-style pod (chat_cluster, email, tool_result), `PodRow.body` is staged.

## Pod-aware output

Anything you write to `/workspace/outputs/` automatically becomes a new pod. The result's `output_pod_ids` lists them. Downstream skills (or another `execute_code` call) can ingest those by passing them as `input_pod_ids` next time.

```python
execute_code(
    source="""
        import pandas as pd
        df = pd.read_csv('inputs/transactions_abc.csv')
        agg = df.groupby('category')['amount'].sum()
        agg.to_csv('outputs/by_category.csv')
        print(agg.to_string())
    """,
    input_pod_ids=["datapod:tool_result:exec_..._transactions.csv"],
)
# → result.data.output_pod_ids includes a new pod for by_category.csv
```

## Sealed responses (sensitive output)

If the stdout will contain sensitive data (medical, financial, personal), set `response_pod_kind`. The stdout goes into a pod of that kind with the matching authority band; the calling agent gets back `response_pod_id` instead of seeing the bytes. A downstream skill at the appropriate authority can `pod_fetch` it.

```python
execute_code(
    source="""
        import pandas as pd
        df = pd.read_csv('inputs/health.csv')
        print(df.describe().to_string())
    """,
    input_pod_ids=["datapod:health.private:..."],
    response_pod_kind="health.private",
)
# → result.data has response_pod_id but NO stdout field.
```

## Network policy

**By default the container has NO network access** (`--network=none`). Sockets fail immediately. This is correct for most uses (data analysis, image work, document generation).

To allow network: pass `egress_allowlist=["api.example.com", "fal.run"]`. In v1, this lifts the network block entirely and audits the call; per-domain enforcement is v1.1. **Treat `egress_allowlist` as a budget commitment, not a soft suggestion.**

If you only need ONE HTTP call, do not use `execute_code` — use `http_request`. Reserve `execute_code` + network for cases where the script makes many calls in a loop, processes the responses, or needs a Python SDK that's not worth wrapping.

## Pre-installed libraries (no `requirements` needed)

| Category | Libraries |
|---|---|
| Data | numpy, pandas, scipy, scikit-learn |
| Charts | matplotlib, seaborn |
| Image | Pillow, opencv-python (headless), pytesseract (+ tesseract OCR binary) |
| PDF | pypdf, pymupdf, reportlab |
| Spreadsheet | openpyxl |
| Web / HTTP | requests, httpx, beautifulsoup4, lxml |
| Audio/Video | (use `subprocess` to call `ffmpeg`) |

If you need something else, pass `requirements=["faster-whisper", "weasyprint"]`. **This is slow** (~15–60s per package); only worth it when the work is non-trivial.

## Resource and time budget

- CPU: 2 cores
- Memory: 2 GB
- PIDs: 512
- Filesystem outside `/workspace`: read-only
- `/tmp`: 512 MB tmpfs
- Timeout: 30s default, hard cap 5 minutes

If a task will take longer than 5 minutes, it doesn't belong here — make it a dayflow routine.

## Error patterns

- `error_code: docker_not_installed` → host hasn't started Docker Desktop. Surface this to the user as "I need Docker running" rather than retrying.
- `error_code: timeout` → script ran past `timeout_s`. Don't blindly retry with a higher timeout; ask whether this should be a routine instead.
- `error_code: unsupported_language` → v1 is Python only.
- `error_code: invalid_arguments` → empty `source` slipped through. Don't call with an empty body.
- `result.data.ok = False` with non-zero exit → the script itself failed. `stderr` (truncated to 256 chars) is in `result.data.stderr`; for the full trace, run again with `response_pod_kind` and read the pod.

## Anti-patterns

- **Don't** wrap `http_request` work in `execute_code`. Use `http_request` directly — it's pod-aware, audited, and doesn't spin up a container.
- **Don't** use `execute_code` to read host files. Use `bash_manager` for that. `execute_code`'s view of the host fs is whatever you bind-mount via `fs_allowlist`, and that path should be rare.
- **Don't** stuff bytes of the input into `source` as a literal. Pass it as a pod (`input_pod_ids`).
- **Don't** print sensitive data without `response_pod_kind`. The bytes will land in the result for any agent in the conversation to see.
- **Don't** assume packages outside the pre-installed set are present. Either check the list above, or pass `requirements`.

## Authority

`execute_code` is gated at **AUTH_USER (99)**. The user must approve every call (or pre-authorize the room). Dayflow can REQUEST a sandbox run via a ticket but cannot fire one autonomously — that's intentional, since the sandbox is high-trust.
