"""Exports helpers — list, build, send deliverables.

Pure relocation of video-studio/app/server.py L2892-2899
(the ``_export_item`` helper). The aggregation logic for
``GET /api/exports`` lives in the route module because it
calls 3 different subdirs (output/, output/edits/,
output/script-swap/, output/subtitle-studio/) — too much
control flow for a pure helper.

WHAT'S HERE (B5 push, 2026-07-20):
  - _export_item      — build the per-item dict for an exports
                        list (kind/label/mtime/size/path/view/stem)

DubWorkdir USAGE (the moment of truth for S5's class):
  The route module uses DubWorkdir to read the dub workdir's
  ``final.mp4`` and ``final-captioned.mp4`` paths, instead of
  string-concatenating ``(d / "final.mp4")`` like server.py does.
  This is the **first real consumer** of the S5 class — S5
  shipped it as a path-properties-only skeleton, and B5 proves
  the shape is right for actual route use.

server.py is unchanged. _export_item stays at its original line
until the entire exports subsystem is retired. Rule 16.
"""
from __future__ import annotations

from pathlib import Path


def _export_item(p: Path, kind: str, label: str) -> dict:
    """Build the per-item dict for an exports list.

    Pure relocation of server.py L2892-2899.
    """
    from flask import current_app  # lazy
    autovsl = current_app.config["AUTOVSL_ROOT"]
    st = p.stat()
    inside_root = str(p).startswith(str(autovsl))
    rel = str(p.relative_to(autovsl)).replace("\\", "/") if inside_root else None
    return {"kind": kind, "label": label, "mtime": st.st_mtime, "size": st.st_size,
            "path": rel,                                    # repo-rel (actions work on this)
            "view": f"/media/{rel}" if rel else f"/captioned/{p.parent.name}",
            "stem": p.parent.name if not inside_root else p.stem}
