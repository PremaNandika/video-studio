"""Ads Factory helpers — shared constants + spawn/validation helpers.

Subsystem-helper module for ``routes/ads_factory.py`` (the Ads Factory
tab: the /api/run production actions [B13 S3] + the creator/product/VSL
CRUD [B13 S4]). Holds everything that isn't a route handler or the
``build_vsl_worker`` thread target, so the route module stays lean
(moved here at the user's request, 2026-07-22).

Two flavors, same as ``services/helpers/library.py`` + ``qc.py``:
  - ``valid_slug`` / ``library_meta`` are pure (no ``current_app``).
  - ``_spawn`` / ``_spawn_shell`` / ``_check_slug`` read ``current_app``
    at call time — they only run inside request handlers, so
    ``current_app`` is bound (identical to library.py's helpers).

Names are kept exactly as they were in the route module (including the
leading underscores on the spawn/validation helpers) so the wholesale
relocation changes no call sites — the routes import and call them
unchanged.

server.py is unchanged (Rule 16).
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from flask import abort, current_app, jsonify

from services.helpers.common import read_json

# fal.ai video models + their per-clip cost (server.py L176-180). Used to
# validate generate-video's --model.
VIDEO_MODELS = {
    "seedance-480p": 0.05,
    "seedance-720p": 0.11,
    "seedance-1080p": 0.24,
    "wan-5b-720p": 0.15,
    "wan-480p": 0.20,
    "hailuo-768p": 0.27,
    "wan-580p": 0.30,
    "kling-turbo": 0.35,
    "wan-720p": 0.40,
}

# The residual shell actions (subset of server.py's ACTIONS, L159-165) —
# dub/caption/recaption already moved (B7/B8); transcribe is special (in routes).
_RUN_ACTIONS = {
    "check-media": {"script": "scripts/check-media.sh", "label": "Check media"},
    "assemble": {"script": "scripts/assemble-vsl.sh", "label": "Assemble VSL"},
    "generate-vo": {"script": "scripts/generate-vo.sh", "label": "Generate VO (free)"},
    "generate-video": {
        "script": "scripts/generate-video.sh",
        "label": "Generate video (fal.ai, costs $)",
    },
    "print-prompts": {"script": "scripts/print-prompts.sh", "label": "Print prompts"},
    "list-models": {
        "script": "scripts/generate-video.sh",
        "label": "List video models",
    },
}

# Product-scaffold constants (server.py L181-183). Used by POST /api/product.
PRODUCT_DIRS = ["avatars", "angles", "scripts", "shot-lists", "stories"]
MANIFEST_STAGES = [
    "1_product_intake",
    "2_avatar_research",
    "3_angles",
    "4_scripts",
    "5_shot_list",
    "6_generation",
]
# Uploads the Creator page lists (server.py L2187).
CREATOR_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
# Guards writes to output/library.json (server.py L2186).
library_lock = threading.Lock()


def _check_slug(slug: str) -> str:
    """The monolith's slug guard (server.py L437): a slug lands on a shell
    command line, so it must be plain alnum + - + _."""
    if slug and not slug.replace("-", "").replace("_", "").isalnum():
        abort(400, "bad slug")
    return slug


def _spawn(action: str, slug: str, label: str, cmd: list[str], gpu: bool | None = None):
    """Create the job record + spawn ``runner.run`` on a daemon thread.

    Mirrors the monolith's job-dict shape. ``gpu`` left as None means the
    runner auto-detects from GPU_MARKERS (transcribe.py), matching
    server.py which set no ``gpu`` key for these actions.

    Runs in request context (reads ``current_app``); ``jobs`` is imported
    locally to keep the import surface explicit.
    """
    from services.jobs import jobs

    job_id = uuid.uuid4().hex[:8]
    job = {
        "id": job_id,
        "action": action,
        "slug": slug,
        "label": label,
        "status": "running",
        "lines": [],
        "returncode": None,
        "started": time.time(),
        "ended": None,
    }
    if gpu is not None:
        job["gpu"] = gpu
    jobs[job_id] = job
    runner = current_app.config["JOB_RUNNER"]
    threading.Thread(target=runner.run, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id})


def _spawn_shell(action_key: str, slug: str, tail: list[str] | None = None):
    """Build ``[BASH, <script>, slug|'fairy-flame', *tail]`` and spawn it.
    Mirrors server.py L565-594 for the generic shell actions."""
    autovsl = current_app.config["AUTOVSL_ROOT"]
    action = _RUN_ACTIONS[action_key]
    cmd = [
        current_app.config["BASH"],
        str(autovsl / action["script"]),
        slug or "fairy-flame",
    ] + (tail or [])
    label = f"{action['label']} — {slug}" if slug else action["label"]
    return _spawn(action_key, slug, label, cmd)


def valid_slug(slug: str) -> bool:
    """Strict slug check (server.py L1117): non-empty, alnum + - + _, and
    no path parts. Used by product-create + build-vsl. Stricter than the
    shell ``_check_slug`` (this also requires ``slug == Path(slug).name``)."""
    return (
        bool(slug)
        and slug.replace("-", "").replace("_", "").isalnum()
        and slug == Path(slug).name
    )


def library_meta(autovsl: Path) -> dict:
    """Read output/library.json (per-upload title/tags/approved), {} if absent.
    Pure relocation of server.py L2190-2191; takes ``autovsl`` explicitly."""
    return read_json(autovsl / "output" / "library.json") or {}
