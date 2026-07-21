"""QC helpers — frame extraction, probing, and the reviews store.

Subsystem-helper module (mirrors services/helpers/library.py +
exports.py — the established home for a tab's non-stateful helpers).
Pure move of the QC support functions from video-studio/app/server.py
L1767-1893.

CONFIG-AS-ARGS (the key difference from library.py):
  library.py's helpers read ``current_app`` at call time because they
  only run inside request handlers. QC's helpers are ALSO called from
  ``qc_ai_worker`` (routes/qc.py), which runs on a bare daemon thread
  with NO application context — reading ``current_app`` there raises
  RuntimeError (the B9 lesson). So every helper here takes its config
  as an explicit ``cfg`` dict (or a single path), NEVER current_app.
  The ``ai-review`` route builds the cfg in request context and hands
  it down to the worker, which passes it to these helpers.

  The ``cfg`` dict shape (built by routes.qc._qc_cfg):
    {
      "autovsl_root": Path,   # the data root (= ROOT in server.py)
      "qc_cache":     Path,   # <root>/output/qc/cache
      "qc_reviews":   Path,   # <root>/output/qc/reviews.json
      "ffmpeg":       str,    # resolved ffmpeg command (common.ffmpeg)
      "ffprobe":      str,    # resolved ffprobe command (common.ffprobe)
      "env":          dict,   # subprocess env (job-env factory output)
    }

safe_video_path lives here FOR NOW — QC is its only new-side consumer.
When B13d's /api/edit lands (a 2nd, different subsystem), promote it to
services/helpers/common.py (the cross-subsystem home), same as
read_json / ffprobe / safe_output_path.

server.py is unchanged. These helpers stay at their original lines until
the QC subsystem is retired. Rule 16.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path

from flask import abort

from services.helpers.common import read_json


# Video extensions QC will operate on (server.py L1771).
QC_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}

# Guards writes to the reviews store (server.py L1772). Lives with the
# store I/O it protects (qc_store / qc_save) so both the review route and
# the ai-review worker import one lock.
qc_lock = threading.Lock()


def safe_video_path(rel: str, autovsl_root: Path) -> Path:
    """Resolve a repo-relative path, requiring a video inside the data root.

    Pure relocation of server.py L1816-1822. Takes ``autovsl_root``
    explicitly (server.py read the module-level ROOT). Route-only helper
    (all callers are request handlers), so ``abort`` is safe here.
    """
    target = (autovsl_root / rel.replace("\\", "/")).resolve()
    if not str(target).startswith(str(autovsl_root)) or target.suffix.lower() not in QC_VIDEO_EXTS:
        abort(400, "path must be a video inside the repo")
    if not target.is_file():
        abort(404, "video not found")
    return target


def ffprobe_json(path: Path, cfg: dict) -> dict:
    """Full ffprobe format+streams JSON, or {} on error.

    Pure relocation of server.py L1825-1835. Uses ``cfg["ffprobe"]`` +
    ``cfg["env"]`` instead of ``ff_tool("ffprobe")`` + ``job_env()``.
    """
    r = subprocess.run(
        [cfg["ffprobe"], "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, env=cfg["env"],
    )
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def video_duration(probe: dict) -> float:
    """Duration (seconds) out of an ffprobe dict, 0.0 on error.
    Pure relocation of server.py L1838-1842 (no config needed)."""
    try:
        return float((probe.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def qc_cache_dir(src: Path, tag: str, cfg: dict) -> Path:
    """Per-file, mtime-keyed cache dir for extracted frames.
    Pure relocation of server.py L1845-1848."""
    rel = str(src.relative_to(cfg["autovsl_root"]))
    key = hashlib.md5(f"{rel}|{int(src.stat().st_mtime)}|{tag}".encode()).hexdigest()[:12]
    return cfg["qc_cache"] / key


def extract_spread_frames(src: Path, count: int, cfg: dict) -> list[dict]:
    """Evenly spaced full frames -> [{path, t}], cached per file mtime.
    Pure relocation of server.py L1851-1871."""
    probe = ffprobe_json(src, cfg)
    dur = video_duration(probe)
    if dur <= 0:
        return []
    outdir = qc_cache_dir(src, f"spread{count}", cfg)
    outdir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(count):
        ts = dur * (i + 1) / (count + 1)
        out = outdir / f"f-{i + 1:02d}.jpg"
        if not out.is_file():
            subprocess.run(
                [cfg["ffmpeg"], "-y", "-ss", f"{ts:.3f}", "-i", str(src),
                 "-frames:v", "1", "-q:v", "3", str(out)],
                capture_output=True, timeout=120, env=cfg["env"],
            )
        if out.is_file():
            frames.append({"path": str(out.relative_to(cfg["autovsl_root"])).replace("\\", "/"),
                           "t": round(ts, 2)})
    return frames


def extract_burst_frames(src: Path, at: float, cfg: dict, count: int = 6) -> list[str]:
    """Consecutive frames (fps=8) from ``at`` seconds — mouth-articulation review.
    Pure relocation of server.py L1874-1884."""
    outdir = qc_cache_dir(src, f"burst{count}@{at:.1f}", cfg)
    outdir.mkdir(parents=True, exist_ok=True)
    if not any(outdir.glob("b-*.jpg")):
        subprocess.run(
            [cfg["ffmpeg"], "-y", "-ss", f"{at:.3f}", "-i", str(src),
             "-vf", "fps=8", "-frames:v", str(count), "-q:v", "3", str(outdir / "b-%02d.jpg")],
            capture_output=True, timeout=120, env=cfg["env"],
        )
    return [str(p.relative_to(cfg["autovsl_root"])).replace("\\", "/")
            for p in sorted(outdir.glob("b-*.jpg"))]


def qc_store(cfg: dict) -> dict:
    """Read the reviews store (or {}). Pure relocation of server.py L1887-1888."""
    return read_json(cfg["qc_reviews"]) or {}


def qc_save(store: dict, cfg: dict) -> None:
    """Write the reviews store. Pure relocation of server.py L1891-1893."""
    cfg["qc_reviews"].parent.mkdir(parents=True, exist_ok=True)
    cfg["qc_reviews"].write_text(json.dumps(store, indent=1), encoding="utf-8")
