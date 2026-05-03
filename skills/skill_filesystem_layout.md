# Filesystem layout — Jukka's Windows machine

Knowledge for any agent that runs filesystem commands (e.g. via `run_shell`).

## Documents folder is OneDrive-redirected

Jukka's "Documents" folder is managed by OneDrive. The bash command rewriter
already maps `~/Documents` to the OneDrive-managed location automatically, so
in normal usage just write `~/Documents` and it Just Works.

What you may encounter under each path:

- `~/Documents` — auto-rewritten to `~/OneDrive/Documents`. ~58 active folders
  (CVs, projects, books, ML articles, etc.). This is the live Documents.
- `C:\Users\semis\Documents` — the **legacy** local folder, frozen since 2022.
  Only contains a stale resume PDF, a few Windows shell symlinks (My Music,
  My Pictures, My Videos), and an old Visual Studio 2019 folder. Ignore this
  unless you're explicitly asked about pre-OneDrive content.
- `~/OneDrive/Documents` — same as `~/Documents` (the redirect target).

If a search under `~/Documents` comes up empty for something Jukka clearly
has, do **not** assume it's missing. Check `C:\Users\semis\Documents` as a
last resort, but expect the OneDrive-managed location to have the real data.

## What Windows considers "Documents"

File Explorer's sidebar entry labeled "Documents" maps to the OneDrive path
above, not the local one. The authoritative source is the registry value at
`HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders\Personal`.

## Other shell folders are also OneDrive-redirected

Pictures, Music, and Videos use the same redirection pattern as Documents.
The bash command rewriter automatically maps:
- `~/Pictures` → `~/OneDrive/Pictures`
- `~/Music`    → `~/OneDrive/Music`
- `~/Videos`   → `~/OneDrive/Videos`

Just write the natural `~/Pictures/foo.jpg` and it Just Works. The legacy
`C:\Users\semis\Pictures` etc. are usually empty.

## Reachable directories (run_shell scope)

`run_shell` is scoped to these directories — paths outside are refused:
- `~/Documents` (auto-resolves to OneDrive)
- `~/Pictures`
- `~/Music`
- `~/Videos`

## Sending a file by email

To email a local file (image, PDF, video, anything):
1. Find the file (use `run_shell ls` if the user didn't give an exact path).
2. Mint a pod from its path with `mint_pod_from_path`. This is idempotent:
   the same file always returns the same `pod_id`.
3. Pass the returned `pod_id` to `send_email` as `pod_ids: ["<pod_id>"]`.
   `send_email` resolves the pod, attaches the bytes with the original
   filename and mime type, and sends.

Do NOT try to attach files by raw path — `send_email` only accepts pods.
