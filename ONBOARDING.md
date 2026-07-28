# Onboarding — Setting up Video Studio after cloning

Two scripts do the work: **`setup.py`** installs everything, **`doctor.py`** tells you
what is still wrong. Read §0, run two commands, then work through whatever the doctor
reports. Most of the wall-clock time is downloads (~10 GB).

```powershell
git clone <this-repo> "Video AI editing"
cd "Video AI editing"
python setup.py          # creates config + 4 venvs + weights, GPU-matched
python doctor.py         # verifies; prints a fix command per failure
```

Both are idempotent — safe to re-run, they skip finished work. `setup.py --list`
shows the individual steps; `--only dub` runs one; `--skip-optional` omits the
optional tabs.

## 0. Prerequisites

- **Windows 10/11** (Windows-first; paths and scripts assume it)
- **NVIDIA GPU.** 8 GB is comfortable; 4 GB works (the codebase is full of 4 GB
  workarounds). CPU-only runs, but dubbing is unusably slow.
- **Python 3.12 AND 3.14** (or just 3.12). 3.12 is *required* for the dubbing venv:
  `numpy<2.0`, which coqui-tts needs, has no wheels for 3.13/3.14. Other venvs are
  fine on either. `setup.py` picks the right base interpreter per venv.
- **Git** (Git Bash included)
- **ffmpeg** — `winget install Gyan.FFmpeg`, then open a new shell so PATH updates.
  The app auto-discovers it (PATH → WinGet → common dirs); set `ffmpeg_bin` in
  config.json only if discovery fails.
- **Claude CLI** (`claude`) — script rewriting and the DubSync vision advisor call
  `claude -p` headlessly. Install Claude Code and sign in once.
- **Windows Smart App Control must be OFF** (Settings → Privacy & security → App &
  browser control). It blocks unsigned DLLs (llvmlite/numba) with WinError 4551 and
  the dubbing venv will not load.

Disk: budget ~25 GB. The venvs alone are ~13 GB.

## 1. What `setup.py` builds

| Step | Creates | Notes |
|---|---|---|
| `config` | `video-studio/config.json` | paths resolved to your checkout; random PIN + secret |
| `cv` | `autoVSL/.venv` | Flask server + repair engines (flask, opencv, numpy, scipy, pillow) |
| `whisper` | `course_pipeline/.venv` | faster-whisper. **No torch** — it runs on CTranslate2 |
| `dub` | `dubbing-studio/venv` | Python 3.12; torch + coqui-tts + gfpgan |
| `weights` | Wav2Lip + GFPGAN `.pth` | ~750 MB, size-verified |
| `vsr` *(optional)* | `tools/vsr/.venv` | Power Tools upscale |
| `propainter` *(optional)* | `tools/ProPainter` | Subtitle Recovery tab |

**ComfyUI + SD1.5** (Brand Studio, ~5 GB) is not automated: download ComfyUI portable
for Windows, extract to `ComfyUI_windows_portable/` at the workspace root, and launch
it with `--lowvram`. If it ships cu130 torch on an RTX card, see the GPU note below.

Not installed by `setup.py`: **`autoVSL/.env`** with `FAL_KEY=...` for paid fal.ai dubs.
Skip it entirely to stay on the free local engines. Never commit it, or `config.json`.

## 2. The GPU/CUDA rule (the one that bites)

`setup.py` reads your compute capability from `nvidia-smi` and installs
**torch 2.8.0 / torchaudio 2.8.0 / torchvision 0.23.0 on cu128**. That single pin is
deliberate and covers `sm_61`–`sm_120` (Pascal → Blackwell):

- **cu121 / cu126 have no `sm_120` kernels.** On an RTX 50-series card
  `torch.cuda.is_available()` still returns `True` — it only fails at the first kernel
  launch with *"no kernel image is available for execution on the device"*. This is why
  `doctor.py` runs a real matmul instead of trusting the flag.
- **torchaudio ≥ 2.9 breaks the voice stage.** It routes `.load()` through torchcodec,
  which needs FFmpeg *shared* libraries; the Gyan `full_build` above is static (3 exes,
  no DLLs). XTTS then dies mid-synthesis inside `load_audio()`.

If you edit these pins, `doctor.py` will catch both failure modes before a job does.

## 3. Launch

```powershell
cd video-studio\app
..\..\autoVSL\.venv\Scripts\python server.py
```

Open **http://localhost:5180**. Localhost skips the PIN; other devices on your Wi-Fi
get the PIN screen (the PIN is in `config.json`).

## 4. Verify

`python doctor.py` should report **0 fail**. Then exercise the free pipeline end to end:

1. **Library** loads → server + cv venv OK
2. Upload a short talking-head clip in **New Project** → transcript appears → whisper OK
3. **Transcript** tab → Claude rewrite → saves `script-edited.txt` → Claude CLI OK
4. **Dubbing** tab, engine `local`, $0 → XTTS + Wav2Lip + weights OK
   (first run downloads the XTTS model, ~1.9 GB)
5. **Captions** on the result → the whole free chain works

Fit the script to the footage. XTTS speaks slower than most source audio, so a
verbatim transcript usually overruns the video and Wav2Lip pads frames to match the
audio. The rewrite in step 3 sizes word count to duration — that is what it is for.

## 5. Tuning the AI prompts

Every LLM prompt — script rewrite, clone, VSL shot list, QC review, DubSync advisor,
caption spell-fix, brand copy, the chat system prompts, course distillation — lives in
`prompts.json` at the workspace root, together with the model and timeout each one uses.

```powershell
python prompts.py                # list all prompts + the {placeholders} they take
python prompts.py copy_rewrite   # print one in full
```

Edit the text in place and the next job picks it up — no restart. Keep `{placeholders}`
intact (the code fills them) and leave `{{doubled}}` braces doubled; they are literal
JSON braces in a prompt's output spec. `python prompts.py` (and `doctor.py`) fails loudly
if you break either. For changes you do not want to commit — trying `sonnet` where the
committed default is `opus`, say — put them in `prompts.local.json` (gitignored), which
is merged key-by-key over the committed file.

## 6. Before you write any code

1. `PROJECT-SUMMARY.md` — architecture, the workdir data structure, and §8
   **"hard-won gotchas"**. Every rule there (no `-shortest`, one GPU job at a time,
   frame-count verification) exists because of a real production failure.
2. `docs/REFACTOR-PLAN.md` — where the codebase is heading and the commit discipline
   (pure-move vs pure-change commits).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `python doctor.py` says a config path is dead | The checkout moved. Delete `video-studio/config.json` and re-run `python setup.py --only config` |
| "no kernel image is available for execution" | torch has no kernels for your GPU — see §2 |
| `ImportError: TorchCodec is required for load_with_torchcodec` | torchaudio ≥ 2.9 — pin the §2 trio |
| `No module named 'torchvision.transforms.functional_tensor'` | basicsr vs torchvision ≥ 0.17. `python setup.py --only dub` re-applies the patch (it lives in site-packages and does not survive a venv rebuild) |
| `No module named 'TTS'` but the venv exists | Likely a stale venv from another machine — check `Lib/site-packages` for `cp310`-tagged `.pyd` files in a 3.12 venv. Delete the venv folder and re-run `setup.py --only dub` |
| WinError 4551 on numba/llvmlite | Turn off Windows Smart App Control (§0) |
| "Face not detected" during lip-sync | Expected on end-cards; the patched inference handles it. "…in ANY frame" means the video truly has no face |
| Everything dub-related fails at once | Two GPU jobs ran concurrently and OOM'd the card — never bypass the GPU lock |
| `bash: .venvScriptsactivate: command not found` | You're in Git Bash — use `source .venv/Scripts/activate` |
| ComfyUI won't start | Launch with `--lowvram`; check its torch matches your GPU (§2) |
