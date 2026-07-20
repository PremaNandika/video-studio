"""Exports route module — 5 routes that own the output/deliverable
filesystem manipulation.

Routes owned by this module:
    GET  /api/exports              — every finished deliverable in one list
    POST /api/exports/send         — copy a deliverable to Desktop exports dir
                                     (flat, unique name, never clobbers)
    POST /api/output-to-desktop    — copy a single .mp4 to the Desktop VSLs dir
    DELETE /api/output             — move an output .mp4 to .trash (soft delete)
    POST /api/output-rename        — rename an output .mp4 (with secure_filename)

The HTML page route ``GET /exports`` is frontend and is NOT in this
module (B2 was skipped — frontend is out of scope per the user).

DEPENDENCIES:
  - services.helpers.exports:    _export_item (per-item dict builder)
  - services.helpers.common:     safe_output_path, soft_delete
  - services.workdir:            DubWorkdir (first real consumer — S5
                                 class; reads final.mp4 + final-captioned.mp4)
  - shutil + werkzeug.utils.secure_filename: for the copy + rename

WHAT IS NOT HERE:
  - GET /exports (HTML page) — frontend, deferred
  - /api/trash/restore + /api/trash/empty — could go here (B5
    "Exports" feels right for trash management) or in a future
    trash blueprint. Deferred to keep this commit focused.
  - safe_output_path is in services/helpers/common.py (general
    helper, used by 2+ routes; matches user's "common is fine for
    general" rule).

server.py is unchanged. All 5 routes, _export_item, safe_output_path,
soft_delete, and the SUBSTUDIO_OUT constant stay at their original
lines until the entire output subsystem is retired. Rule 16.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request
from werkzeug.utils import secure_filename

from services.helpers.common import safe_output_path, soft_delete
from services.helpers.exports import _export_item
from services.workdir import DubWorkdir


exports_bp = Blueprint("exports", __name__)


@exports_bp.get("/api/exports")
def api_exports():
    """Every finished deliverable across the pipeline, one list.

    Returns: {"items": [<sorted by mtime desc>], "exports_dir": <str>}

    Iterates 4 subdirs of autoVSL/output/:
      - output/*.mp4                → kind="vsl"
      - output/edits/*.mp4          → kind="edit"  (only if dir exists)
      - output/script-swap/<d>/final.mp4        → kind="dub"
      - output/script-swap/<d>/final-captioned.mp4 → kind="dub-captioned"
      - output/subtitle-studio/<d>/captioned.mp4 → kind="captioned"

    Pure relocation of server.py L3146-3170, with the dub workdir
    access rewritten to use DubWorkdir (the only behavior change is
    structural — the resulting paths are identical).

    Defensive: every iterdir() is guarded by is_dir() because on
    a fresh dev box none of these exist. server.py L3146-3170 has
    the same bug (B4 fix pattern).
    """
    items = []
    autovsl = current_app.config["AUTOVSL_ROOT"]
    exports_dir = current_app.config["EXPORTS_DIR"]
    out = autovsl / "output"

    # 1) output/*.mp4 (VSL renders at the top level)
    if out.is_dir():
        for p in sorted(out.glob("*.mp4")):
            items.append(_export_item(p, "vsl", p.stem))

    # 2) output/edits/*.mp4 (VSL edits)
    edits = out / "edits"
    if edits.is_dir():
        for p in sorted(edits.glob("*.mp4")):
            items.append(_export_item(p, "edit", p.stem))

    # 3) output/script-swap/<d>/{final,final-captioned}.mp4 (dubs)
    #    Uses DubWorkdir for the first time in a real route.
    swap_work = autovsl / "output" / "script-swap"
    if swap_work.is_dir():
        for d in sorted(swap_work.iterdir()):
            if not d.is_dir():
                continue
            wd = DubWorkdir(autovsl, d.name)  # ← S5 class in action
            if wd.final.is_file():
                items.append(_export_item(wd.final, "dub", d.name))
            if wd.final_captioned.is_file():
                items.append(_export_item(wd.final_captioned, "dub-captioned",
                                          f"{d.name} (captioned)"))

    # 4) output/subtitle-studio/<d>/captioned.mp4 (subtitle-studio outputs)
    substudio_out = current_app.config["SUBSTUDIO_OUT"]
    if substudio_out.is_dir():
        for d in sorted(substudio_out.iterdir()):
            if not d.is_dir():
                continue
            cap = d / "captioned.mp4"
            if cap.is_file():
                items.append(_export_item(cap, "captioned",
                                          f"{d.name} (subtitle studio)"))

    items.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"items": items, "exports_dir": str(exports_dir)})


@exports_bp.post("/api/exports/send")
def api_exports_send():
    """Copy a deliverable into the ONE Desktop exports folder
    (flat, unique name, never clobbers an earlier export).

    Pure relocation of server.py L3173-3197. Two kinds supported:
      - "captioned":  body has "kind":"captioned" + "stem" → looks up
                     SUBSTUDIO_OUT / stem / captioned.mp4
      - any other:   body has "path" (repo-relative) → looks up
                     the file under ROOT/output/, requires .mp4.
                     Flattens dub paths: output/script-swap/<name>/final.mp4
                     → <name>-final.mp4
    """
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    exports_dir = current_app.config["EXPORTS_DIR"]
    substudio_out = current_app.config["SUBSTUDIO_OUT"]
    kind = body.get("kind")
    if kind == "captioned":
        stem = Path(body.get("stem") or "").name
        src = substudio_out / stem / "captioned.mp4"
        flat = f"{stem}-captioned.mp4"
    else:
        rel = (body.get("path") or "").replace("\\", "/")
        src = (autovsl / rel).resolve()
        if not str(src).startswith(str(autovsl / "output")) or src.suffix != ".mp4":
            abort(400, "path must be an .mp4 under output/")
        parts = src.relative_to(autovsl / "output").parts
        flat = src.name if len(parts) == 1 else f"{parts[-2]}-{src.name}"
    if not src.is_file():
        abort(404, "deliverable not found")
    exports_dir.mkdir(parents=True, exist_ok=True)
    dest, n = exports_dir / flat, 2
    while dest.exists():                       # never clobber an earlier export
        dest = exports_dir / f"{Path(flat).stem}-{n}.mp4"
        n += 1
    shutil.copy2(src, dest)
    return jsonify({"saved_to": str(dest)})


@exports_bp.post("/api/output-to-desktop")
def api_output_to_desktop():
    """Copy a single .mp4 to the Desktop VSLs dir (with auto-flatten).

    Pure relocation of server.py L1181-1192. Flattens dub paths:
    output/script-swap/<name>/final.mp4 → <name>-final.mp4
    """
    body = request.get_json(force=True)
    src = safe_output_path(body.get("path") or f"output/{Path(body.get('name') or '').name}")
    if not src.is_file():
        abort(404, "output video not found")
    rel_parts = src.relative_to(current_app.config["AUTOVSL_ROOT"] / "output").parts
    flat = src.name if len(rel_parts) == 1 else f"{rel_parts[-2]}-{src.name}"
    desktop_vsls = current_app.config["DESKTOP_VSLS"]
    desktop_vsls.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, desktop_vsls / flat)
    return jsonify({"saved_to": str(desktop_vsls / flat)})


@exports_bp.delete("/api/output")
def api_output_delete():
    """Move an output .mp4 to .trash (soft delete, restore-able).

    Pure relocation of server.py L1195-1201.
    """
    target = safe_output_path(request.args.get("path") or "")
    if not target.is_file():
        abort(404)
    moved = soft_delete(target, f"render-{target.stem}")
    return jsonify({"moved_to": moved})


@exports_bp.post("/api/output-rename")
def api_output_rename():
    """Rename an output .mp4. Uses werkzeug's secure_filename to
    sanitize the new name. Aborts 409 if a file with the new name
    already exists.

    Pure relocation of server.py L1204-1219.
    """
    body = request.get_json(force=True)
    target = safe_output_path(body.get("path") or "")
    if not target.is_file():
        abort(404)
    new_name = secure_filename(Path(body.get("new_name") or "").name)
    if not new_name:
        abort(400, "bad name")
    if not new_name.endswith(".mp4"):
        new_name += ".mp4"
    dest = target.with_name(new_name)
    if dest.exists():
        abort(409, "a video with that name already exists")
    target.rename(dest)
    return jsonify({"renamed_to": str(dest.relative_to(current_app.config["AUTOVSL_ROOT"])).replace("\\", "/")})


def register_exports(app) -> None:
    """Register the exports route module on ``app``."""
    app.register_blueprint(exports_bp)
