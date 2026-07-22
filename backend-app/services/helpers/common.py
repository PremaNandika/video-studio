"""General helpers — used by 2+ route modules.

Per the user's rule (2026-07-20), "common" is the name for
helpers used across multiple subsystems. The same directory
layout as ``library.py`` (subsystem-specific helpers), but
this file is for path validation, soft-delete, and other
utilities that don't belong to any one tab.

WHAT'S HERE:
  - safe_output_path  — validate + resolve a repo-relative
                        .mp4 path under output/ (used by
                        api_output_to_desktop, api_output_delete,
                        api_output_rename — all B5)
  - soft_delete       — move a file/folder to .trash with
                        restore metadata (used by api_output_delete)
  - read_json         — safe JSON loader (None on missing/bad).
                        Routes-facing home for the loader that
                        captions/clone/dubsync all need. (B11 extract:
                        was a local ``_read_json`` copy in captions.py
                        and clone.py.) services/spend.py keeps its own
                        copy — services stay self-contained (Rule 5.1),
                        so this is the helper for the ROUTE layer.
  - ffprobe           — resolve the ffprobe exe from an ffmpeg bin dir
                        (Gyan path if present, else the bare name).
                        Routes-facing home for the resolver that was
                        inline in subtitles.py + a local ``_ffprobe``
                        in clone.py. (B11 extract.) Mirrors server.py's
                        ``ff_tool`` / ``ffmpeg_exe``.

server.py is unchanged. The helpers stay at their original
lines until the relevant subsystems are retired. Rule 16.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from flask import abort

# Video extensions accepted by safe_video_path (server.py's QC_VIDEO_EXTS).
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


def safe_video_path(rel: str, autovsl_root: Path) -> Path:
    """Resolve a repo-relative path, requiring a video inside the data root.

    Promoted here from services/helpers/qc.py in B13 S2 — QC was its 1st
    consumer, routes/files.py's /api/edit is the 2nd (a different
    subsystem), which is the cross-subsystem promotion trigger. Route-only
    (uses ``abort``). Mirrors server.py L1816-1822 (takes ``autovsl_root``
    explicitly rather than reading the module-level ROOT).
    """
    target = (autovsl_root / rel.replace("\\", "/")).resolve()
    if (
        not str(target).startswith(str(autovsl_root))
        or target.suffix.lower() not in VIDEO_EXTS
    ):
        abort(400, "path must be a video inside the repo")
    if not target.is_file():
        abort(404, "video not found")
    return target


def read_json(path: Path):
    """Safe JSON loader — returns None on any error (missing/bad).

    The routes-facing copy. Mirrors server.py's read_json() at L1613
    (the behavior every legacy caller relied on). ``services/spend.py``
    keeps its own identical loader so the spend service has no
    dependency on the helpers layer (Rule 5.1).
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def ffprobe(ffmpeg_bin: Path) -> str:
    """Resolve the ffprobe executable from an ffmpeg bin directory.

    Returns ``<ffmpeg_bin>/ffprobe.exe`` if that file exists, else the
    bare ``"ffprobe"`` name (so the call still works when ffprobe is
    already on PATH). Mirrors server.py's ``ff_tool("ffprobe")`` /
    ``ffmpeg_exe("ffprobe")`` (L1811 / L770). ``ffmpeg_bin`` is a Path
    the caller reads from ``app.config["FFMPEG_BIN"]`` in request
    context (or receives in a worker's paths bundle).
    """
    exe = Path(ffmpeg_bin) / "ffprobe.exe"
    return str(exe) if exe.is_file() else "ffprobe"


def ffmpeg(ffmpeg_bin: Path) -> str:
    """Resolve the ffmpeg executable from an ffmpeg bin directory.

    Sibling of ``ffprobe`` above: ``<ffmpeg_bin>/ffmpeg.exe`` if present,
    else the bare ``"ffmpeg"`` name. Mirrors server.py's
    ``ff_tool("ffmpeg")`` / ``ffmpeg_exe("ffmpeg")``. Added in B14 (QC
    extracts frames with ffmpeg); shares the resolver with ffprobe.
    """
    exe = Path(ffmpeg_bin) / "ffmpeg.exe"
    return str(exe) if exe.is_file() else "ffmpeg"


def safe_output_path(rel: str) -> Path:
    """Resolve a repo-relative path, requiring an .mp4 inside output/.

    Pure relocation of server.py L280-285. Used by the output
    routes to validate user-supplied paths before reading/moving
    them. Aborts with 400 if the path escapes output/ or isn't
    an .mp4.

    Reads AUTOVSL_ROOT from app.config at call time.
    """
    from flask import current_app  # lazy: only needed when called

    autovsl = current_app.config["AUTOVSL_ROOT"]
    target = (autovsl / rel.replace("\\", "/")).resolve()
    if not str(target).startswith(str(autovsl / "output")) or target.suffix != ".mp4":
        abort(400, "path must be an .mp4 under output/")
    return target


def soft_delete(target: Path, label: str) -> str:
    """Move a file/folder into .trash (recording where it came
    from, for restore).

    Pure relocation of server.py L257-266. Returns the .trash-relative
    path of the deleted item (e.g. ".trash/render-foo-20260720-153012").

    Reads AUTOVSL_ROOT from app.config at call time. The trash dir
    and its index are derived from that root.
    """
    from flask import current_app  # lazy

    autovsl = current_app.config["AUTOVSL_ROOT"]
    trash = autovsl / ".trash"
    trash.mkdir(exist_ok=True)
    original = str(target.relative_to(autovsl)).replace("\\", "/")
    name = f"{label}-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(target), str(trash / name))
    idx = read_json(trash / "index.json") or {}
    idx[name] = {"original": original, "deleted": time.time()}
    (trash / "index.json").write_text(json.dumps(idx, indent=1), encoding="utf-8")
    return f".trash/{name}"
