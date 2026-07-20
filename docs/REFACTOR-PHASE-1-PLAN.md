# Phase 1 — server.py Split Plan

> **Source of truth:** `docs/REFACTOR-PLAN.md` (the plan) and
> `docs/BACKEND-REFACTOR-RULES.md` (the discipline). This file is the
> working roadmap for Phase 1 specifically — what to extract, in what
> order, and how to interpret "pure move" for this codebase.
>
> **Scope reminder:** `/video-studio/` and `/backend-app/` only. Sibling
> trees (`autoVSL/`, `subtitle-studio/`, `dubbing-studio/`, `tools/`,
> `CodeFormer/`, `ComfyUI_windows_portable/`) are read-only — see
> `backend-app/README.md` for the dependency table.

## 0. The current state

- **App:** `video-studio/app/server.py` — **3,649 lines**, 97 routes, 144 top-level helpers.
- **Job store:** `video-studio/app/jobs.py` — 198 lines, self-contained, can be moved as-is.
- **Engines:** `video-studio/app/engines/` — 6 files (brand_content, compositor, dubsync_repair, frame_swap, object_repair, visual_repair) — **untouchable**, moved byte-identical in commit 2 of Phase 1.
- **New backend:** `backend-app/` — currently the 6-file boot scaffold (Flask factory + "Server Online.").
- **Phase 0 was skipped** (per `.hermes/decisions/phase-1-2026-07-20-skip-phase-0.md`). No `video-studio/.git/`, no `baseline` tag, no recorded golden-run baseline.

## 1. The pure-move interpretation (Rule 5.1) — calling it out

**Question:** is `@app.get("/x")` → `@bp.get("/x")` (a Blueprint decorator) a pure move?

**Answer: yes, with one caveat.** Flask blueprints are the canonical relocation mechanism — `app.register_blueprint(bp, url_prefix=...)` reproduces the same URL → handler mapping the global `app` had, with byte-identical handler bodies. There is no behavior change. The only code that *appears* new is the `bp = Blueprint("x", __name__)` line and the `app.register_blueprint(bp)` call in `app.py`, both of which are required scaffolding for the move to compile.

**The caveat:** if a route body's helper dependencies (e.g. `load_spend()`) are *also* being moved to a service module in the same commit, the route body must update its imports — which is technically a "change" even if the call site is identical. The rule I'll follow:

> **Each commit moves either (a) routes, or (b) a service/helper module — not both in the same commit.** If a blueprint's route body needs `from services.spend import load_spend`, that import comes from a *previous* commit. Helpers stay as `def` calls in `server.py` (delegating to the service) until the route is moved, at which point the import inlines.

This keeps every commit bisectable.

## 2. The boot-check strategy (replacement for the skipped golden run)

Since we have no baseline record, the safety net becomes **per-commit boot + smoke**:

| Step | Command | What it proves |
|---|---|---|
| 1 | `cd backend-app && .venv/Scripts/python.exe wsgi.py &` (background, port 5181) | New app starts |
| 2 | `curl -i http://127.0.0.1:5181/` | New app responds with 200 + "Server Online." |
| 3 | `cd video-studio && autoVSL/.venv/Scripts/python.exe app/server.py &` (background, port 5180) | Old app still starts |
| 4 | `curl -i http://127.0.0.1:5180/api/ping` | Old app responds |
| 5 | For each extracted blueprint: hit the new blueprint's URL on 5181 and compare to the old route on 5180 | The move preserved behavior |

A 5-step smoke takes ~30s. The user runs it (Rule 4: git is yours; this is git-adjacent verification). Output goes in `.hermes/smoke/<date>-<sha>.txt` (gitignored).

## 3. Extraction order (services first, then blueprints)

The plan in `REFACTOR-PLAN.md` says: "Extract cross-cutting services first: `gpu.py`, `spend.py`, `llm.py`, `prompts.py`. Write `workdir.py`. Move routes into blueprints one tab at a time."

The plan's service order is **gpu / spend / llm / prompts / workdir**. Looking at `server.py` global state, I'd reorder slightly for fewer import-coupling surprises — but the plan is the source of truth, so the order below follows the plan with **one small adjustment** flagged in §3.1.

### 3.1 Service extraction (5 commits — `services/` will have 5 files, not 6)

**Decision logged 2026-07-20:** the plan's target-structure diagram shows 6
service files (`jobs`, `gpu`, `spend`, `llm`, `workdir` — and a 6th if
`prompts` is also counted). We ship **5**, merging `gpu.py` into `jobs.py`,
because the GPU symbols are already co-located with the job store today
(see verification below).

**Verification (grep of `video-studio/app/jobs.py`):**

| Symbol | In `jobs.py` today? |
|---|---|
| `GPU_LOCK` (threading lock) | yes (L32) |
| `needs_gpu()` (command detector) | yes (L122) |
| `foreign_erase_running()` (cross-app arbitration) | yes (L135) |
| `wait_for_gpu()` (queue + responsive-to-Stop) | yes (L151) |
| `acquire_gpu()` (take the lock) | yes (L167) |
| `_awake_keeper()` (Windows keep-awake) | yes (L178) |
| `SetThreadExecutionState` (Windows API call) | yes (L187) |

`server.py` imports all of these from one place (L154–155):

```python
from jobs import (jobs, jobs_lock, GPU_LOCK, needs_gpu, wait_for_gpu,
                  acquire_gpu, init as jobs_init)
```

After S1, a blueprint writes `from services.jobs import wait_for_gpu, …` —
identical to today's import, one path segment longer. No facade, no
re-export, no second module that shares a lock with the first.

**Each commit = one service file in `backend-app/services/`.** Helpers in
`server.py` become thin shims (1-line forwarders) until the blueprint that
uses them is moved; the shim is deleted in the blueprint's move commit.

| # | Commit | New file | What moves from `server.py` | Notes |
|---|---|---|---|---|
| S1 | Move job store | `backend-app/services/jobs.py` | entire `video-studio/app/jobs.py` (198 lines, byte-identical) | The `jobs_init()` call moves to the new app factory. |
| S2 | Move prompts | `backend-app/services/prompts.py` | `COPY_PROMPT` constant (L27–40), `CLONE_PROMPT` if present, `QC_PROMPT` (L1774–1808) | Constant-only move. Routes import `from services.prompts import COPY_PROMPT`. |
| S3 | Move spend | `backend-app/services/spend.py` | `FAL_SPEND_FILE`, `spend_lock`, `TTS_RATE_PER_1K`, `MINIMAX_CLONE_FEE`, `LIPSYNC_RATE_PER_SEC`, `load_spend()`, `estimate_dub_cost()`, `record_spend()` (L189–254) | Helper-heavy. Imports `Path`, `time`, `json`, `threading`. |
| S4 | Move LLM | `backend-app/services/llm.py` | `CLAUDE_EXE` resolution (L22–25), `chats`/`chats_lock` state, `run_chat_turn` worker, `inspiration_block()` (L712–734), `load_bank_entry()` (L695–709) | The "pop CLAUDECODE" rule lives HERE per the plan, not duplicated per route. |
| S5 | Move workdir | `backend-app/services/workdir.py` | (NEW abstraction) — class owning `output/script-swap/<stem>/` paths + the methods listed in Rule 8.3 | The "one new abstraction" the plan calls out. Written in this commit. |

### 3.2 Blueprint extraction (15+ commits, one blueprint per commit)

Ordered from smallest/safest to largest/most-coupled. The plan says "same order as the UI migration: library/exports first, dubsync last" — so the order below is also the migration order for Phase 3.

| # | Blueprint | Routes | LOC range in server.py | Risk | Depends on services |
|---|---|---|---|---|---|
| B1 | `routes/auth.py` | `/login`, `/api/login`, `/api/logout`, `/api/ping` + `_require_pin` before_request + `_is_local` | L100–149 (50) | low | — |
| B2 | `routes/pages.py` | All 16 `*_page()` route functions + `static_files()` | L3448–3568 (120) | low | — |
| B3 | `routes/jobs.py` | `/api/jobs`, `/api/job/<id>`, `/api/job/<id>/stop`, `/api/job/<id>/resume`, `job_progress()` | L346–423 + L604–652 (130) | low | jobs (S1) |
| B4 | `routes/library.py` | `/api/overview`, `/api/bank/<name>`, `uploads_state()` | L1654–1741 (90) | medium | — (uses globals) |
| B5 | `routes/exports.py` | `/api/exports`, `/api/exports/send` | L3146–3237 (95) | low | — |
| B6 | `routes/scripts.py` | `/api/script/<stem>` (GET+POST), `/api/upload`, `/api/upload` (DELETE), `/api/transcript-to-product`, `transcript_plain_text()` | L657–765 + L915–950 (140) | medium | — |
| B7 | `routes/dubbing.py` | `/api/run` (only the `dub` and `transcribe` branches; the rest is creator), `/api/dub-promote`, `/api/dubs`, `dub_versions()`, `run_dub_job()` | L429–563 (subset) + L1576–1653 + L2395–2427 (180) | high | jobs, spend, llm (S1, S3, S4) |
| B8 | `routes/captions.py` | `/api/recaption`, `/api/captions/<stem>` (GET+POST), `/captioned/<stem>`, `/api/aifix/<stem>` | L2276–2394 (120) | medium | — |
| B9 | `routes/subtitles.py` | `/api/clean-preview`, `/api/clean-subs`, `/api/upload` (DELETE if not in scripts), `/api/clean-restore`, `clean_subs_worker()`, `ffmpeg_exe()` | L879–1086 (210) | medium | jobs (S1) |
| B10 | `routes/clone.py` | `/api/clone/*` (5 routes), `clone_actor_video()`, `winner_script()` | L2949–3145 (200) | high | jobs, spend, llm |
| B11 | `routes/dubsync.py` | `/api/dubsync/repair`, `/api/dubsync/visual-preview`, `/api/dubsync/advise`, `/api/dubsync/upload`, `_advise_frames()`, `_sparse_thumbs()`, `_video_meta()`, `_find_original()` | L2428–2948 (520) | **highest** | jobs, llm + lots of helpers |
| B12 | `routes/chat.py` | `/api/chat`, `/api/chat/<turn_id>`, `/api/copywrite` | L1306–1491 (190) | medium | llm, prompts (S4, S2) |
| B13 | `routes/qc.py` | `/api/qc/*` (7 routes) | L1896–2153 (260) | high | llm (for ai-review) |
| B14 | `routes/brand.py` | `/api/brand/*` (8 routes) + `/brand-studio` page + `/brand-out/<rel>` + `load_brand_kit()` | L3238–3461 (225) | medium | — |
| B15 | `routes/creator.py` | `/api/creator/*`, `/api/file`, `/api/output*`, `/api/trash/*`, `/api/product/*`, `/api/research-doc/*`, `/api/build-vsl`, `/api/agent-note`, `/api/edit`, `/api/thumb`, `/media/<rel>`, `/studio*` | L1087–1305 + L1742–1895 + L2194–2275 (560) | **highest** | jobs, spend, llm |
| B16 | `routes/tools.py` | `/api/edit` (already in B15? reconsider) | — | low | — |

> **B15/Creator is the biggest cluster (560 lines, 15+ routes).** Per Rule 5.2, it should be one blueprint per commit. If a single commit would exceed ~400 LOC of moved code, split it into Creator-core (research-doc, product, bank) and Creator-output (trash, output-rename, export-to-desktop). Flag in decisions log when we get there.

### 3.3 Engines + final cleanup (2 commits)

| # | Commit | What | Notes |
|---|---|---|---|
| E1 | Move engines (byte-identical) | `backend-app/engines/` ← `video-studio/app/engines/*` (6 files) | Rule 10: engines are untouchable. `cp -r` only. Then `app.py` updates its engine path imports. |
| E2 | Delete the old server | Delete `video-studio/app/server.py`. Update any `run_*.bat` launcher in the repo to point at `backend-app/wsgi.py`. | End of Phase 1. Tag `phase-1` (per Rule 4: user does git; I'll propose the tag command). |

### 3.4 End of Phase 1 (per Rule 11.1)

One final commit, **separate from the above**:

| # | Commit | What |
|---|---|---|
| P | Add Pydantic | `requirements.txt` adds `pydantic` + `flask-pydantic`. New `backend-app/schemas/` directory. Per-blueprint request/response models. `docs/PYDANTIC-MIGRATION.md`. |

This is one commit, not per-blueprint, so bisectability is preserved.

## 4. Estimated total commits in Phase 1

| Section | Commits |
|---|---|
| Services (3.1) | 5 |
| Blueprints (3.2) | 16 (B1–B16) |
| Engines + cleanup (3.3) | 2 |
| Pydantic (3.4) | 1 |
| **Total** | **24 commits** |

At ~30s per smoke check + ~5 min per commit, Phase 1 is roughly **2 hours of focused work**, not counting debugging.

## 5. What this plan is NOT

- **Not a behavior change.** No route returns different data; no engine is rewritten; no schema migrates. Pure relocation + glue.
- **Not a frontend change.** The HTML tabs in `video-studio/app/static/` keep working. They call `/api/...` and as long as the URL mapping is preserved (via `register_blueprint(bp)` with no `url_prefix` change), nothing on the UI changes.
- **Not a test suite.** Pytest is Phase 4. The smoke check (§2) is the safety net.

## 6. Decisions made 2026-07-20 (3 picked, 0 deferred)

1. **Skip `services/gpu.py` — Option A.** See
   `.hermes/decisions/phase-1-2026-07-20-skip-gpu-py.md` for the
   verification. 5 service files, not 6.
2. **B11 DubSync (520 LOC) and B15 Creator (560 LOC) stay as single
   commits — Option A.** Their route bodies share helpers tightly
   (`_advise_frames`, `_sparse_thumbs`, `_find_original` for B11;
   `soft_delete`, `trash_state`, `safe_output_path` for B15). Splitting
   would either leave helpers in `server.py` (smell) or duplicate them
   across blueprints (anti-pattern). One large commit per cluster is
   bisectable at the "did X work?" granularity, which is enough.
3. **Smoke output in `.hermes/smoke/` — Option A.** Gitignored, private
   debugging artifact. Matches the `.hermes/decisions/` precedent
   (Rule 13: "private, not a public artifact"). The plan documents
   the *strategy*; the smoke files are the *evidence* and don't need
   to be public.

## 7. The first concrete commit

**B1 — `routes/auth.py`** is the proposed first move. Reasons:

- Smallest cluster (50 LOC, 4 routes + 1 `before_request`).
- No global state dependencies (uses `CONFIG` once, `session`, and a couple of constants).
- Verifiable in <30s: hit `/login`, `/api/login`, `/api/logout`, `/api/ping` on 5181 and confirm same behavior as 5180.
- Tests the registration plumbing end-to-end (Blueprint + `register_blueprint` + `url_prefix`).

The proposed commit message format follows Rule 5.4:

> **Extract auth + before_request from server.py to routes/auth.py**
>
> Pure move, no behavior change. Affects lines 100–149 of server.py.
> Owns the PIN gate (`_require_pin` before_request), the login page, the
> `/api/login` + `/api/logout` endpoints, and `/api/ping` (the unauthenticated
> health check that mobile clients poll). Reads `REMOTE_PIN` from `app.config`
> (populated by the app factory) instead of the module-level constant.

---

**This plan is yours to amend.** If you want to:

- Reorder anything,
- Split B11/B15 into smaller commits,
- Re-instate Phase 0 (git init video-studio/) before starting B1,
- Or change the smoke-check storage location,

say so and I'll update this file. Otherwise: confirm and I'll prepare B1.
