LOCAL VIDEO DOWNLOADER
======================

A small website that runs on YOUR computer and downloads videos (or whole
playlists) as MP4 files. Everything stays on your machine (127.0.0.1).

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
FFmpeg merges the video + audio into the final MP4. Install it once:
  Windows :  winget install Gyan.FFmpeg
  macOS   :  brew install ffmpeg
  Linux   :  sudo apt install ffmpeg
The launcher will warn you if it's missing.

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
