# Video Studio — Refactor Plan

**Goal:** A maintainable codebase — modular Flask backend, modern SvelteKit frontend, clean separation of code vs data — without touching the working AI engines.

**Stack decision:** SvelteKit + Tailwind CSS + shadcn-svelte (frontend) · Flask blueprints (backend) · no changes to engines or venvs.

**Architecture map:** [excalidraw diagram](https://excalidraw.com/#json=8MiyWyt4InPnhvXmi3XRE,CptBwq0x-FhLZAE-etYFow)

---

## Guiding principles

1. **Never touch the engines.** `object_repair.py`, the patched Wav2Lip, ProPainter wrappers, and the 4-venv split stay exactly as they are. Every weird thing in there was earned by a real failure.
2. **Move code, don't improve it.** Every commit is either a pure move or a pure change — never both. This keeps the history bisectable.
3. **Invariants survive every phase:** one GPU job at a time · `-frames:v` (never `-shortest`) · `confirm_cost` gate on every fal.ai call · frame-count verification on every engine output.
4. **Golden run after every phase.** Tag each phase completion in git so rollback is one phase, never back to zero.

---

## Target structure

```
video-studio/
├── backend/
│   ├── app.py               ← Flask app factory (~50 lines)
│   ├── config.py            ← sole reader of config.json
│   ├── routes/              ← one blueprint per tab:
│   │                          auth, library, dubbing, clone, dubsync,
│   │                          captions, subtitles, brand, exports, tools
│   ├── services/
│   │   ├── jobs.py          ← job store, resume, process mgmt (exists today)
│   │   ├── gpu.py           ← GPU lock + cross-app arbitration
│   │   ├── spend.py         ← fal.ai cost tracking + confirm_cost gate
│   │   ├── llm.py           ← claude-CLI wrapper (pops CLAUDECODE once, here)
│   │   └── workdir.py       ← NEW: owns the workdir file layout
│   ├── engines/             ← repair engines, moved as-is
│   └── prompts.py           ← COPY_PROMPT, CLONE_PROMPT
├── frontend/                ← SvelteKit + Tailwind + shadcn-svelte
└── config.json

data/ (today: autoVSL/)      ← uploads/, workdirs/, banks/ — pure data, no code
```

---

## Phase 0 — Safety net *(≈1 hour — do this first)*

- [ ] `git init` inside `video-studio/`, commit everything, tag `baseline`
- [ ] Commit autoVSL's ~31 pending changes (contains the Wav2Lip patch + caption fallback — must not be lost)
- [ ] Write a **golden-run script**: app boots → `/api/jobs` responds → one cheap operation on an existing workdir (caption re-run or remux) → output is byte-valid with the correct frame count
- [ ] Run the golden run once and record the result as the baseline

## Phase 1 — Split `server.py` *(backend, structure only, no behavior change)*

The 3,700-line `server.py` is the biggest maintainability debt.

- [ ] Extract cross-cutting services first: `gpu.py`, `spend.py`, `llm.py`, `prompts.py`
- [ ] Write `workdir.py` — the one new abstraction: a class owning the workdir layout (`final.mp4`, `script-edited.txt`, `new-vo.mp3`, `versions.json`, …). Today these filenames are string literals scattered everywhere.
- [ ] Move routes into blueprints one tab at a time (same order as the UI migration: library/exports first, dubsync last)
- [ ] Golden run passes → tag `phase-1`

## Phase 2 — Separate code from data

`autoVSL/` is currently three things at once: a venv, engine scripts, and the data root.

- [ ] Move `dashboard/local_dub.py`, `dub.py`, `caption.py`, `scripts/script-swap.py` into the backend repo
- [ ] Reference `uploads/`, `output/script-swap/`, `banks/` only via `config.json` (`data_root`) — **no physical renames** (stage caching + `source.txt` absolute paths depend on current locations)
- [ ] Venvs stay where they are (referenced by path in config)
- [ ] Golden run passes → tag `phase-2`

## Phase 3 — Frontend rewrite (SvelteKit + Tailwind + shadcn-svelte)

Architecture: SvelteKit SPA (`adapter-static`, `fallback: index.html`), served by Flask in production — one process, one port (5180), PIN gate and LAN phone access unchanged. Dev mode: vite on 5173 with `/api` proxy → 5180 for hot reload.

**3a — Scaffold**
- [ ] `npx sv create frontend` (Svelte 5 + TypeScript) · `npx sv add tailwindcss` · `npx shadcn-svelte@latest init`
- [ ] Configure adapter-static + vite proxy

**3b — Foundation (build before any tab)**
- [ ] Typed API client (`lib/api.ts`)
- [ ] Job-polling store (`lib/stores/jobs.svelte.ts`) — replaces copy-pasted polling in every HTML file
- [ ] Layout shell (`+layout.svelte`) — replaces `vs-nav.js`: sidebar, spend tracker, GPU-busy indicator
- [ ] Shared components: `JobLogViewer`, `VideoCard` (thumbs), `CostConfirmDialog` (fal money gate → shadcn AlertDialog)

**3c — Migrate tabs, easiest first** *(old HTML keeps working during migration)*
- [ ] Exports, Library — read-only lists, validates the foundation
- [ ] New Project, Transcript, Captions — forms + one job each
- [ ] Dubbing, Clone Winner — job orchestration + cost gate
- [ ] DubSync Repair, QA Review — hardest: custom video player (frame-range marking ⏺/⏹, box drawing, chat advisor). No shadcn equivalent — custom Svelte work, port last
- [ ] Remote — fold into responsive design instead of a separate phone page

**3d — Cutover**
- [ ] Flask serves `frontend/build/` as static fallback, keeps `/api/*`
- [ ] Delete old `static/*.html`
- [ ] Verify PIN/LAN flow **from a phone** (session cookie behavior breaks silently)
- [ ] Golden run passes → tag `phase-3`

## Phase 4 — Lock it in

- [ ] pytest for the route layer with engine subprocess calls mocked (fast, no GPU)
- [ ] Unit test for the frame-count guard — the project's most important invariant
- [ ] Golden run wired up as the E2E check
- [ ] Update PROJECT-SUMMARY.md directory map; move the gotchas (§8) into `docs/`
- [ ] Workspace root cleanup: loose `*.json` / `*.mp3` / `liitt-local-tool.tar.gz` → `data/` or delete

---

## Effort estimate

| Phase | Size | Behavior change |
|---|---|---|
| 0 — Safety net | ~1 hour | none |
| 1 — Backend split | biggest backend chunk | none |
| 2 — Code/data separation | small–medium | none |
| 3 — Frontend rewrite | biggest overall (DubSync player dominates) | UI only |
| 4 — Tests + cleanup | small | none |

The app stays fully usable throughout — Phases 1–2 change no behavior, and Phase 3 keeps the old HTML tabs alive until cutover.

## Key risks

| Risk | Mitigation |
|---|---|
| Losing the uncommitted Wav2Lip patch / caption fallback | Phase 0 commits everything before any change |
| Breaking job resume (stage-cached workdirs) | No physical renames of data paths; golden run per phase |
| Breaking PIN/LAN phone access at cutover | Explicit phone test in Phase 3d |
| Regression hidden inside a "move + improve" commit | Pure-move vs pure-change commit rule |
| GPU OOM from concurrent jobs after route split | GPU lock lives in one service (`gpu.py`); 409 guard is load-bearing |
