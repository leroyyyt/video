"""
Local Video Downloader — FastAPI backend (queue + playlists + output formats).

Highlights:
  * Bind strictly to 127.0.0.1 (never reachable over LAN / internet).
  * A single background worker thread processes a FIFO queue, so blocking
    yt-dlp/FFmpeg work never touches the event loop and only ONE download
    runs at a time (system-friendly) while you can queue as many as you like.
  * A "job" = one submitted URL. A job has one or more "items" (videos).
      - single video  -> 1 item
      - playlist       -> N items (capped by MAX_PLAYLIST_ITEMS)
  * Each job carries an OUTPUT FORMAT and a QUALITY choice that apply to every
    item in the job (single, playlist, or batch):
      - mp4    -> normal MP4 video, pick a max video height (or "best").
      - iphone -> iPhone-compatible MP4 (H.264 video + AAC audio).
      - mp3    -> audio-only MP3 (pick a target bitrate or "best").
    Quality always falls back to the next best available option.
  * Every produced file is downloadable on its own; multi-item jobs also offer a
    "download all" ZIP. Per-item downloads land in your browser's downloads
    folder (point it at a "YoutubeVideos" folder if you like).
  * FFmpeg is required for merging, iPhone re-encoding and MP3 extraction. If it
    is missing the UI shows a clear warning and jobs that need it fail loudly
    rather than crashing silently.
"""

from __future__ import annotations

import copy
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yt_dlp
from yt_dlp.postprocessor import PostProcessor
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, field_validator
from starlette.background import BackgroundTask

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
STATIC_DIR = BASE_DIR / "static"
DOWNLOAD_DIR.mkdir(exist_ok=True)

HOST = "127.0.0.1"          # hardcoded loopback bind — see __main__
PORT = 8000
MAX_QUEUED_JOBS = 50        # soft cap on queued/processing jobs
MAX_PLAYLIST_ITEMS = 50     # cap videos pulled from a single playlist

# ---- Output formats & quality ---------------------------------------------

OUTPUT_FORMATS = ("mp4", "iphone", "mp3")

# Video quality choices -> max height in pixels (None = best available).
VIDEO_QUALITIES: Dict[str, Optional[int]] = {
    "best": None,
    "2160": 2160,
    "1440": 1440,
    "1080": 1080,
    "720": 720,
    "480": 480,
    "360": 360,
}

# Audio (MP3) quality choices -> ffmpeg preferredquality value.
# "best" maps to yt-dlp's "0" (best VBR). Others are target kbps.
AUDIO_QUALITIES: Dict[str, str] = {
    "best": "0",
    "320": "320",
    "192": "192",
    "128": "128",
}

# Final container/extension produced for each output format.
OUTPUT_EXT = {"mp4": "mp4", "iphone": "mp4", "mp3": "mp3"}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s"
)
logger = logging.getLogger("downloader")


# ---------------------------------------------------------------------------
# FFmpeg detection
# ---------------------------------------------------------------------------

def ffmpeg_available() -> bool:
    """True if an ffmpeg binary is on PATH (re-checked each call — cheap)."""
    return shutil.which("ffmpeg") is not None


def ffprobe_path() -> Optional[str]:
    return shutil.which("ffprobe")


# ---------------------------------------------------------------------------
# Format-selector builders (yt-dlp `format` strings) + postprocessors
# ---------------------------------------------------------------------------

def _mp4_format(height: Optional[int]) -> str:
    """Normal MP4. Prefer mp4/m4a streams; fall back to any, then to a lower
    quality automatically. yt-dlp picks the best available <= height, so
    choosing 4K on a 720p video simply yields 720p."""
    if height is None:
        return (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/best"
        )
    h = height
    return (
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={h}]+bestaudio/"
        f"best[height<={h}]/"
        f"bestvideo+bestaudio/best"
    )


def _iphone_format(height: Optional[int]) -> str:
    """iPhone-compatible MP4. Strongly prefer H.264 (avc1) video + AAC (mp4a)
    audio so no re-encode is needed for typical YouTube content (<=1080p).
    The IPhoneCompatPP below re-encodes anything that slipped through."""
    if height is None:
        return (
            "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/best"
        )
    h = height
    return (
        f"bestvideo[height<={h}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={h}]+bestaudio/"
        f"best[height<={h}]/"
        f"bestvideo+bestaudio/best"
    )


def _audio_format() -> str:
    """Best audio stream; the MP3 extractor postprocessor converts it."""
    return "bestaudio/best"


class IPhoneCompatPP(PostProcessor):
    """Ensure the final file is H.264 video + AAC audio in an MP4 container.

    Most YouTube videos up to 1080p already ship avc1/mp4a, so the format
    selector usually avoids any re-encode. For sources that are VP9/AV1/Opus
    (e.g. 4K), this transcodes to H.264/AAC so the file plays natively on
    iPhone after Telegram / Files.
    """

    def run(self, info):
        path = info.get("filepath")
        if not path or not os.path.exists(path):
            return [], info
        ffprobe = ffprobe_path()
        vcodec = acodec = ""
        if ffprobe:
            try:
                vcodec = subprocess.run(
                    [ffprobe, "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=codec_name",
                     "-of", "default=nw=1:nk=1", path],
                    capture_output=True, text=True, timeout=30,
                ).stdout.strip()
                acodec = subprocess.run(
                    [ffprobe, "-v", "error", "-select_streams", "a:0",
                     "-show_entries", "stream=codec_name",
                     "-of", "default=nw=1:nk=1", path],
                    capture_output=True, text=True, timeout=30,
                ).stdout.strip()
            except Exception:
                self.to_screen("ffprobe failed; assuming re-encode needed")

        already_ok = vcodec in ("h264", "avc1") and acodec in ("aac", "mp4a")
        target = os.path.splitext(path)[0] + ".mp4"
        if already_ok and path.lower().endswith(".mp4"):
            return [], info  # nothing to do — fast path

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise yt_dlp.utils.PostProcessingError(
                "FFmpeg is required for iPhone output but was not found."
            )

        tmp_out = os.path.splitext(path)[0] + ".iphone.tmp.mp4"
        # Copy streams that are already compatible; transcode the rest.
        vargs = ["-c:v", "copy"] if vcodec in ("h264", "avc1") else \
            ["-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1"]
        aargs = ["-c:a", "copy"] if acodec in ("aac", "mp4a") else \
            ["-c:a", "aac", "-b:a", "192k"]
        cmd = [ffmpeg, "-y", "-i", path, *vargs, *aargs,
               "-movflags", "+faststart", tmp_out]
        self.to_screen("Making file iPhone-compatible (H.264/AAC)…")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not os.path.exists(tmp_out):
            raise yt_dlp.utils.PostProcessingError(
                "FFmpeg failed to produce an iPhone-compatible file:\n"
                + proc.stderr[-800:]
            )
        # Replace original with the .mp4 result.
        if path != target:
            Path(path).unlink(missing_ok=True)
        os.replace(tmp_out, target)
        info["filepath"] = target
        return [], info


def build_ydl_opts(
    output_format: str,
    quality: str,
    outtmpl: str,
    progress_hook: Callable,
    pp_hook: Callable,
) -> Tuple[Dict[str, Any], List[PostProcessor]]:
    """Construct yt-dlp options + extra postprocessors for a given format."""
    opts: Dict[str, Any] = {
        "outtmpl": outtmpl,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
    }
    extra_pps: List[PostProcessor] = []

    if output_format == "mp3":
        opts["format"] = _audio_format()
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": AUDIO_QUALITIES.get(quality, "192"),
        }]
    elif output_format == "iphone":
        height = VIDEO_QUALITIES.get(quality, None)
        opts["format"] = _iphone_format(height)
        opts["merge_output_format"] = "mp4"
        extra_pps.append(IPhoneCompatPP())
    else:  # mp4 (default)
        height = VIDEO_QUALITIES.get(quality, None)
        opts["format"] = _mp4_format(height)
        opts["merge_output_format"] = "mp4"

    return opts, extra_pps


# ---------------------------------------------------------------------------
# In-memory, thread-safe job store  +  FIFO work queue
# ---------------------------------------------------------------------------

_jobs: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()
_task_queue: "queue.Queue[str]" = queue.Queue()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(url: str, output_format: str, quality: str) -> str:
    """Register a queued job and enqueue it for the worker."""
    with _lock:
        active = sum(
            1 for j in _jobs.values() if j["status"] in ("queued", "processing")
        )
        if active >= MAX_QUEUED_JOBS:
            raise HTTPException(
                status_code=429,
                detail=f"Queue is full ({MAX_QUEUED_JOBS}). Let some finish first.",
            )
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {
            "id": job_id,
            "url": url,
            "output_format": output_format,
            "quality": quality,
            "type": None,            # video | playlist
            "status": "queued",      # queued | processing | completed | partial | failed
            "title": None,
            "error": None,
            "items": [],             # list of item dicts
            "created_at": _now(),
            "updated_at": _now(),
        }
    _task_queue.put(job_id)
    logger.info("Queued job %s (%s) [%s/%s]", job_id, url, output_format, quality)
    return job_id


def update_job(job_id: str, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)
            job["updated_at"] = _now()


def set_items(job_id: str, items: List[Dict[str, Any]]) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["items"] = items
            job["updated_at"] = _now()


def update_item(job_id: str, index: int, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        for it in job["items"]:
            if it["index"] == index:
                it.update(fields)
                break
        job["updated_at"] = _now()


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        job = _jobs.get(job_id)
        return copy.deepcopy(job) if job else None


def all_jobs() -> List[Dict[str, Any]]:
    with _lock:
        return [copy.deepcopy(j) for j in _jobs.values()]


def delete_job(job_id: str) -> None:
    with _lock:
        _jobs.pop(job_id, None)


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def _safe_filename(title: str, ext: str = "mp4") -> str:
    base = _SAFE_NAME.sub("_", title or "video").strip().strip(".") or "video"
    return f"{base[:120]}.{ext}"


def _pick_output(paths: List[Path], prefer_ext: str = "mp4") -> Optional[Path]:
    real = [
        p for p in paths
        if p.is_file() and p.suffix.lower() not in (".part", ".ytdl")
        and ".part-" not in p.name and ".tmp." not in p.name
    ]
    if not real:
        return None
    preferred = [p for p in real if p.suffix.lower() == f".{prefer_ext.lower()}"]
    return max(preferred or real, key=lambda p: p.stat().st_size)


def _find_named_file(job_dir: Path, stem: str, prefer_ext: str = "mp4") -> Optional[Path]:
    return _pick_output(list(job_dir.glob(f"{stem}.*")), prefer_ext)


def _find_indexed_file(job_dir: Path, n: int, prefer_ext: str = "mp4") -> Optional[Path]:
    return _pick_output(list(job_dir.glob(f"{n:03d}.*")), prefer_ext)


def _remove_job_files(job_id: str) -> None:
    d = DOWNLOAD_DIR / job_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    for p in DOWNLOAD_DIR.glob(f"{job_id}-*.zip"):
        try:
            p.unlink()
        except OSError:
            pass


def _safe_unlink(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
        logger.info("Deleted temp %s", Path(path).name)
    except OSError as exc:
        logger.warning("Could not delete %s: %s", path, exc)


def _sweep_all() -> None:
    for p in DOWNLOAD_DIR.iterdir():
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
        except OSError:
            pass


def _media_type(filename: str) -> str:
    return "audio/mpeg" if filename.lower().endswith(".mp3") else "video/mp4"


# ---------------------------------------------------------------------------
# yt-dlp progress plumbing
# ---------------------------------------------------------------------------

def _make_hooks(
    job_id: str, resolver: Callable[[Dict[str, Any]], Optional[int]]
) -> Tuple[Callable, Callable]:
    """Build (progress_hook, postprocessor_hook) that route updates to the
    correct item via `resolver(d) -> item index | None`."""

    def progress_hook(d: Dict[str, Any]) -> None:
        i = resolver(d)
        if i is None:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total * 100) if total else 0.0
            update_item(
                job_id, i, status="processing", stage="downloading",
                progress=round(min(pct, 100.0), 1),
            )
        elif status == "finished":
            update_item(job_id, i, stage="processing")

    def pp_hook(d: Dict[str, Any]) -> None:
        i = resolver(d)
        if i is None:
            return
        if d.get("status") == "started":
            update_item(job_id, i, stage="converting")
        elif d.get("status") == "finished":
            update_item(job_id, i, stage="finalizing")

    return progress_hook, pp_hook


def _new_item(index: int, title: str) -> Dict[str, Any]:
    return {
        "index": index,
        "title": title,
        "status": "queued",     # queued | processing | completed | failed
        "stage": "queued",
        "progress": 0.0,
        "filepath": None,
        "filename": None,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Worker — runs in its own thread; processes the queue serially
# ---------------------------------------------------------------------------

def worker_loop() -> None:
    while True:
        job_id = _task_queue.get()
        try:
            process_job(job_id)
        except Exception:  # never let the worker die
            logger.exception("Worker error on job %s", job_id)
            update_job(job_id, status="failed", error="Internal error")
        finally:
            _task_queue.task_done()


def _probe(url: str) -> Dict[str, Any]:
    """Cheaply determine single-video vs playlist and list entries."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",   # flatten playlist entries; full info for a lone video
        "skip_download": True,
        "playlistend": MAX_PLAYLIST_ITEMS,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        entries = [e for e in (info.get("entries") or []) if e][:MAX_PLAYLIST_ITEMS]
        return {"type": "playlist", "title": info.get("title") or "Playlist", "entries": entries}
    return {"type": "video", "title": info.get("title") or "Video", "entries": [info]}


def process_job(job_id: str) -> None:
    job = get_job(job_id)
    if job is None:          # removed while queued
        return
    url = job["url"]
    output_format = job.get("output_format", "mp4")
    quality = job.get("quality", "best")

    # Hard requirement: every output format here relies on FFmpeg (merging,
    # transcoding or audio extraction). Fail loudly instead of crashing silently.
    if not ffmpeg_available():
        update_job(
            job_id, status="failed",
            error="FFmpeg not found. Install FFmpeg and restart — it is required "
                  "for MP4 merging, iPhone re-encoding and MP3 extraction.",
        )
        return

    update_job(job_id, status="processing")
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    try:
        meta = _probe(url)
    except Exception as exc:
        logger.exception("Probe failed for %s", url)
        update_job(job_id, status="failed", error=f"Could not read URL: {exc}")
        return

    if meta["type"] == "playlist":
        _download_playlist(job_id, job_dir, url, meta, output_format, quality)
    else:
        _download_single(job_id, job_dir, url, meta, output_format, quality)

    _finalize(job_id)


def _download_single(
    job_id: str, job_dir: Path, url: str, meta: Dict[str, Any],
    output_format: str, quality: str,
) -> None:
    update_job(job_id, type="video", title=meta["title"])
    set_items(job_id, [_new_item(0, meta["title"])])
    ext = OUTPUT_EXT.get(output_format, "mp4")

    progress_hook, pp_hook = _make_hooks(job_id, lambda d: 0)
    opts, extra_pps = build_ydl_opts(
        output_format, quality, str(job_dir / "video.%(ext)s"),
        progress_hook, pp_hook,
    )
    opts["noplaylist"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            for pp in extra_pps:
                ydl.add_post_processor(pp, when="post_process")
            info = ydl.extract_info(url, download=True)
        title = info.get("title") or meta["title"]
        produced = _find_named_file(job_dir, "video", ext)
        if produced is None:
            raise RuntimeError("Download finished but no output file was found.")
        update_item(
            job_id, 0, status="completed", stage="done", progress=100.0,
            title=title, filepath=str(produced), filename=_safe_filename(title, ext),
        )
        logger.info("Job %s item 0 -> %s", job_id, produced.name)
    except Exception as exc:
        logger.exception("Single download failed for %s", url)
        update_item(job_id, 0, status="failed", stage="error", error=str(exc))


def _download_playlist(
    job_id: str, job_dir: Path, url: str, meta: Dict[str, Any],
    output_format: str, quality: str,
) -> None:
    entries = meta["entries"]
    update_job(job_id, type="playlist", title=meta["title"])
    set_items(job_id, [
        _new_item(i, e.get("title") or f"Video {i + 1}") for i, e in enumerate(entries)
    ])
    ext = OUTPUT_EXT.get(output_format, "mp4")

    def resolver(d: Dict[str, Any]) -> Optional[int]:
        idx = (d.get("info_dict") or {}).get("playlist_index")
        return (idx - 1) if isinstance(idx, int) else None

    progress_hook, pp_hook = _make_hooks(job_id, resolver)
    opts, extra_pps = build_ydl_opts(
        output_format, quality, str(job_dir / "%(playlist_index)03d.%(ext)s"),
        progress_hook, pp_hook,
    )
    opts["ignoreerrors"] = True          # skip a bad video, keep going
    opts["playlistend"] = MAX_PLAYLIST_ITEMS
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            for pp in extra_pps:
                ydl.add_post_processor(pp, when="post_process")
            ydl.download([url])
    except Exception:
        logger.exception("Playlist download error for %s", url)

    # Reconcile produced files with items (file mapping is by index = source of truth).
    job = get_job(job_id)
    for it in job["items"]:
        i = it["index"]
        produced = _find_indexed_file(job_dir, i + 1, ext)
        if produced is not None:
            update_item(
                job_id, i, status="completed", stage="done", progress=100.0,
                filepath=str(produced), filename=_safe_filename(it["title"], ext),
            )
        elif it["status"] != "completed":
            update_item(
                job_id, i, status="failed", stage="error",
                error=it.get("error") or "Video unavailable or skipped",
            )


def _finalize(job_id: str) -> None:
    job = get_job(job_id)
    if job is None:
        return
    items = job["items"]
    if not items:
        update_job(job_id, status="failed", error="No videos found")
        return
    done = sum(1 for it in items if it["status"] == "completed")
    if done == len(items):
        update_job(job_id, status="completed")
    elif done == 0:
        update_job(job_id, status="failed", error="All downloads failed")
    else:
        update_job(job_id, status="partial")
    logger.info("Job %s finished: %d/%d ok", job_id, done, len(items))


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    _sweep_all()
    threading.Thread(target=worker_loop, name="dl-worker", daemon=True).start()
    if not ffmpeg_available():
        logger.warning(
            "FFmpeg NOT found on PATH — MP4 merging, iPhone output and MP3 "
            "extraction will not work until you install it."
        )
    logger.info("Ready on http://%s:%s", HOST, PORT)
    yield


app = FastAPI(title="Local Video Downloader", docs_url=None, redoc_url=None, lifespan=lifespan)


class JobRequest(BaseModel):
    url: str
    output_format: str = "mp4"
    quality: str = "best"

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^https?://", v, re.IGNORECASE):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("output_format")
    @classmethod
    def _validate_format(cls, v: str) -> str:
        v = (v or "mp4").strip().lower()
        if v not in OUTPUT_FORMATS:
            raise ValueError(f"output_format must be one of {OUTPUT_FORMATS}")
        return v

    @field_validator("quality")
    @classmethod
    def _validate_quality(cls, v: str) -> str:
        return (v or "best").strip().lower()


def _public_item(it: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "index": it["index"],
        "title": it["title"],
        "status": it["status"],
        "stage": it["stage"],
        "progress": it["progress"],
        "error": it["error"],
        "ready": it["status"] == "completed" and bool(it["filepath"]),
    }


def _public_job(job: Dict[str, Any]) -> Dict[str, Any]:
    items = job["items"]
    done = sum(1 for it in items if it["status"] == "completed")
    return {
        "id": job["id"],
        "url": job["url"],
        "output_format": job.get("output_format", "mp4"),
        "quality": job.get("quality", "best"),
        "type": job["type"],
        "status": job["status"],
        "title": job["title"],
        "error": job["error"],
        "created_at": job["created_at"],
        "total": len(items),
        "completed": done,
        "items": [_public_item(it) for it in items],
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/capabilities")
async def capabilities():
    """Frontend uses this to warn when FFmpeg is missing."""
    return {
        "ffmpeg": ffmpeg_available(),
        "formats": list(OUTPUT_FORMATS),
        "video_qualities": list(VIDEO_QUALITIES.keys()),
        "audio_qualities": list(AUDIO_QUALITIES.keys()),
    }


@app.post("/jobs", status_code=202)
async def submit_job(payload: JobRequest, background_tasks: BackgroundTasks):
    job_id = create_job(payload.url, payload.output_format, payload.quality)
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs")
async def list_jobs():
    jobs = sorted(all_jobs(), key=lambda j: j["created_at"], reverse=True)
    return {"jobs": [_public_job(j) for j in jobs]}


@app.get("/jobs/{job_id}")
async def job_detail(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _public_job(job)


@app.delete("/jobs/{job_id}")
async def remove_job(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] == "processing":
        raise HTTPException(status_code=409, detail="Job is still processing")
    _remove_job_files(job_id)
    delete_job(job_id)
    return {"ok": True}


@app.delete("/jobs")
async def clear_finished():
    removed = 0
    for job in all_jobs():
        if job["status"] in ("completed", "partial", "failed"):
            _remove_job_files(job["id"])
            delete_job(job["id"])
            removed += 1
    return {"removed": removed}


@app.get("/download/{job_id}/{item_index}")
async def download_item(job_id: str, item_index: int):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    it = next((x for x in job["items"] if x["index"] == item_index), None)
    if it is None:
        raise HTTPException(status_code=404, detail="Item not found")
    if it["status"] != "completed" or not it["filepath"]:
        raise HTTPException(status_code=409, detail="File is not ready yet")
    path = Path(it["filepath"])
    if not path.exists():
        raise HTTPException(status_code=410, detail="File no longer available")
    name = it["filename"] or path.name
    return FileResponse(path, media_type=_media_type(name), filename=name)


def _build_zip(job: Dict[str, Any], items: List[Dict[str, Any]]) -> str:
    fd, tmp = tempfile.mkstemp(prefix=f"{job['id']}-", suffix=".zip", dir=DOWNLOAD_DIR)
    os.close(fd)  # we only need the path; ZipFile reopens it
    used: set[str] = set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        for it in items:
            src = Path(it["filepath"])
            name = it["filename"] or src.name
            arc = f"{it['index'] + 1:03d} - {name}"
            n, base = 1, arc
            while arc in used:
                arc = f"{base} ({n})"
                n += 1
            used.add(arc)
            zf.write(src, arcname=arc)
    return tmp


@app.get("/download/{job_id}")
async def download_job(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    completed = [
        it for it in job["items"]
        if it["status"] == "completed" and it["filepath"] and Path(it["filepath"]).exists()
    ]
    if not completed:
        raise HTTPException(status_code=409, detail="No files are ready yet")

    # Single file -> serve directly.
    if len(completed) == 1 and len(job["items"]) == 1:
        it = completed[0]
        path = Path(it["filepath"])
        name = it["filename"] or path.name
        return FileResponse(path, media_type=_media_type(name), filename=name)

    # Multiple -> bundle a ZIP (temp file, deleted after streaming).
    zip_path = _build_zip(job, completed)
    zip_name = _safe_filename(job["title"] or "videos", "zip")
    return FileResponse(
        zip_path, media_type="application/zip", filename=zip_name,
        background=BackgroundTask(_safe_unlink, zip_path),
    )


if __name__ == "__main__":
    import uvicorn
    # Single process only (in-memory state + one worker). Do NOT use --workers.
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
