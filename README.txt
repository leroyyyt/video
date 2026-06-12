LOCAL VIDEO DOWNLOADER
======================

A small website that runs on YOUR computer and downloads videos (or whole
playlists) as MP4 video, iPhone-compatible video, or MP3 audio. Everything
stays on your machine (127.0.0.1).

------------------------------------------------------------------
OUTPUT FORMAT  (choose before pressing Add)
------------------------------------------------------------------
  MP4    = normal video file.
  iPhone = video file (.mp4) encoded H.264/AAC so it plays natively on
           iPhone — best for sending via Telegram and opening from the
           Files app.
  MP3    = audio / music only, saved as an .mp3 file.

QUALITY: pick a video resolution (MP4/iPhone) or audio bitrate (MP3). If the
chosen quality isn't available, the next best one is used automatically (e.g.
choose 4K on a 720p video and you get 720p). The format + quality you pick
apply to single videos, playlists, AND the whole queued batch.

------------------------------------------------------------------
HOW TO START  (no commands to type)
------------------------------------------------------------------
  Windows :  double-click  run.bat
  macOS   :  double-click  run.command
             (first time: right-click -> Open, to clear the security prompt)
  Linux   :  double-click  run.sh   (or: ./run.sh)

The first launch sets things up automatically and then opens the app in your
browser at  http://127.0.0.1:8000 . Keep the small window open while using it;
close it to stop the app.

------------------------------------------------------------------
ONE REQUIREMENT: FFmpeg
------------------------------------------------------------------
FFmpeg is strongly recommended (effectively required): it merges video +
audio, transcodes iPhone output to H.264/AAC, and extracts MP3 audio. Without
it, MP4 / iPhone / MP3 modes will not work and the web page shows a warning.
Install it once:
  Windows :  winget install Gyan.FFmpeg
  macOS   :  brew install ffmpeg
  Linux   :  sudo apt install ffmpeg
The launcher and the web UI both warn you if it's missing.

------------------------------------------------------------------
USING IT
------------------------------------------------------------------
  1. Paste a video or playlist URL (the "Paste" button fills it for you).
  2. Press "Add". Queue as many as you like — they run one at a time.
  3. When a video is ready, press its "Download" button.
     Playlists also get a "Download all (.zip)" button.
  4. The "x" removes a job; "Clear finished" tidies everything done.

Downloaded files live in the  downloads/  folder until you remove the job
or restart the app.

Only download content you own or have the right to download, and respect
each site's terms of service and copyright.
