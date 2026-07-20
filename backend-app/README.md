# backend-app

New modular Flask backend for Video Studio. Replaces the 3,649-line monolith at
`video-studio/app/server.py` via a phase-by-phase split (see
[`docs/REFACTOR-PLAN.md`](../docs/REFACTOR-PLAN.md)).

## Status: Phase 1 — scaffold

This folder is currently a 6-file boot checkpoint: Flask app factory with one
route (`GET /` returns `Server Online.`). No blueprints, no services, no engine
wrappers yet. Subsequent commits add one blueprint at a time (Rule 5.2).

## Layout (current)

```
backend-app/
├── .gitignore
├── .venv/                     (created by `uv venv`, gitignored)
├── README.md
├── app.py                     Flask app factory + index route
├── env.example                PORT/HOST/FLASK_ENV template
├── requirements.txt           Flask only (pydantic lands late Phase 1)
└── wsgi.py                    Entry point
```

## Run

```bash
cd backend-app
uv venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe wsgi.py
# browse to http://127.0.0.1:5181/  →  "Server Online."
```

## Read-only boundary (per `docs/BACKEND-REFACTOR-RULES.md` §2)

The new `backend-app/` may call scripts in the following sibling trees via
**subprocess only** (never `import`). Each dependency's role:

| Sibling tree | What we call from it |
|---|---|
| `autoVSL/` | `dashboard/dub.py`, `dashboard/local_dub.py`, `dashboard/caption.py`, `scripts/script-swap.py` — paid/free dub, captions, fal.ai pipeline |
| `subtitle-studio/` | `erase_subs.py` (ProPainter AI inpaint), `subclean.py` (OpenCV cleaner), `recaption.py` (faster-whisper → ASS) |
| `dubbing-studio/` | `lipsync.py` — local Wav2Lip + GFPGAN/CodeFormer (weights live in `tools/Wav2Lip/`) |
| `course_pipeline/` | only the `.venv` (faster-whisper CUDA) |
| `tools/` | `tools/Wav2Lip/`, `tools/ProPainter/`, `tools/vsr/` — vendored model code; engines in `tools/Wav2Lip/inference.py` is locally patched |
| `CodeFormer/` | face-restore weights (gitignored) |
| `ComfyUI_windows_portable/` | SD1.5 image gen for Brand Studio (launched externally, called via HTTP) |

Engines stay **byte-identical** during the refactor (Rule 10). Every weird
thing in them was earned by a real failure; re-implementing is a multi-week trap.
