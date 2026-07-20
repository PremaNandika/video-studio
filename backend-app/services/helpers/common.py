"""General helpers — used by 2+ route modules.

Per the user's rule (2026-07-20), "common" is the name for
helpers used across multiple subsystems. The same directory
layout as ``library.py`` (subsystem-specific helpers), but
this file is for path validation, soft-delete, and other
utilities that don't belong to any one tab.

WHAT'S HERE (B5 push, 2026-07-20):
  - safe_output_path  — validate + resolve a repo-relative
                        .mp4 path under output/ (used by
                        api_output_to_desktop, api_output_delete,
                        api_output_rename — all B5)
  - soft_delete       — move a file/folder to .trash with
                        restore metadata (used by api_output_delete;
                        will be used by dubsync + clone later)

WHAT IS NOT HERE (deferred to other blueprints / helpers):
  - generic read_json — already in services/spend.py (inlined
    during S3; can move here when a 3rd service needs it)
  - generic file-glob helpers — none yet; will add when a
    2nd subsystem needs them

server.py is unchanged. The 2 helpers stay at their original
lines until the entire output-manipulation subsystem is
retired. Rule 16.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from flask import abort

from services.spend import read_json  # safe JSON loader, no exception on bad/missing


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
