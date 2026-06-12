"""
Local Video Downloader — FastAPI backend (queue + playlists).

Highlights:
  * Bind strictly to 127.0.0.1 (never reachable over LAN / internet).
  * A single background worker thread processes a FIFO queue, so blocking
    yt-dlp/FFmpeg work never touches the event loop and only ONE download
    runs at a time (system-friendly) while you can queue as many as you like.
  * A "job" = one submitted URL. A job has one or more "items" (videos).
      - single video  -> 1 item
      - playlist       -> N items (capped by MAX_PLAYLIST_ITEMS)
  * Every item is downloadable as its own MP4; multi-item jobs also offer a
    "download all" ZIP.
  * Files persist in downloads/<job_id>/ until you remove the job (trash
    button / DELETE) or restart the app (startup sweep). Generated ZIPs are
    temporary and deleted after they finish streaming.
"""

from __future__ import annotations

import copy
import logging
import os
import queue
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yt_dlp
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

FORMAT_SELECTOR = (
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s"
)
logger = logging.getLogger("downloader")


# ---------------------------------------------------------------------------
# In-memory, thread-safe job store  +  FIFO work queue
# ---------------------------------------------------------------------------

_jobs: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()
_task_queue: "queue.Queue[str]" = queue.Queue()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(url: str) -> str:
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
            "type": None,            # video | playlist
            "status": "queued",      # queued | processing | completed | partial | failed
            "title": None,
            "error": None,
            "items": [],             # list of item dicts
            "created_at": _now(),
            "updated_at": _now(),
        }
    _task_queue.put(job_id)
    logger.info("Queued job %s (%s)", job_id, url)
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


def _pick_output(paths: List[Path]) -> Optional[Path]:
    real = [
        p for p in paths
        if p.is_file() and p.suffix not in (".part", ".ytdl") and ".part-" not in p.name
    ]
    if not real:
        return None
    mp4s = [p for p in real if p.suffix.lower() == ".mp4"]
    return max(mp4s or real, key=lambda p: p.stat().st_size)


def _find_named_file(job_dir: Path, stem: str) -> Optional[Path]:
    return _pick_output(list(job_dir.glob(f"{stem}.*")))


def _find_indexed_file(job_dir: Path, n: int) -> Optional[Path]:
    return _pick_output(list(job_dir.glob(f"{n:03d}.*")))


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
            update_item(job_id, i, stage="merging")
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
        _download_playlist(job_id, job_dir, url, meta)
    else:
        _download_single(job_id, job_dir, url, meta)

    _finalize(job_id)


def _download_single(job_id: str, job_dir: Path, url: str, meta: Dict[str, Any]) -> None:
    update_job(job_id, type="video", title=meta["title"])
    set_items(job_id, [_new_item(0, meta["title"])])

    progress_hook, pp_hook = _make_hooks(job_id, lambda d: 0)
    opts = {
        "format": FORMAT_SELECTOR,
        "merge_output_format": "mp4",
        "outtmpl": str(job_dir / "video.%(ext)s"),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        title = info.get("title") or meta["title"]
        produced = _find_named_file(job_dir, "video")
        if produced is None:
            raise RuntimeError("Download finished but no output file was found.")
        update_item(
            job_id, 0, status="completed", stage="done", progress=100.0,
            title=title, filepath=str(produced), filename=_safe_filename(title, "mp4"),
        )
        logger.info("Job %s item 0 -> %s", job_id, produced.name)
    except Exception as exc:
        logger.exception("Single download failed for %s", url)
        update_item(job_id, 0, status="failed", stage="error", error=str(exc))


def _download_playlist(job_id: str, job_dir: Path, url: str, meta: Dict[str, Any]) -> None:
    entries = meta["entries"]
    update_job(job_id, type="playlist", title=meta["title"])
    set_items(job_id, [
        _new_item(i, e.get("title") or f"Video {i + 1}") for i, e in enumerate(entries)
    ])

    def resolver(d: Dict[str, Any]) -> Optional[int]:
        idx = (d.get("info_dict") or {}).get("playlist_index")
        return (idx - 1) if isinstance(idx, int) else None

    progress_hook, pp_hook = _make_hooks(job_id, resolver)
    opts = {
        "format": FORMAT_SELECTOR,
        "merge_output_format": "mp4",
        "outtmpl": str(job_dir / "%(playlist_index)03d.%(ext)s"),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "ignoreerrors": True,          # skip a bad video, keep going
        "quiet": True,
        "no_warnings": True,
        "playlistend": MAX_PLAYLIST_ITEMS,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception:
        logger.exception("Playlist download error for %s", url)

    # Reconcile produced files with items (file mapping is by index = source of truth).
    job = get_job(job_id)
    for it in job["items"]:
        i = it["index"]
        produced = _find_indexed_file(job_dir, i + 1)
        if produced is not None:
            update_item(
                job_id, i, status="completed", stage="done", progress=100.0,
                filepath=str(produced), filename=_safe_filename(it["title"], "mp4"),
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
    logger.info("Ready on http://%s:%s", HOST, PORT)
    yield


app = FastAPI(title="Local Video Downloader", docs_url=None, redoc_url=None, lifespan=lifespan)


class JobRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^https?://", v, re.IGNORECASE):
            raise ValueError("URL must start with http:// or https://")
        return v


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


@app.post("/jobs", status_code=202)
async def submit_job(payload: JobRequest, background_tasks: BackgroundTasks):
    job_id = create_job(payload.url)
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
    return FileResponse(path, media_type="video/mp4", filename=it["filename"] or path.name)


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
        return FileResponse(path, media_type="video/mp4", filename=it["filename"] or path.name)

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
