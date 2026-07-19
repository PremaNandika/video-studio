# Onboarding — Setting up Video Studio after cloning

Follow these steps top to bottom. On a normal connection the whole setup takes 1–2 hours, most of it downloads.

## 0. Prerequisites

- **Windows 10/11** (the project is Windows-first; paths and scripts assume it)
- **NVIDIA GPU** with ≥4 GB VRAM + up-to-date NVIDIA driver (reference machine: RTX 3050 Ti 4 GB)
- **Python 3.14 x64** — install from python.org, check "Add to PATH"
- **Git** (Git Bash included)
- **ffmpeg** — install the Gyan build via WinGet:
  ```powershell
  winget install Gyan.FFmpeg
  ```
- **Claude CLI** (`claude`) — required for script rewriting and the DubSync vision advisor. Install Claude Code and sign in once; the app calls `claude -p` headlessly.
- **Windows Smart App Control must be OFF** (Settings → Privacy & security → App & browser control). If it's on, it blocks unsigned DLLs (llvmlite/numba) with WinError 4551 and the dubbing venv will not load.

## 1. Clone

```bash
git clone https://github.com/YOUR-ORG/video-studio.git "Video AI editing"
cd "Video AI editing"
```

Note the quotes — the folder name contains spaces, and several configs assume that.

## 2. Create the 4 Python environments

Each engine family has conflicting dependencies (different torch/CUDA stacks), so they are isolated. All four use a file literally named `requirements.txt` in their folder.

**PowerShell** (for Git Bash, replace the activate line with `source <venv>/Scripts/activate`):

```powershell
# 2a. Main env — runs the Flask server + repair engines (OpenCV, numpy, Pillow)
cd autoVSL
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
deactivate

# 2b. Transcription env — faster-whisper (CUDA)
cd ..\course_pipeline
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
deactivate

# 2c. Dubbing env — torch + XTTS + Wav2Lip deps (the big one, several GB)
cd ..\dubbing-studio
python -m venv venv
venv\Scripts\activate
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
pip install -r requirements.txt
deactivate

# 2d. (Optional) Video super-resolution env — Power Tools tab
cd ..\tools\vsr
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
deactivate
```

Note 2c installs torch for **CUDA 12.6** (`cu126`) — correct for RTX 30-series. Adjust the index URL if your GPU needs a different CUDA build.

## 3. Download model weights (not in git)

**Wav2Lip + GFPGAN** (local lip-sync, ~1.5 GB, required):

| File | From | Save to |
|---|---|---|
| `wav2lip_gan.pth` | github.com/Rudrabha/Wav2Lip releases | `tools/Wav2Lip/checkpoints/` |
| `GFPGANv1.4.pth` | github.com/TencentARC/GFPGAN releases | `tools/Wav2Lip/gfpgan_weights/` |

Do NOT overwrite `tools/Wav2Lip/inference.py` with the upstream version — the one in this repo is patched (face-less frame handling). Only the weight files are downloaded.

**ProPainter** (subtitle erasing, ~600 MB, required for the Subtitles tab):
clone https://github.com/sczhou/ProPainter to `tools/ProPainter/`.

**ComfyUI + SD1.5** (Brand Studio image ads, ~5 GB, optional):
download ComfyUI portable for Windows, extract to `ComfyUI_windows_portable/` at the workspace root, and make sure it launches with `--lowvram`. If it ships cu130 torch and you're on an RTX card, downgrade to cu126.

## 4. Configuration

```powershell
copy video-studio\config.json.example video-studio\config.json
```

Then edit `video-studio/config.json`:

- `port` — 5180 unless you have a conflict
- `autovsl_root` — absolute path to your `autoVSL/` folder
- `venvs` — absolute paths to the four interpreters you created in step 2
- `remote_pin` — pick your own 6-digit PIN (phone/LAN access)
- `lan_access` — `true` if you want to drive it from your phone

If you'll use **paid fal.ai dubs**: create `autoVSL/.env` containing your key as `FAL_KEY=...` (get one from fal.ai). Skip this entirely to stay on the free local engines.

Never commit `config.json` or `.env` — both are gitignored on purpose.

## 5. Launch

```powershell
cd video-studio\app
..\..\autoVSL\.venv\Scripts\python server.py
```

Open **http://localhost:5180**. Localhost skips the PIN; other devices on your Wi-Fi get the PIN screen.

## 6. Verify the install

1. **Library tab** loads without errors → server + main venv OK.
2. Upload a short talking-head clip in **New Project** → transcription completes → whisper venv OK.
3. Run a **local dub** (Dubbing tab, "local" engine, $0) → XTTS + Wav2Lip + weights OK. First run downloads the XTTS model (~2 GB) automatically.
4. Run **Captions** on the result → the full free pipeline works end to end.

## 7. Before you write any code

Read these two documents, in this order:

1. `PROJECT-SUMMARY.md` — architecture, the workdir data structure, and **§8 "hard-won gotchas"**. Every rule in there (no `-shortest`, one GPU job at a time, frame-count verification) exists because of a real production failure. Do not re-learn them the hard way.
2. `docs/REFACTOR-PLAN.md` — where the codebase is heading and the commit discipline (pure-move vs pure-change commits).

## Troubleshooting quick hits

| Symptom | Fix |
|---|---|
| `bash: .venvScriptsactivate: command not found` | You're in Git Bash — use `source .venv/Scripts/activate` (forward slashes) |
| WinError 4551 on numba/llvmlite | Turn off Windows Smart App Control (step 0) |
| "Face not detected" during lip-sync | Expected on end-cards; the patched inference handles it. "…in ANY frame" means the video truly has no face |
| "Module torch not found" in dubbing | Step 2c's torch install failed or used the wrong CUDA index URL — re-run it |
| Everything dub-related fails at once | Two GPU jobs ran concurrently and OOM'd the card — never bypass the GPU lock |
| ComfyUI won't start | Launch with `--lowvram`; check torch is cu126 not cu130 |
