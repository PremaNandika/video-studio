"""Library route module — the single GET /api/overview endpoint.

The "Library tab" backend. One route, 6 sources of state combined
into a single response the frontend renders as the unified project
index.

Routes owned by this module:
    GET /api/overview  — products + vsls + banks + outputs +
                          uploads + trash + research + fal_spend,
                          all in one JSON response

The aggregation logic is in ``services/helpers/library.py`` (5
helpers: ``uploads_state``, ``dub_versions``, ``vsl_state``,
``trash_state``, ``upload_active_job``). This module is a thin
wrapper that calls them and combines the result with the spend
ledger.

DEPENDENCIES:
  - services.helpers.library: uploads_state, vsl_state, trash_state
  - services.spend: SpendLedger (for fal_spend total)
  - services.spend: read_json (used by helpers, not directly here)

WHAT IS NOT HERE (deferred to other blueprints):
  - GET /api/creator/library — Ads Factory blueprint (B6)
  - GET /api/bank/<name>     — Banks editor (not in plan, deferred)
  - GET /api/file            — Text file viewer (not in plan, deferred)
  - GET /media/<path>        — Media file server (not in plan, deferred)

These are technically grouped with "library" in the original
plan but they serve other tabs in the UI. Moving them with B4
would inflate the diff and create cross-cutting routes in one
blueprint.

server.py is unchanged. The route, the 5 helpers, and the
constants they read stay at their original lines until the
entire library subsystem is retired. Rule 16.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from services.helpers.library import trash_state, uploads_state, vsl_state
from services.spend import SpendLedger, read_json


# Pure relocation of server.py L90 — the set of bank files the
# Library tab counts for the "banks" summary. Used by api_overview
# to build the `banks` dict (one count per bank).
_BANK_FILES = ("hooks", "angles", "pain-points", "broll")

# Pure relocation of server.py L1689-1703 — these are the
# "on_desktop" checks. The constants (DESKTOP_VSLS, READY_DIR)
# come from app.config in create_app().
library_bp = Blueprint("library", __name__)


@library_bp.get("/api/overview")
def api_overview():
    """The unified project index — every tab's home view in one shot.

    Returns:
        {
          "products": [...],        # VSL product manifests
          "vsls":      [...],        # VSL timelines + segments
          "banks":     {<name>: <count>},  # JSONL bank line counts
          "outputs":   [...],        # All .mp4 outputs (dubs + VSLs)
          "research":  [...],        # .md paths under research/
          "uploads":   [...],        # Per-upload pipeline status
          "trash":     [...],        # .trash items
          "fal_spend": <float>,      # Running fal.ai total
        }
    """
    autovsl = current_app.config["AUTOVSL_ROOT"]
    desktop_vsls = current_app.config["DESKTOP_VSLS"]
    ready_dir = current_app.config["READY_DIR"]

    # --- products (VSL product manifests) ---
    products = []
    for mf in sorted((autovsl / "products").glob("*/manifest.json")):
        m = read_json(mf) or {}
        slug = m.get("slug", mf.parent.name)
        stages = []
        for key, st in (m.get("stages") or {}).items():
            stages.append({"key": key, "status": (st or {}).get("status", "?")})
        scripts = [
            {"file": s.get("file"), "status": s.get("status", "?"), "angle": s.get("angle")}
            for s in ((m.get("stages") or {}).get("4_scripts", {}) or {}).get("scripts", [])
        ]
        products.append({
            "slug": slug, "product": m.get("product", slug),
            "stage": m.get("stage"), "stages": stages, "scripts": scripts,
        })

    # --- vsls ---
    # Defensive: server.py L1672 calls iterdir() unguarded, which 500s
    # on dev machines where autoVSL/ has no vsls/ subdir. This is a
    # pre-existing bug in server.py; the new app fixes it because
    # the user hit it in Postman. Same fix would land in server.py
    # separately (per Rule 16, we don't touch server.py here).
    vsls = []
    vsls_dir = autovsl / "vsls"
    if vsls_dir.is_dir():
        vsls = [vsl_state(p.name) for p in sorted(vsls_dir.iterdir())
                if (p / "timeline.json").is_file()]

    # --- banks (JSONL line counts) ---
    banks = {}
    for bank in _BANK_FILES:
        path = autovsl / "banks" / f"{bank}.jsonl"
        n = 0
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                n = sum(1 for line in f if line.strip())
        banks[bank] = n

    # --- outputs (every .mp4 in autoVSL/output) ---
    outputs = []
    for p in sorted((autovsl / "output").glob("*.mp4")):
        outputs.append({
            "kind": "vsl", "group": None, "name": p.name,
            "path": f"output/{p.name}",
            "size": p.stat().st_size, "mtime": p.stat().st_mtime,
            "current": True, "on_desktop": (desktop_vsls / p.name).is_file(),
        })
    swap_work = autovsl / "output" / "script-swap"
    if swap_work.is_dir():
        for d in sorted(swap_work.iterdir()):
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
                current = f.name == "final.mp4"
                outputs.append({
                    "kind": "dub", "group": d.name, "name": f.name,
                    "path": f"output/script-swap/{d.name}/{f.name}",
                    "size": f.stat().st_size, "mtime": f.stat().st_mtime,
                    "current": current,
                    "on_desktop": (ready_dir / f"{d.name}-ready.mp4").is_file() if current
                                  else (desktop_vsls / f"{d.name}-{f.name}").is_file(),
                })
    edits_dir = autovsl / "output" / "edits"
    if edits_dir.is_dir():
        for p in sorted(edits_dir.glob("*.mp4")):
            outputs.append({"kind": "vsl", "group": None, "name": "✂ " + p.name,
                            "path": f"output/edits/{p.name}", "size": p.stat().st_size,
                            "mtime": p.stat().st_mtime, "current": True,
                            "on_desktop": (desktop_vsls / p.name).is_file()})

    # --- research (markdown files under autoVSL/research, excluding "raw") ---
    research = sorted(
        str(p.relative_to(autovsl)).replace("\\", "/")
        for p in (autovsl / "research").rglob("*.md")
        if "raw" not in p.parts
    )

    # --- fal_spend total (read from SpendLedger) ---
    ledger: SpendLedger = current_app.config["SPEND_LEDGER"]
    fal_spend = round(float(ledger.load().get("total", 0.0)), 2)

    return jsonify({
        "products": products,
        "vsls": vsls,
        "banks": banks,
        "outputs": outputs,
        "research": research,
        "uploads": uploads_state(),
        "trash": trash_state(),
        "fal_spend": fal_spend,
    })


def register_library(app) -> None:
    """Register the library route module on ``app``."""
    app.register_blueprint(library_bp)
