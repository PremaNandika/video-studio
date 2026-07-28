#!/usr/bin/env python3
"""Video Studio — one-shot environment installer.

Builds everything this project needs on a fresh Windows + NVIDIA machine:
config.json, the four Python venvs, the CUDA wheels that match YOUR GPU, the
model weights, and the one third-party source patch the lip-sync stage needs.

    python setup.py                 # everything (skips what is already done)
    python setup.py --only dub      # one step
    python setup.py --list          # show step names
    python setup.py --skip-optional # required steps only (no vsr / ProPainter)

Every step is idempotent: re-running skips finished work, so it is safe after
a partial or failed run. Nothing is deleted -- a venv that exists is reused.
Run `python doctor.py` afterwards to verify.

Why this file exists: the recipe is genuinely machine-dependent (CUDA arch,
Python version, install order) and several failures only surface minutes into
a running job. Everything encoded here was verified on an RTX 5060 (sm_120)
rather than inferred -- see NOTES at the bottom.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VS = ROOT / "video-studio"

# ── shell helpers ─────────────────────────────────────────────────────────────
class Fail(RuntimeError):
    """A step could not continue; message is shown to the user."""


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def head(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, streaming nothing, raising Fail on non-zero exit."""
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-6:]
        raise Fail(f"command failed: {' '.join(str(c) for c in cmd[:3])}...\n    "
                   + "\n    ".join(tail))
    return p


def pip(venv_py: Path, *args: str, index: str | None = None) -> None:
    cmd = [venv_py, "-m", "pip", "install", "--disable-pip-version-check", *args]
    if index:
        cmd += ["--index-url", index]
    run(cmd)


def venv_py(venv_dir: Path) -> Path:
    return venv_dir / "Scripts" / "python.exe"


def has_modules(venv_dir: Path, mods: list[str]) -> bool:
    """True if every module imports inside that venv (used to skip finished work)."""
    py = venv_py(venv_dir)
    if not py.is_file():
        return False
    code = "import importlib,sys\n" + \
           f"[importlib.import_module(m) for m in {mods!r}]\nprint('ok')"
    try:
        p = subprocess.run([str(py), "-c", code], capture_output=True, text=True,
                           timeout=300, encoding="utf-8", errors="replace")
        return p.returncode == 0
    except Exception:
        return False


# ── host detection ────────────────────────────────────────────────────────────
def find_python(want: str) -> Path:
    """Locate a specific CPython minor version (e.g. "3.12") on this machine."""
    launcher = shutil.which("py")
    if launcher:
        p = subprocess.run([launcher, f"-{want}", "-c",
                            "import sys;print(sys.executable)"],
                           capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip():
            return Path(p.stdout.strip())
    for guess in (
        Path(os.environ.get("LOCALAPPDATA", "")) / f"Programs/Python/Python{want.replace('.', '')}/python.exe",
        Path(f"C:/Python{want.replace('.', '')}/python.exe"),
    ):
        if guess.is_file():
            return guess
    raise Fail(f"Python {want} not found. Install it from python.org "
               f"(the dub venv needs exactly {want}), then re-run.")


def compute_cap() -> tuple[int, int] | None:
    """GPU compute capability as (major, minor), or None if there is no NVIDIA GPU."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        p = subprocess.run([exe, "--query-gpu=compute_cap", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=60)
        m = re.search(r"(\d+)\.(\d+)", p.stdout or "")
        return (int(m.group(1)), int(m.group(2))) if m else None
    except Exception:
        return None


# torch 2.8.0+cu128 ships kernels for sm_61 .. sm_120, i.e. Pascal through
# Blackwell -- one pin covers essentially every CUDA GPU this project targets.
# It is also the NEWEST release whose torchaudio still loads audio natively;
# 2.9+ routes torchaudio.load through torchcodec (see NOTES).
TORCH_CUDA = ["torch==2.8.0", "torchaudio==2.8.0", "torchvision==0.23.0"]
TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu128"
TORCH_MIN_CC = (6, 1)


def torch_install(py: Path, cc: tuple[int, int] | None) -> None:
    if cc is None:
        say("no NVIDIA GPU detected -> installing CPU torch (dubbing will be very slow)")
        pip(py, *TORCH_CUDA, index="https://download.pytorch.org/whl/cpu")
        return
    if cc < TORCH_MIN_CC:
        say(f"GPU is sm_{cc[0]}{cc[1]}, older than the cu128 wheels support "
            f"-> installing CPU torch")
        pip(py, *TORCH_CUDA, index="https://download.pytorch.org/whl/cpu")
        return
    say(f"GPU is sm_{cc[0]}{cc[1]} -> torch 2.8.0 + cu128")
    pip(py, *TORCH_CUDA, index=TORCH_CUDA_INDEX)


# ── step: config.json ─────────────────────────────────────────────────────────
def step_config() -> None:
    """Write video-studio/config.json from the example, resolved for this machine."""
    dst, src = VS / "config.json", VS / "config.example.json"
    if dst.is_file():
        say(f"exists, leaving alone: {dst.name}")
        return
    if not src.is_file():
        raise Fail(f"missing template: {src}")

    text = src.read_text(encoding="utf-8")
    text = (text.replace("<REPO>", str(ROOT).replace("\\", "/"))
                .replace("<YOUR-HOME>", str(Path.home()).replace("\\", "/")))
    cfg = json.loads(text)
    cfg.pop("_comment", None)
    cfg["secret_key"] = secrets.token_hex(32)
    pin = f"{secrets.randbelow(10**6):06d}" if hasattr(secrets, "randbelow") \
        else f"{secrets.randbits(20) % 10**6:06d}"
    cfg["remote_pin"] = pin
    dst.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    say(f"wrote {dst.name} -- generated PIN {pin} (phone/LAN access)")
    say("edit it if you want a different port, PIN or exports dir")


# ── step: the cv venv (Flask server + repair engines) ─────────────────────────
CV_MODULES = ["flask", "cv2", "numpy", "scipy", "PIL"]


def step_cv() -> None:
    d = ROOT / "autoVSL" / ".venv"
    if has_modules(d, CV_MODULES):
        say("already complete")
        return
    if not venv_py(d).is_file():
        say(f"creating venv at {d}")
        run([sys.executable, "-m", "venv", d])
    py = venv_py(d)
    pip(py, "--upgrade", "pip")
    say("installing flask / opencv / numpy / scipy / pillow")
    pip(py, "flask", "opencv-python", "numpy", "scipy", "pillow")
    req = ROOT / "autoVSL" / "requirements.txt"
    if req.is_file():
        say("installing autoVSL/requirements.txt (edge-tts, fal-client, httpx)")
        pip(py, "-r", req)


# ── step: the whisper venv (transcription) ────────────────────────────────────
def step_whisper() -> None:
    """faster-whisper runs on CTranslate2 -- deliberately NO torch here."""
    d = ROOT / "course_pipeline" / ".venv"
    if has_modules(d, ["faster_whisper", "ctranslate2"]):
        say("already complete")
        return
    if not venv_py(d).is_file():
        say(f"creating venv at {d}")
        run([sys.executable, "-m", "venv", d])
    py = venv_py(d)
    pip(py, "--upgrade", "pip")
    req = ROOT / "course_pipeline" / "requirements.txt"
    say("installing faster-whisper (CTranslate2 backend, no torch needed)")
    if req.is_file():
        pip(py, "-r", req)
    else:
        pip(py, "faster-whisper", "tqdm", "imageio-ffmpeg")
    if compute_cap():
        # transcribe.py::_register_cuda_dlls() looks for these in site-packages/nvidia
        say("installing cuBLAS/cuDNN wheels for GPU transcription (~500 MB)")
        pip(py, "nvidia-cublas-cu12", "nvidia-cudnn-cu12")


# ── step: the dub venv (XTTS + Wav2Lip) ───────────────────────────────────────
# Install ORDER is load-bearing. gfpgan's chain pulls numpy 2 + opencv 5, which
# break coqui-tts (needs numpy < 2), so the numpy-1.x pins are re-applied last.
DUB_PY_VERSION = "3.12"
DUB_CORE = ["coqui-tts==0.25.1", "soundfile==0.12.1", "ffmpeg-python==0.2.0",
            "click", "tqdm", "numpy<2.0"]
DUB_LIPSYNC = ["gfpgan", "realesrgan"]
DUB_REPIN = ["numpy==1.26.4", "opencv-python==4.10.0.84",
             "scikit-image==0.24.0", "tifffile==2024.8.30"]


def step_dub() -> None:
    d = ROOT / "dubbing-studio" / "venv"
    if has_modules(d, ["torch", "TTS", "gfpgan"]):
        say("already complete")
        patch_basicsr(d)          # cheap, and does not survive a venv rebuild
        return

    if not venv_py(d).is_file():
        base = find_python(DUB_PY_VERSION)
        say(f"creating venv at {d} using Python {DUB_PY_VERSION} ({base})")
        run([base, "-m", "venv", d])
    py = venv_py(d)

    ver = run([py, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"]).stdout.strip()
    if ver != DUB_PY_VERSION:
        raise Fail(f"{d} is Python {ver}, but this stack needs {DUB_PY_VERSION} "
                   f"(numpy<2 and coqui-tts have no wheels for 3.13/3.14).\n"
                   f"    Delete that folder and re-run.")

    pip(py, "--upgrade", "pip")
    torch_install(py, compute_cap())
    say("installing coqui-tts (XTTS v2) + audio deps")
    pip(py, *DUB_CORE)
    say("installing gfpgan/realesrgan for the HD lip-sync stage")
    pip(py, *DUB_LIPSYNC)
    say("re-pinning numpy 1.x / opencv 4.x (gfpgan's chain bumps them)")
    pip(py, *DUB_REPIN)
    patch_basicsr(d)


def patch_basicsr(venv_dir: Path) -> None:
    """torchvision >= 0.17 removed transforms.functional_tensor; basicsr still imports it.

    Lives in site-packages, so it is re-applied on every setup run -- a venv
    rebuild or a basicsr upgrade silently reverts it and breaks HD lip-sync.
    """
    f = venv_dir / "Lib/site-packages/basicsr/data/degradations.py"
    if not f.is_file():
        return
    old = "from torchvision.transforms.functional_tensor import rgb_to_grayscale"
    new = "from torchvision.transforms.functional import rgb_to_grayscale"
    text = f.read_text(encoding="utf-8")
    if old not in text:
        return
    f.write_text(text.replace(old, new), encoding="utf-8")
    cache = f.parent / "__pycache__"
    if cache.is_dir():
        shutil.rmtree(cache, ignore_errors=True)
    say("patched basicsr/data/degradations.py (functional_tensor -> functional)")


# ── step: the vsr venv (optional, Power Tools upscale) ────────────────────────
def step_vsr() -> None:
    d = ROOT / "tools" / "vsr" / ".venv"
    req = ROOT / "tools" / "vsr" / "requirements.txt"
    if not req.is_file():
        say("tools/vsr not present, skipping")
        return
    if has_modules(d, ["torch", "cv2"]):
        say("already complete")
        return
    if not venv_py(d).is_file():
        say(f"creating venv at {d}")
        run([sys.executable, "-m", "venv", d])
    py = venv_py(d)
    pip(py, "--upgrade", "pip")
    torch_install(py, compute_cap())
    say("installing tools/vsr/requirements.txt")
    pip(py, "-r", req)


# ── step: model weights ───────────────────────────────────────────────────────
# (destination, url, min MB, what needs it)
WEIGHTS = [
    ("tools/Wav2Lip/checkpoints/wav2lip_gan.pth",
     "https://huggingface.co/camenduru/Wav2Lip/resolve/main/checkpoints/wav2lip_gan.pth",
     300, "local lip-sync"),
    ("tools/Wav2Lip/gfpgan_weights/GFPGANv1.4.pth",
     "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.4/GFPGANv1.4.pth",
     250, "lip-sync face restore"),
]


def step_weights() -> None:
    for rel, url, min_mb, purpose in WEIGHTS:
        dst = ROOT / rel
        if dst.is_file() and dst.stat().st_size > min_mb * 1_000_000:
            say(f"have {dst.name} ({dst.stat().st_size / 1e6:.0f} MB)")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        say(f"downloading {dst.name} for {purpose} ...")
        tmp = dst.with_suffix(dst.suffix + ".part")
        try:
            urllib.request.urlretrieve(url, tmp)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise Fail(f"could not download {dst.name}: {e}\n"
                       f"    fetch it manually from {url}\n"
                       f"    and save it as {rel}")
        mb = tmp.stat().st_size / 1e6
        if mb < min_mb:
            tmp.unlink(missing_ok=True)
            raise Fail(f"{dst.name} downloaded only {mb:.0f} MB "
                       f"(expected > {min_mb} MB) -- the mirror likely served an "
                       f"error page. Download it manually from {url}")
        tmp.replace(dst)
        say(f"  saved {dst.name} ({mb:.0f} MB)")


def step_propainter() -> None:
    d = ROOT / "tools" / "ProPainter"
    if d.is_dir():
        say("already present")
        return
    git = shutil.which("git")
    if not git:
        raise Fail("git not on PATH; clone github.com/sczhou/ProPainter into tools/ manually")
    say("cloning ProPainter (subtitle erasing, ~600 MB)")
    run([git, "clone", "--depth", "1", "https://github.com/sczhou/ProPainter", d])


# ── driver ────────────────────────────────────────────────────────────────────
# name -> (function, required?, description)
STEPS: dict[str, tuple] = {
    "config":     (step_config,     True,  "video-studio/config.json for this machine"),
    "cv":         (step_cv,         True,  "autoVSL/.venv -- Flask server + repair engines"),
    "whisper":    (step_whisper,    True,  "course_pipeline/.venv -- transcription"),
    "dub":        (step_dub,        True,  "dubbing-studio/venv -- XTTS + Wav2Lip (Python 3.12)"),
    "weights":    (step_weights,    True,  "Wav2Lip + GFPGAN checkpoints (~750 MB)"),
    "vsr":        (step_vsr,        False, "tools/vsr/.venv -- Power Tools upscale"),
    "propainter": (step_propainter, False, "tools/ProPainter -- subtitle erasing"),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Install the Video Studio environment.")
    ap.add_argument("--only", help="comma-separated step names (see --list)")
    ap.add_argument("--skip-optional", action="store_true",
                    help="required steps only (no vsr / ProPainter)")
    ap.add_argument("--list", action="store_true", help="list steps and exit")
    args = ap.parse_args()

    if args.list:
        for name, (_, required, desc) in STEPS.items():
            print(f"  {name:<11} {'required' if required else 'optional':<9} {desc}")
        return 0

    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in STEPS]
        if unknown:
            print(f"unknown step(s): {', '.join(unknown)}\nsee: python setup.py --list")
            return 2
    else:
        wanted = [n for n, (_, req, _) in STEPS.items()
                  if req or not args.skip_optional]

    cc = compute_cap()
    print(f"Video Studio setup -- {ROOT}")
    print(f"host python {sys.version.split()[0]} · "
          f"GPU {'sm_%d%d' % cc if cc else 'none detected (CPU mode)'}")
    print(f"steps: {', '.join(wanted)}")

    failed: list[tuple[str, str]] = []
    for name in wanted:
        fn, required, desc = STEPS[name]
        head(f"{name} -- {desc}")
        try:
            fn()
        except Fail as e:
            failed.append((name, str(e)))
            print(f"  FAILED: {e}")
            if required:
                print("  (required step -- continuing so you can see all problems)")
        except KeyboardInterrupt:
            print("\ninterrupted")
            return 130

    print()
    if failed:
        print(f"{len(failed)} step(s) failed:")
        for name, msg in failed:
            print(f"  - {name}: {msg.splitlines()[0]}")
        print("\nFix the above and re-run -- finished steps are skipped.")
        return 1

    print("Setup complete. Verify with:  python doctor.py")
    return 0


# ── NOTES ─────────────────────────────────────────────────────────────────────
# Verified on Windows 11 / RTX 5060 (sm_120, Blackwell) / Python 3.12 + 3.14.
#
# 1. torch 2.8.0 + cu128 is chosen for BOTH reasons at once:
#      - cu121/cu126 ship no sm_120 kernels. torch.cuda.is_available() still
#        returns True on such a card; the failure only appears at the first
#        kernel launch ("no kernel image is available").
#      - torchaudio >= 2.9 routes .load() through torchcodec, which needs FFmpeg
#        *shared* libraries. The Gyan full_build this project installs is static
#        (3 exes, no DLLs), so XTTS dies mid-synthesis inside load_audio().
#    2.8.0/cu128 is the newest build satisfying both, and its arch list spans
#    sm_61..sm_120, so it also covers older cards.
#
# 2. The dub venv must be Python 3.12: numpy<2.0 (required by coqui-tts) has no
#    wheels for 3.13/3.14. The other venvs are fine on 3.14.
#
# 3. The whisper venv deliberately has no torch. faster-whisper runs on
#    CTranslate2; transcribe.py, caption.py and recaption.py are the only
#    scripts invoked with it and none import torch. GPU support comes from the
#    nvidia-cublas/cudnn wheels that _register_cuda_dlls() looks for.
#
# 4. patch_basicsr() runs on EVERY invocation, including the "already complete"
#    path, because it edits site-packages -- a venv rebuild or a basicsr
#    upgrade silently reverts it and only HD lip-sync breaks.
if __name__ == "__main__":
    sys.exit(main())
