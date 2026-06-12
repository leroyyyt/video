# Local Video Downloader

A small self-hosted website that runs on **your own computer** and downloads
videos — single clips or whole playlists — as MP4 files. The backend
(FastAPI) runs `yt-dlp` + FFmpeg in a background worker; the frontend is a
button-driven page. Everything stays local: the server binds strictly to
`127.0.0.1`.

> **Note:** this is a local app, not a hosted website. GitHub Pages cannot run
> it (Pages only serves static files, and this needs Python + FFmpeg). Clone it
> and run it on your machine.

## Features

- Paste a video **or playlist** URL — playlists become one downloadable file
  per video, plus a **Download all (.zip)**.
- **Queue** as many links as you like; they download one at a time to stay
  system-friendly.
- Live progress per video; per-item and bulk downloads.
- Binds to `127.0.0.1` only; one active download at a time.

## Requirements

- Python 3.9+
- **FFmpeg** on your PATH (merges video + audio):
  - Windows: `winget install Gyan.FFmpeg`
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`

## Run it (no commands needed)

- **Windows:** double-click `run.bat`
- **macOS:** double-click `run.command` (first time: right-click → Open)
- **Linux:** `./run.sh`

The launcher sets up a private environment on first run, then opens
`http://127.0.0.1:8000` in your browser. Keep the window open while using it.

### Manual run

```bash
pip install -r requirements.txt
python main.py
```

## Usage

1. Paste a URL (the **Paste** button fills it for you) and press **Add**.
2. Watch progress; press **Download** on each finished video, or
   **Download all (.zip)** for playlists.
3. **×** removes a job; **Clear finished** tidies completed ones.

Files live in `downloads/` until you remove the job or restart.

## Configuration

Top of `main.py`: `MAX_PLAYLIST_ITEMS`, `MAX_QUEUED_JOBS`, `HOST`, `PORT`.

## Legal

For personal use with content you own or have the right to download. Respect
each site's terms of service and copyright law.

## License

MIT — see [LICENSE](LICENSE).
