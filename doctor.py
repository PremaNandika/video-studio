#!/usr/bin/env python3
"""Video Studio — environment doctor.

Checks whether THIS machine can actually run the pipeline, and prints a fix
command for every failure. Run it with any Python 3.10+; it inspects the four
project venvs by path, so it does not need to run inside one:

    python doctor.py

Why this exists: every path, venv, CUDA wheel and model weight in this project
is machine-specific, and the failure modes lie. `torch.cuda.is_available()`
returns True on a GPU whose compute capability the installed wheels do not
support -- the real error only surfaces on the first kernel launch. So this
script runs an actual matmul rather than trusting the availability flag.

Exit code 0 = every REQUIRED check passed. Optional checks never fail the run.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "video-studio" / "config.json"

# ── result plumbing ───────────────────────────────────────────────────────────
OK, WARN, FAIL = "OK", "WARN", "FAIL"
rows: list[tuple[str, str, str, str]] = []   # (status, area, detail, fix)


def add(status: str, area: str, detail: str, fix: str = "") -> None:
    rows.append((status, area, detail, fix))


def run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    """Run a command, returning (rc, stdout+stderr). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "executable not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except Exception as e:                                    # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def py_snippet(venv_py: Path, code: str, timeout: int = 180) -> tuple[int, str]:
    return run([str(venv_py), "-c", code], timeout=timeout)


# ── 1. config ─────────────────────────────────────────────────────────────────
def check_config() -> dict:
    if not CONFIG_PATH.is_file():
        add(FAIL, "config.json", "missing",
            f"copy {CONFIG_PATH.parent / 'config.example.json'} -> config.json and edit it")
        return {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:                                    # noqa: BLE001
        add(FAIL, "config.json", f"unparseable: {e}", "fix the JSON syntax")
        return {}

    # every path key must exist on this machine -- this is the #1 breakage when
    # the project is moved or cloned, because these are absolute paths
    dead: list[str] = []
    for key in ("autovsl_root", "engines_dir", "subtitle_studio", "dubbing_studio",
                "course_pipeline", "brand_kit", "banks_dir"):
        val = cfg.get(key)
        if val and not Path(val).exists():
            dead.append(key)
    for key, val in (cfg.get("engines") or {}).items():
        if not Path(val).exists():
            dead.append(f"engines.{key}")

    if dead:
        add(FAIL, "config.json paths", f"{len(dead)} dead: {', '.join(dead)}",
            f"paths point outside this checkout; re-point them at {ROOT}")
    else:
        add(OK, "config.json", f"parsed, paths resolve (data root: {cfg.get('autovsl_root')})")

    if cfg.get("secret_key", "").startswith("dev-only"):
        add(WARN, "config.secret_key", "default value", "set a random secret_key")
    return cfg


def check_prompts() -> None:
    """prompts.json holds every LLM prompt; a typo there breaks the AI tabs at runtime,
    not at boot, so validate the whole file (placeholders included) up front."""
    rc, out = run([sys.executable, str(ROOT / "prompts.py")], timeout=30)
    tail = (out.strip().splitlines() or ["no output"])[-1]
    if rc == 0:
        add(OK, "prompts.json", tail)
    else:
        add(FAIL, "prompts.json", tail.removeprefix("ERROR: ") or "validation failed",
            "fix prompts.json (or prompts.local.json), then re-run `python prompts.py`")


# ── 2. external binaries ──────────────────────────────────────────────────────
def check_binaries() -> None:
    # ffmpeg AND ffprobe must resolve; engines shell out to both by bare name
    for tool in ("ffmpeg", "ffprobe"):
        found = shutil.which(tool)
        if found:
            rc, out = run([found, "-version"], timeout=30)
            ver = out.splitlines()[0][:60] if out else "?"
            add(OK if rc == 0 else FAIL, tool, ver if rc == 0 else "found but won't run",
                "" if rc == 0 else "reinstall: winget install Gyan.FFmpeg")
        else:
            add(FAIL, tool, "not on PATH", "winget install Gyan.FFmpeg  (then open a new shell)")

    claude = shutil.which("claude") or next(
        (str(p) for p in (Path.home() / ".local/bin/claude.exe",
                          Path.home() / ".local/bin/claude") if p.exists()), None)
    if claude:
        add(OK, "claude CLI", claude)
    else:
        add(FAIL, "claude CLI", "not found",
            "install Claude Code + sign in (script rewrite & vision advisor need it)")

    if shutil.which("git"):
        add(OK, "git", shutil.which("git"))
    else:
        add(WARN, "git", "not on PATH", "install Git for Windows")


# ── 3. GPU ────────────────────────────────────────────────────────────────────
def check_gpu() -> str | None:
    """Return the GPU's compute capability as 'sm_XX', or None if unavailable."""
    rc, out = run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                   "--format=csv,noheader"], timeout=30)
    if rc != 0:
        add(WARN, "GPU", "no nvidia-smi -- CPU only (dubbing will be unusably slow)",
            "install the NVIDIA driver, or accept CPU-only")
        return None
    add(OK, "GPU", out.strip().splitlines()[0][:70])
    return None  # capability is read via torch below (nvidia-smi doesn't report it portably)


# ── 4. venvs ──────────────────────────────────────────────────────────────────
# name -> (config key, required?, modules that must import, gpu probe, what it powers)
#
# The gpu probe differs per venv because the compute backends differ:
#   "ct2"   -> faster-whisper runs on CTranslate2, NOT torch. torch may be present
#              in that venv but nothing imports it, so its CUDA build is irrelevant.
#   "torch" -> XTTS/Wav2Lip/VSR do their maths in torch, so torch kernels must exist.
VENVS: dict[str, tuple[str, bool, list[str], str | None, str]] = {
    "cv":      ("cv",      True,  ["flask", "cv2", "numpy", "scipy", "PIL"], None,    "Flask server + repair engines"),
    "whisper": ("whisper", True,  ["faster_whisper", "ctranslate2"],         "ct2",   "transcription + caption timing"),
    "dub":     ("dub",     True,  ["torch", "TTS"],                          "torch", "XTTS voice clone + Wav2Lip"),
    "vsr":     ("vsr",     False, ["torch"],                                 "torch", "video super-resolution (Power Tools)"),
}

IMPORT_PROBE = r"""
import importlib, sys
mods = {mods!r}
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        bad.append(f"{{m}}({{type(e).__name__}})")
print("PYTHON", sys.version.split()[0])
print("MISSING", ",".join(bad) if bad else "-")
"""

# CTranslate2 carries its own CUDA kernels; the real question is whether it can
# both see the device and load its cuBLAS/cuDNN DLLs, so actually run inference.
CT2_PROBE = r"""
import ctranslate2
print("CT2", ctranslate2.__version__)
print("DEVICES", ctranslate2.get_cuda_device_count())
try:
    from faster_whisper import WhisperModel
    import numpy as np
    m = WhisperModel("tiny", device="cuda", compute_type="int8_float16")
    list(m.transcribe(np.zeros(16000, dtype=np.float32))[0])
    print("INFER", "ok")
except Exception as e:
    print("INFER", f"FAILED {type(e).__name__} {str(e)[:120]}")
"""

# The check that matters: is_available() lies on an unsupported card, so launch a kernel.
CUDA_PROBE = r"""
import torch
print("TORCH", torch.__version__)
print("AVAIL", torch.cuda.is_available())
if torch.cuda.is_available():
    cc = torch.cuda.get_device_capability(0)
    print("DEVCC", f"sm_{cc[0]}{cc[1]}")
    print("ARCHS", ",".join(torch.cuda.get_arch_list()))
    try:
        x = torch.randn(64, 64, device="cuda")
        float((x @ x).sum())
        print("KERNEL", "ok")
    except Exception as e:
        print("KERNEL", f"FAILED {type(e).__name__}")
"""


def torch_fix_hint(dev_cc: str, archs: str) -> str:
    """Suggest the right wheel when the installed torch has no kernels for this GPU."""
    if dev_cc == "sm_120":       # Blackwell (RTX 50-series) needs cu128 or newer
        return "pip install --force-reinstall torch torchaudio --index-url https://download.pytorch.org/whl/cu130"
    return ("reinstall torch against a CUDA build that includes "
            f"{dev_cc} (installed wheel covers: {archs}) -- see pytorch.org/get-started/locally")


def check_venvs(cfg: dict) -> None:
    venvs = cfg.get("venvs") or {}
    for name, (key, required, mods, probe, purpose) in VENVS.items():
        label = f"venv:{name}"
        raw = venvs.get(key)
        if not raw:
            add(FAIL if required else WARN, label, f"not in config.venvs ({purpose})",
                f'add "{key}" to config.json venvs')
            continue
        venv_py = Path(raw)
        if not venv_py.is_file():
            add(FAIL if required else WARN, label, f"interpreter missing: {venv_py}",
                f"python -m venv the {name} env (see SETUP.md), then install its requirements")
            continue

        rc, out = py_snippet(venv_py, IMPORT_PROBE.format(mods=mods))
        info = dict(
            (ln.split(" ", 1) + [""])[:2] for ln in out.splitlines() if " " in ln
        ) if rc == 0 else {}
        pyver = info.get("PYTHON", "?")
        missing = info.get("MISSING", "?")

        if rc != 0:
            add(FAIL if required else WARN, label, f"interpreter won't run: {out.strip()[:80]}",
                "recreate the venv")
            continue
        if missing != "-":
            add(FAIL if required else WARN, label, f"py{pyver} -- missing: {missing}",
                f'"{venv_py}" -m pip install -r <that project>/requirements.txt')
        else:
            add(OK, label, f"py{pyver} -- all imports OK ({purpose})")

        if missing != "-" or not probe:
            continue      # can't probe the GPU through a venv that's missing its deps

        if probe == "ct2":
            rc2, out2 = py_snippet(venv_py, CT2_PROBE, timeout=600)
            t = dict((ln.split(" ", 1) + [""])[:2] for ln in out2.splitlines() if " " in ln)
            ver, devs, infer = t.get("CT2", "?"), t.get("DEVICES", "0"), t.get("INFER", "?")
            if devs == "0":
                add(WARN, f"{label}:cuda", f"ctranslate2 {ver} sees no CUDA device (CPU fallback)",
                    "transcription still works, just slower")
            elif infer == "ok":
                add(OK, f"{label}:cuda", f"ctranslate2 {ver} -- GPU inference OK")
            else:
                add(FAIL, f"{label}:cuda", f"ctranslate2 {ver} GPU inference {infer}",
                    "upgrade ctranslate2 (needs a build with kernels for this GPU), "
                    "or force --device cpu")
            continue

        rc2, out2 = py_snippet(venv_py, CUDA_PROBE, timeout=240)
        t = dict((ln.split(" ", 1) + [""])[:2] for ln in out2.splitlines() if " " in ln)
        tver, avail = t.get("TORCH", "?"), t.get("AVAIL", "?")
        kernel, dev_cc, archs = t.get("KERNEL"), t.get("DEVCC", "?"), t.get("ARCHS", "?")
        if avail != "True":
            add(WARN, f"{label}:cuda", f"torch {tver} -- CUDA unavailable (CPU mode)",
                "install a CUDA build of torch if you have an NVIDIA GPU")
        elif kernel == "ok":
            add(OK, f"{label}:cuda", f"torch {tver} -- {dev_cc} kernel launch OK")
        else:
            add(FAIL, f"{label}:cuda",
                f"torch {tver} has NO kernels for {dev_cc} (built for {archs})",
                torch_fix_hint(dev_cc, archs))


# ── 4b. dub stack runtime traps ───────────────────────────────────────────────
# These only surface once a dub is already running (minutes in, mid-synthesis),
# so they are worth catching up front.
DUB_STACK_PROBE = r"""
import numpy as np, tempfile, os
print("VERSIONS", "")
import torch, torchaudio
print("TORCHAUDIO", torchaudio.__version__)

# XTTS loads its voice reference through torchaudio.load. From torchaudio 2.9 on
# that delegates to torchcodec, which needs FFmpeg *shared* libs -- the Gyan
# static build has none, so this raises deep inside the voice stage.
try:
    import soundfile as sf
    p = os.path.join(tempfile.gettempdir(), "_doctor_probe.wav")
    sf.write(p, np.zeros(2205, dtype="float32"), 22050)
    torchaudio.load(p)
    print("AUDIOLOAD", "ok")
except Exception as e:
    print("AUDIOLOAD", f"FAILED {type(e).__name__}")

# gfpgan -> basicsr imports torchvision.transforms.functional_tensor, removed in
# torchvision >= 0.17. Breaks the HD lip-sync stage only.
try:
    from gfpgan import GFPGANer
    print("GFPGAN", "ok")
except Exception as e:
    print("GFPGAN", f"FAILED {type(e).__name__}")
"""


def check_dub_stack(cfg: dict) -> None:
    raw = (cfg.get("venvs") or {}).get("dub")
    if not raw or not Path(raw).is_file():
        return
    rc, out = py_snippet(Path(raw), DUB_STACK_PROBE, timeout=300)
    t = dict((ln.split(" ", 1) + [""])[:2] for ln in out.splitlines() if " " in ln)
    if "TORCHAUDIO" not in t:
        return      # import-level failure already reported by check_venvs

    ta = t.get("TORCHAUDIO", "?")
    if t.get("AUDIOLOAD") == "ok":
        add(OK, "dub:torchaudio.load", f"torchaudio {ta} -- loads audio natively")
    else:
        add(FAIL, "dub:torchaudio.load", f"torchaudio {ta} cannot load audio ({t.get('AUDIOLOAD')})",
            "pin torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0 (cu128) -- "
            "torchaudio >= 2.9 needs torchcodec, which needs ffmpeg shared libs")
    if t.get("GFPGAN") == "ok":
        add(OK, "dub:gfpgan", "imports OK (HD lip-sync available)")
    else:
        add(FAIL, "dub:gfpgan", f"import failed ({t.get('GFPGAN')})",
            "patch site-packages/basicsr/data/degradations.py: import rgb_to_grayscale "
            "from torchvision.transforms.functional (not .functional_tensor)")


# ── 5. model weights ──────────────────────────────────────────────────────────
# (relative path, required?, what needs it, where to get it)
WEIGHTS = [
    ("tools/Wav2Lip/checkpoints/wav2lip_gan.pth", True,
     "local lip-sync", "github.com/Rudrabha/Wav2Lip releases"),
    ("tools/Wav2Lip/gfpgan_weights/GFPGANv1.4.pth", True,
     "lip-sync face restore", "github.com/TencentARC/GFPGAN releases"),
    ("tools/Wav2Lip/face_detection/detection/sfd/s3fd.pth", True,
     "face detection", "bundled with Wav2Lip"),
    ("tools/ProPainter", False, "subtitle erasing", "clone github.com/sczhou/ProPainter"),
    ("ComfyUI_windows_portable", False, "Brand Studio images", "ComfyUI portable for Windows"),
]


def check_weights() -> None:
    for rel, required, purpose, src in WEIGHTS:
        p = ROOT / rel
        if p.exists():
            size = f" ({p.stat().st_size / 1e6:.0f} MB)" if p.is_file() else ""
            add(OK, f"weights:{Path(rel).name}", f"present{size}")
        else:
            add(FAIL if required else WARN, f"weights:{Path(rel).name}",
                f"missing -- {purpose} will fail", f"download from {src} -> {rel}")


# ── 6. data root ──────────────────────────────────────────────────────────────
def check_data(cfg: dict) -> None:
    root = cfg.get("autovsl_root")
    if not root or not Path(root).is_dir():
        return
    data = Path(root)
    ups = data / "uploads"
    work = data / "output" / "script-swap"
    n_up = len([p for p in ups.glob("*") if p.is_file()]) if ups.is_dir() else 0
    n_wd = len([p for p in work.glob("*") if p.is_dir()]) if work.is_dir() else 0
    add(OK if ups.is_dir() else WARN, "data:uploads",
        f"{n_up} file(s) in {ups}" if ups.is_dir() else f"missing: {ups}",
        "" if ups.is_dir() else "create it (New Project uploads land here)")
    add(OK, "data:workdirs", f"{n_wd} dub workdir(s)")
    if (data / ".env").is_file():
        add(OK, "data:.env", "present (fal.ai key for paid dubs)")
    else:
        add(WARN, "data:.env", "missing -- paid fal.ai dubs unavailable",
            f"create {data / '.env'} with FAL_KEY=... (skip to stay free/local)")


# ── report ────────────────────────────────────────────────────────────────────
BADGE = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}


def main() -> int:
    print(f"Video Studio doctor -- {ROOT}")
    print(f"host python {sys.version.split()[0]} on {sys.platform}\n")

    cfg = check_config()
    check_prompts()
    check_binaries()
    check_gpu()
    if cfg:
        check_venvs(cfg)
        check_dub_stack(cfg)
    check_weights()
    if cfg:
        check_data(cfg)

    width = max(len(a) for _, a, _, _ in rows) + 2
    for status, area, detail, _ in rows:
        print(f"{BADGE[status]} {area.ljust(width)} {detail}")

    fails = [r for r in rows if r[0] == FAIL]
    warns = [r for r in rows if r[0] == WARN]
    print(f"\n{len(rows) - len(fails) - len(warns)} ok · {len(warns)} warn · {len(fails)} fail")

    if fails or warns:
        print("\nfixes, in order:")
        for status, area, _, fix in rows:
            if fix and status in (FAIL, WARN):
                print(f"  {'!' if status == FAIL else '·'} {area}: {fix}")

    if fails:
        print(f"\n{len(fails)} required check(s) failing -- the app will not work yet.")
        return 1
    print("\nAll required checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
