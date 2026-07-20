"""Scripts route module — read/save the per-workdir edited script.

Routes owned by this module:
    GET  /api/script/<stem>   — return the edited script if it
                                 exists, else the plain transcript,
                                 else empty
    POST /api/script/<stem>   — save the edited script to the
                                 workdir (creates the workdir if
                                 missing)

DEPENDENCIES:
  - services.helpers.transcripts:  transcript_plain_text (fallback
                                    reader for the .json sidecar)
  - services.workdir:              DubWorkdir (path properties for
                                    the workdir + script_edited.txt)

WHAT IS NOT HERE (deferred to other blueprints):
  - /api/run (the dispatcher for transcribe/caption/recaption/dub
    + shell-script actions) — B7 Dubbing owns run_job extraction,
    B8 Captions owns the caption/recaption branches, the shell
    actions stay in server.py until their natural blueprint move.
  - /api/copywrite, /api/edit, /api/agent-note — Claude-driven;
    land in B12 Chat or a future Ads Factory blueprint.
  - /api/transcript-to-product, /api/product, /api/build-vsl,
    /api/research-doc, /api/bank/<name>, /api/creator/* — Ads
    Factory cluster, deferred to B13 (catch-all).

server.py is unchanged. Both routes, transcript_plain_text, and
the SWAP_WORK constant stay at their original lines until the
entire scripts subsystem is retired. Rule 16.
"""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request

from services.helpers.transcripts import transcript_plain_text
from services.workdir import DubWorkdir


scripts_bp = Blueprint("scripts", __name__)


@scripts_bp.get("/api/script/<stem>")
def api_script_get(stem):
    """Return the edited script if it exists, else the plain
    transcript, else empty.

    Response shape: ``{"text": <str>, "source": "edited" |
    "transcript" | "empty"}`` — the frontend uses ``source`` to
    show "you haven't edited this yet, here's the transcript".

    Pure relocation of server.py L746-753. Uses DubWorkdir
    (S5 class) to get the workdir path — first time a B6
    route uses DubWorkdir for a workdir file.
    """
    stem = Path(stem).name   # never trust the URL — same as server.py
    autovsl = current_app.config["AUTOVSL_ROOT"]
    wd = DubWorkdir(autovsl, stem)
    if wd.script_edited.is_file():
        return jsonify({"text": wd.script_edited.read_text(encoding="utf-8"),
                        "source": "edited"})
    text = transcript_plain_text(stem)
    return jsonify({"text": text, "source": "transcript" if text else "empty"})


@scripts_bp.post("/api/script/<stem>")
def api_script_save(stem):
    """Save the edited script to the workdir (creates the workdir
    if missing).

    Pure relocation of server.py L756-765. Writes the script with
    a trailing newline (matches server.py — preserves the original
    file's end-of-file convention).
    """
    stem = Path(stem).name
    text = (request.get_json(force=True).get("text") or "").strip()
    if not text:
        abort(400, "empty script")
    autovsl = current_app.config["AUTOVSL_ROOT"]
    wd = DubWorkdir(autovsl, stem)
    wd.dir.mkdir(parents=True, exist_ok=True)
    wd.script_edited.write_text(text + "\n", encoding="utf-8")
    return jsonify({"saved": f"output/script-swap/{stem}/script-edited.txt",
                    "chars": len(text)})


def register_scripts(app) -> None:
    """Register the scripts route module on ``app``."""
    app.register_blueprint(scripts_bp)
