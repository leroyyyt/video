# Local Video Downloader

A small self-hosted website that runs on **your own computer** and downloads
videos — single clips or whole playlists — as **MP4 video, iPhone-compatible
video, or MP3 audio**. The backend (FastAPI) runs `yt-dlp` + FFmpeg in a
background worker; the frontend is a button-driven page. Everything stays
local: the server binds strictly to `127.0.0.1`.

> **Note:** this is a local app, not a hosted website. GitHub Pages cannot run
> it (Pages only serves static files, and this needs Python + FFmpeg). Clone it
> and run it on your machine.

## Features

- Paste a video **or playlist** URL — playlists become one downloadable file
  per video, plus a **Download all (.zip)**.
- **Output Format** dropdown — choose **MP4**, **iPhone**, or **MP3** before
  downloading. The choice applies to single videos, playlists, and the whole
  queued batch.
- **Quality** dropdown — video resolution (MP4/iPhone) or audio bitrate (MP3),
  with automatic fallback to the next best available.
- **Queue** as many links as you like; they download one at a time to stay
  system-friendly.
- Live progress per video; per-item downloads and bulk **ZIP** export.
- Binds to `127.0.0.1` only; one active download at a time.

## Output formats

Pick one from the **Output Format** dropdown:

- **MP4 — normal video.** A standard MP4 video file. Use the **Quality**
  dropdown to choose a maximum resolution.
- **iPhone — iPhone-compatible video.** Still a video file (`.mp4`), but encoded
  with **H.264 video + AAC audio** so it plays natively on iPhone. This is the
  best choice for sending a clip through Telegram, saving it to the **Files**
  app, and playing it back (or extracting it) on the phone. Most videos up to
  1080p are already H.264/AAC, so no slow re-encode is needed; 4K and other
  VP9/AV1 sources are transcoded automatically.
- **MP3 — audio / music only.** Extracts just the audio and saves it as an
  `.mp3` file. Use this when you only want the music or audio track.

## Quality & fallback behaviour

The **Quality** dropdown changes with the chosen format:

- **MP4 / iPhone (video):** Best available, 4K / 2160p, 1440p, 1080p, 720p,
  480p, 360p.
- **MP3 (audio):** Best available, 320 kbps, 192 kbps, 128 kbps.

If the quality you pick isn't available, the **next best available** option is
used automatically — nothing fails. For example, if you choose **4K** but the
video tops out at **720p**, you get the 720p version. For MP3, the closest
available bitrate is used.

## Requirements

- Python 3.9+
- **FFmpeg** on your PATH — **strongly recommended** (effectively required).
  It merges separate video + audio streams, transcodes iPhone output to
  H.264/AAC, and extracts/encodes MP3 audio. Without it, MP4 merging, iPhone
  mode, and MP3 mode will not work, and the web UI shows a clear warning banner.
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

1. Paste a URL (the **Paste** button fills it for you).
2. Choose an **Output Format** (MP4 / iPhone / MP3) and a **Quality**, then
   press **Add**. The format and quality apply to that whole job — single
   video, every video in a playlist, and every link in the batch.
3. Watch progress; press **Download** on each finished item, or
   **Download all (.zip)** for playlists / multi-item jobs.
4. **×** removes a job; **Clear finished** tidies completed ones.

Per-item downloads go to your browser's normal download location — point it at
a `YoutubeVideos` folder if you'd like everything in one place. Files also live
in `downloads/` on the server until you remove the job or restart.

## Configuration

Top of `main.py`: `MAX_PLAYLIST_ITEMS`, `MAX_QUEUED_JOBS`, `HOST`, `PORT`.

## Legal

For personal use with content you own or have the right to download. Respect
each site's terms of service and copyright law.

## License

MIT — see [LICENSE](LICENSE).
