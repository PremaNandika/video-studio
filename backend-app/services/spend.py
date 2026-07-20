"""fal.ai cost tracking — ledger + cost estimator.

Pure move from video-studio/app/server.py lines 189-254. Per
BACKEND-REFACTOR-RULES.md Rule 17, the spend concern is implemented
as a ``SpendLedger`` class because it owns state that persists
across calls: the ``fal_spend.json`` ledger, the in-memory data
dict, the ``cloned_stems`` cache, and the threading lock.

WHAT MOVED (8 spend symbols + 7 inlined helpers):
  From video-studio/app/server.py:
    FAL_SPEND_FILE            L192  (constant)   — class attr
    TTS_RATE_PER_1K           L193  (constant)   — class attr
    MINIMAX_CLONE_FEE         L194  (constant)   — class attr
    LIPSYNC_RATE_PER_SEC      L195  (constant)   — class attr
    spend_lock                L198  (threading.Lock) — class attr
    load_spend()              L201  (function)  — method
    estimate_dub_cost(...)    L205  (function)  — method
    record_spend(...)         L241  (function)  — method

  Inlined helpers (so the module is self-contained per Rule 5.1):
    FFMPEG_BIN        L92   (constant)   — class attr
    ff_tool(name)     L1811 (function)   — module function (no state)
    job_env()         L288  (function)   — module function (no state)
    read_json(path)   L1613 (function)   — module function (no state)
    ffprobe_json(p)   L1825 (function)   — module function (no state)
    video_duration(p) L1838 (function)   — module function (no state)
    autovsl_root      L50   (constant)   — class attr (constructor arg)

LIFECYCLE (per Rule 17 + Rule 8.1):
  - The app factory constructs one ``SpendLedger`` instance per app,
    passing in the three path constants from ``app.config``.
  - The instance is stashed at ``app.config["SPEND_LEDGER"]``.
  - Route handlers retrieve it via ``current_app.config["SPEND_LEDGER"]``.
  - Worker threads (run_dub_job) receive the instance explicitly via
    the calling route's ``cost_ctx`` dict — Rule 8.5: no cross-tree
    imports, pass dependencies explicitly.
  - Tests construct a SpendLedger with paths to a temp dir; no
    Flask app needed (pure unit tests).

server.py is unchanged. The 8 spend symbols and 7 inlined helpers
remain at their original lines until B7 Dubbing wires the new
service and deletes the old copies. See
.hermes/decisions/phase-1-2026-07-20-spend-self-contained.md.
"""

# --- stdlib (per plan §3.1 S3 row + 2 extras for inlined helpers) -------
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path


# --- inlined helpers (no state; module-level is fine) -------------------

def _ffmpeg_bin() -> Path:
    """The Gyan WinGet ffmpeg path (server.py L92)."""
    return (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.2-full_build/bin"
    )


def ff_tool(name: str, ffmpeg_bin: Path) -> str:
    """Return the absolute path to a ffmpeg-family tool, or its bare name
    if the bin dir isn't present (so subprocess can fall back to PATH)."""
    exe = ffmpeg_bin / f"{name}.exe"
    return str(exe) if exe.is_file() else name


def job_env(ffmpeg_bin: Path) -> dict:
    """Build a subprocess env that has ffmpeg on PATH and PYTHONUTF8=1."""
    env = dict(os.environ)
    if ffmpeg_bin.is_dir():
        env["PATH"] = str(ffmpeg_bin) + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    return env


def read_json(path: Path):
    """Safe JSON loader — returns None on any error (missing file, bad JSON)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def ffprobe_json(path: Path, ffmpeg_bin: Path) -> dict:
    """Run ffprobe on `path` and return its JSON output (format + streams)."""
    r = subprocess.run(
        [ff_tool("ffprobe", ffmpeg_bin), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, env=job_env(ffmpeg_bin),
    )
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def video_duration(probe: dict) -> float:
    """Extract the duration (seconds) from an ffprobe JSON dict."""
    try:
        return float((probe.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


# --- the SpendLedger class (Rule 17) -------------------------------------

class SpendLedger:
    """fal.ai cost ledger. Owns the JSON file, the in-memory data,
    the cloned_stems cache, and the threading lock.

    Constructed once per app by the app factory (see
    ``app.py::create_app``). Stored at ``app.config["SPEND_LEDGER"]``.

    Pricing constants are class-level because they are per-app-invariant
    (the same for every app that uses this codebase).
    """

    # Pricing tables — same across all apps; class-level is appropriate.
    TTS_RATE_PER_1K = {"f5": 0.05, "turbo": 0.06, "hd": 0.10, "local": 0.0}   # USD / 1000 chars
    MINIMAX_CLONE_FEE = 1.50                                                   # one-time per voice (turbo/hd)
    LIPSYNC_RATE_PER_SEC = {
        "latentsync": 0.005, "musetalk": 0.005, "veed": 0.0067,
        "standard": 0.05, "pro": 0.10, "none": 0.0, "wav2lip": 0.0,
        "wav2lip-hd": 0.0,
    }  # USD / second (wav2lip* = local, free; musetalk est.)

    def __init__(self, spend_ledger_file: Path, autovsl_root: Path,
                 ffmpeg_bin: Path | None = None) -> None:
        """
        Args:
            spend_ledger_file: path to output/fal_spend.json.
            autovsl_root: the autoVSL data root (Rule 8.1: from app.config).
            ffmpeg_bin: the ffmpeg install dir. Defaults to the
                LOCALAPPDATA/Gyan WinGet path.
        """
        self._file = Path(spend_ledger_file)
        self._swap_work = Path(autovsl_root) / "output" / "script-swap"
        self._ffmpeg_bin = Path(ffmpeg_bin) if ffmpeg_bin is not None else _ffmpeg_bin()
        self._lock = threading.Lock()

    # --- public methods --------------------------------------------------

    def load(self) -> dict:
        """Read the JSON ledger; return the default empty ledger if missing."""
        return read_json(self._file) or {"total": 0.0, "runs": [], "cloned_stems": {}}

    def estimate_dub_cost(self, engine: str, tts: str, tier: str,
                          video: Path, stem: str, already_cloned: dict) -> dict:
        """Estimate this dub's fal.ai cost. Returns a breakdown dict (no side effects)."""
        dur = video_duration(ffprobe_json(video, self._ffmpeg_bin)) if video and video.is_file() else 0.0
        script = self._swap_work / stem / "script-edited.txt"
        chars = len(script.read_text(encoding="utf-8")) if script.is_file() else 0

        voice_cost = clone_cost = 0.0
        if engine == "fal":
            voice_cost = round(chars / 1000.0 * self.TTS_RATE_PER_1K.get(tts, 0.0), 4)
            if tts in ("turbo", "hd") and already_cloned.get(stem) != tts:
                clone_cost = self.MINIMAX_CLONE_FEE  # one-time voice clone for this stem+model
        lip = tier if engine == "fal" else tier  # both use the tier name; local voice is free
        lipsync_cost = round(dur * self.LIPSYNC_RATE_PER_SEC.get(lip, 0.0), 4)

        total = round(voice_cost + clone_cost + lipsync_cost, 4)
        parts = []
        if engine == "local":
            parts.append("voice: local XTTS (free)")
        else:
            parts.append(f"voice {tts}: ${voice_cost:.3f} ({chars} chars)")
            if clone_cost:
                parts.append(f"one-time clone: ${clone_cost:.2f}")
        if lipsync_cost:
            parts.append(f"lip-sync {lip}: ${lipsync_cost:.3f} ({dur:.0f}s)")
        elif lip == "wav2lip-hd":
            parts.append("lip-sync: Wav2Lip HD + GFPGAN (local GPU, free)")
        elif lip == "wav2lip":
            parts.append("lip-sync: Wav2Lip (local GPU, free)")
        elif lip == "none":
            parts.append("lip-sync: none (free)")
        return {
            "this_run": total, "chars": chars, "duration": round(dur, 1),
            "tts": tts, "tier": lip, "engine": engine,
            "clone": clone_cost, "summary": " · ".join(parts),
        }

    def record(self, stem: str, info: dict) -> dict:
        """Append a run's cost to the ledger and return {this_run, total}."""
        with self._lock:
            d = self.load()
            d["total"] = round(float(d.get("total", 0.0)) + info["this_run"], 4)
            d.setdefault("runs", []).append({
                "ts": time.time(), "stem": stem, "cost": info["this_run"],
                "summary": info["summary"], "engine": info["engine"],
            })
            d["runs"] = d["runs"][-100:]
            if info.get("clone"):
                d.setdefault("cloned_stems", {})[stem] = info["tts"]
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps(d, indent=1), encoding="utf-8")
            return {"this_run": info["this_run"], "total": d["total"]}
