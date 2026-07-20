# Video Studio — Backend Refactor Rules

> Scope: `/video-studio/` and its contents only. The refactor plan in
> `docs/REFACTOR-PLAN.md` is the source of truth for **what** we're doing.
> This file is the source of truth for **how** we're doing it.
>
> Anything not in the plan needs a written note in
> `.hermes/decisions/<phase>-<date>-<topic>.md` and explicit confirmation
> before action.

---

## 1. Source-of-truth hierarchy

1. `docs/REFACTOR-PLAN.md` — phases, structure, effort, risks.
2. `docs/BACKEND-REFACTOR-RULES.md` (this file) — working discipline.
3. `video-studio/AGENTS.md` — system knowledge base for the existing app.
4. `video-studio/docs/ARCHITECTURE.md` — target structure detail.
5. `PROJECT-SUMMARY.md` — architect handoff doc.

When two of these conflict, the higher-numbered one wins (REFACTOR-PLAN > RULES > AGENTS > ARCHITECTURE > SUMMARY). If a conflict isn't resolvable that way, stop and ask.

---

## 2. Scope (what is in, what is out)

### In scope — read & write

- `/video-studio/` and everything under it.
- `/backend-app/` — **the destination for the new refactored backend code.** Currently an empty stub (`env.example` only). This is where `app.py`, `routes/`, `services/`, `engines/`, and the new `README.md` land.

### Out of scope — read-only dependencies

- `autoVSL/`
- `subtitle-studio/`
- `dubbing-studio/`
- `course_pipeline/`
- `tools/`
- `CodeFormer/`
- `ComfyUI_windows_portable/`

These are read-only. The new `backend-app/` may call their scripts via subprocess and read their data files, but it may not modify, rewrite, or "improve" anything in them.

The read-only boundary is documented in `backend-app/README.md` (created in Phase 1) with a one-line description of what each dependency provides.

---

## 3. Phases & order

Phases follow `docs/REFACTOR-PLAN.md`:

- **Phase 0** — Safety net (git init, commit pending changes, golden run baseline)
- **Phase 1** — Split `server.py` into blueprints + extract services
- **Phase 2** — Separate code from data
- **Phase 3** — Frontend rewrite (SvelteKit)
- **Phase 4** — Tests, cleanup, lock it in

Within a phase, the order of work is also in the plan. If the plan is silent on order, propose one and ask before doing.

---

## 4. Git discipline

**All git operations are yours.** Commits, pushes, merges, rebases, branch operations, tag operations — you do them. The agent writes code and proposes commit messages; you run git.

The agent will:

- Not stage files.
- Not commit, push, merge, rebase, branch, or tag.
- Not modify git config.
- Not run destructive git operations (`reset --hard`, `clean -fd`, `checkout --`) without explicit confirmation.

The agent will provide, per change:

- A one-paragraph commit message you can paste verbatim.
- The list of files changed and the rough line range.
- A note on whether the change is a "pure move" or a "pure change" (see Rule 5).

---

## 5. Commit discipline

### 5.1 Pure-move vs pure-change

Every commit is one of:

- **Pure move** — code relocated from one file to another with **logic/behavior-identical** execution. No logic change, no behavior change, no bug fix, no comment cleanup. Whitespace and import-order adjustments are allowed (they don't change behavior); a comment edit or a one-character content change is not.
- **Pure change** — behavior or structure change in one place, with the affected code clearly identified.

**Never both in one commit.** This keeps `git bisect` useful.

**Stricter version — engine files (Rule 10):** the `engines/` directory is moved with **byte-identical contents** (no whitespace, no import-order, no comment change). Every weird thing in there was earned by a real failure. Re-implementing is a multi-week trap.

### 5.2 One blueprint per commit

When extracting routes from `server.py` to blueprints, do one blueprint per commit. Splitting 10 blueprints across 10 commits is bisectable; 10 in one is unreviewable.

### 5.3 No drive-by refactors

If extracting route A and route B in the same area shares a helper, do not extract the helper in the same commit. Note it in `.hermes/decisions/`, finish the move, propose the helper extraction as a separate commit later.

### 5.4 Commit message format

The agent provides a one-paragraph message. Suggested format:

```
Extract <route group> from server.py to <blueprint file>

Pure move, no behavior change. Affects lines X–Y of server.py.
<one-line note on what the blueprint owns, if non-obvious>
```

---

## 6. The 16 bugs in `video-studio/AGENTS.md` §10

**Do not fix during the refactor.** All 16 are deferred to Phase 4, each with its own commit and its own regression test.

If a bug is a **blocker** for a move (the code is structurally broken so it cannot be moved without understanding the bug first):

- Flag it in `.hermes/decisions/phase-N-<date>-<topic>.md` with a one-paragraph note.
- Continue the move without fixing the bug.
- Surface the blocker to you at the end of the move, not mid-move.

If a bug is **not a blocker**, do not raise it during the move. It will be raised in the Phase 4 batch.

---

## 7. Asking before bug fixes

The agent proposes; you decide. No "while I'm in there" fixes.

When the agent notices a bug, smell, or landmine, it batches the ask:

> "There are N things in this file I'd want to fix. Here is the list. Which ones, if any, do you want to do now? The rest are deferred to Phase 4."

The agent does not ask one question per smell. Batch first, ask once.

---

## 8. Separation of concerns

### 8.1 Config from code

- `config.json` (via `services/config.py`) is the **only** place the new code reads machine paths.
- No hardcoded paths in the new `backend-app/` code.
- Existing hardcoded paths in code that hasn't been moved yet stay as-is until that code is touched.

### 8.2 Code from data

- `data/` (the workdirs, uploads, banks, output, jobs) is referenced via `config.json` paths.
- No engine data files (`jobs.json`, `library.json`, `fal_spend.json`, `.trash/index.json`, workdir contents) are restructured or moved.
- `services/workdir.py` is the **only** code that constructs paths inside a workdir.

### 8.3 Workdir layout is owned by one class

`services/workdir.py` owns the filename layout for:

- `output/script-swap/<stem>/` (the dub workdir — `final.mp4`, `script-edited.txt`, `new-vo.mp3`, `versions.json`, etc.)
- `output/recaption/`
- `output/clean-subs/`
- `output/clean-erase/`
- And every other `output/<engine>/<stem>/` folder.

No other file may construct a path inside a workdir. All access goes through `workdir.py`.

The class exposes explicit methods for the read/write operations the engines perform (`promote_repair_take()`, `read_script()`, `write_voice()`, `append_version()`, etc.) — not just filename constants.

### 8.4 No new top-level dependencies

Allowed new deps during the refactor:

- **Pydantic** (added late in Phase 1, see Rule 11)
- **pytest** (added in Phase 4)

Nothing else. If a need arises, stop and ask.

### 8.5 No cross-tree imports

The new `backend-app/` may call scripts in `autoVSL/`, `subtitle-studio/`, etc. **only via subprocess.** It may not `import autoVSL.dashboard.dub` or any other import across the read-only boundary.

Subprocess is the boundary. This keeps the engines replaceable.

---

## 9. No schema migration during the refactor

The on-disk shape of every persistent file is frozen for the duration of the refactor:

- `jobs/jobs.json`
- `jobs/<id>.log`
- `library.json`
- `output/fal_spend.json`
- `.trash/index.json`
- `autoVSL/output/script-swap/<stem>/*` (workdir contents)
- All other `output/<engine>/<stem>/*` folders

If new fields are needed, add them with safe defaults. Do not rename, restructure, or version existing fields. Do not move files. Phase 2's "code from data separation" is about **where the code that reads this data lives**, not about relocating the data itself.

---

## 10. Engines are untouchable

- `video-studio/app/engines/` is moved as a directory to `backend-app/engines/` with byte-identical contents.
- The patched Wav2Lip `inference.py`, the 4-venv split, the ProPainter wrappers, the XTTS pinning — all stay.
- Every weird thing in there was earned by a real failure. Re-implementing is a multi-week trap.

---

## 11. Pydantic / type discipline

### 11.1 Pydantic — one final commit at the end of Phase 1

Do not add Pydantic during the first blueprint extractions. The first half of Phase 1 is pure moves. Once all blueprints are extracted and stable, Pydantic is added in **one final commit** that introduces `flask-pydantic`, the `schemas/` directory, and the per-blueprint request/response models.

Rationale: per-blueprint Pydantic commits would mix "pure move" with "wrap with model" in the same commit, violating Rule 5.1. The single end-of-Phase-1 commit keeps each blueprint's extraction bisectable.

Suggested approach: `flask-pydantic` for request/response validation, with one model per route's request and one per response. Add `docs/PYDANTIC-MIGRATION.md` when starting this work.

### 11.2 Type hints

Scope of type hints during the refactor:

- **New code in `backend-app/`** — function signatures + return types, no annotations inside function bodies. Fast, useful at the boundary, doesn't slow extraction.
- **Moved code** — stays untyped. The type hint is added later, in its own commit, when the code is in its final home.

Do not type-hint moved code during the move. Type hints are a separate concern, a separate commit.

---

## 12. Testing discipline

### 12.1 Golden run — per commit, not per phase

The plan says "golden run after every phase." With one-blueprint-per-commit discipline, the golden run becomes a per-commit safety net. The script lives in the repo (created in Phase 0). You run it; output goes in `.hermes/golden-runs/<sha>.txt`.

A 30-second script before each commit is the difference between "I broke something 8 commits ago and don't know which one" and "I broke something this commit, here's the diff."

### 12.2 Phase 0 baseline is a real artifact

The first golden run output is saved to `docs/golden-runs/phase-0-baseline.txt` and committed. Every subsequent run is diffed against the baseline. A change in golden run output without a corresponding code change is a signal that something else is wrong (config drift, dep drift, environment drift).

### 12.3 Phase 4 is when real tests land

Pytest is added in Phase 4. The route layer is tested with engine subprocess calls mocked (fast, no GPU). The frame-count guard, the 16 bug regressions, and the workdir contract get tests in Phase 4.

The one exception: if a test is needed to make a Phase 0 / Phase 1 commit safe (e.g., a smoke test for the app factory before any blueprint is registered), add it as its own commit with a written note in the decisions log.

---

## 13. Decisions log

`.hermes/decisions/` is the agent's breadcrumb trail. It is **gitignored** — private, not a public artifact.

Every entry follows this format:

```markdown
# <phase> — <date> — <topic>

## What
<one paragraph: what happened / what was done>

## Why
<one paragraph: why this was needed / why it deviated from the plan>

## Agreed
<one paragraph: what was agreed with the user, or "not yet discussed">
```

When the agent deviates from the plan, makes a structural choice the plan didn't anticipate, defers a bug, or hits a blocker, it writes one of these before continuing. The user can promote specific entries to `docs/` later if they want them public.

---

## 14. The new `backend-app/` is the only place new code is written

No new modules under `video-studio/app/`. No new routes added to `server.py`. The moment Phase 1 starts, the monolith is frozen — it only loses code (via moves) and gains nothing.

If a feature is needed during the refactor, it goes in `backend-app/` even if the matching blueprint hasn't been extracted yet. This prevents a "Phase 0.5 add a new route to the old server" anti-pattern.

---

## 15. Asking for confirmation

The agent always asks for confirmation when:

- The action deviates from `REFACTOR-PLAN.md` in any way.
- A bug, smell, or landmine is noticed (batched, see Rule 7).
- A new dependency is needed.
- A schema change is needed.
- A code change touches anything outside `/video-studio/`.
- A decision has meaningful trade-offs the user should weigh in on.

The agent does not ask for confirmation on:

- Pure-moves that follow the plan.
- Reading files, searching, running existing tests, running the golden run.
- Routine blueprint extractions within the agreed phase scope.
- Decisions that are reversible and have an obvious default (the agent picks the default and notes it in the decisions log).

---

## 16. Old-code immutability during refactor (the hard "do not touch" rule)

**During a refactor phase, the agent may only write/modify/delete files in the *new* (refactor-side) tree. The *old* (monolith) tree is fully off-limits — no edits, no deletions, no path shims, no scaffolding, no "I-just-need-to-add-3-lines-to-make-the-import-work."**

### Definitions

- **Old / monolith side** — the code that's being replaced. In Phase 1, that is `video-studio/app/` (the existing `server.py`, `jobs.py`, `engines/`, etc.) and anything else the refactor is moving away from.
- **New / refactor side** — the destination of the move. In Phase 1, that is `backend-app/` (the new `services/`, `routes/`, `engines/`, `app.py`).

### What this means in practice

| Action | Old side | New side |
|---|---|---|
| Write a new file | ❌ never (use new side instead) | ✅ allowed |
| Modify an existing file | ❌ never | ✅ allowed (only files that landed in the new side as part of the refactor) |
| Delete a file | ❌ never (old files stay until the user removes them) | ✅ allowed (refactor can clean up its own tree) |
| Move bytes old → new | ✅ via copy on the new side; old file is left in place | ✅ allowed |
| Add a sys.path shim, env-var injection, or any other "make the import work" scaffolding | ❌ never (this is a modification of the old side by another name) | ✅ if the shim lives in the new side and the old side is untouched |
| Touch `server.py` to update an import path | ❌ never — even 1 line counts as a modification | n/a |

### Why this rule exists

The refactor's correctness comes from being able to compare old and new at every commit. If the agent edits the old side (even to "just add a sys.path shim"), the diff is no longer "old unchanged, new added" — it's "old changed, new added" — and `git bisect` can no longer tell which side introduced a regression.

The cost of following this rule: a few "the plan said swap the import but I can't without modifying the old side" moments. The cost of not following it: an unrecoverable change to the live monolith that may take the running app down.

### The "old code" itself doesn't move

When the refactor replaces a file (e.g. `video-studio/app/jobs.py` → `backend-app/services/jobs.py`):

1. The new file is **copied** to the new side (byte-identical).
2. The old file **stays on disk** in its original location.
3. Imports in the old code **continue to point at the old file** until the user (not the agent) decides to do the swap.
4. The old file is removed **only by the user**, as part of a commit the user writes.

This is true even if the agent is 100% sure the old file is dead code. "Dead code" is the user's call to clean up, not the agent's.

### What the agent does when blocked by this rule

If the plan calls for an action that requires modifying the old side (e.g. "swap the import in `server.py` to point at the new file"), the agent:

1. **Stops** before touching the old side.
2. **Reports the blocker** to the user: "the plan says X, but X requires editing `server.py`; the old-side-immutability rule forbids that; here are N ways to resolve it."
3. **Writes a decision-log entry** at `.hermes/decisions/phase-N-<date>-<topic>.md` describing the blocker.
4. **Waits** for the user to either (a) re-authorize the old-side edit explicitly, (b) approve a plan amendment, or (c) pick a different path.

The agent does **not** proceed with the old-side edit and "explain later." The agent does **not** split the edit into smaller old-side edits to make each one feel trivial.

### Out of scope

This rule applies to the refactor phase only. Outside an active refactor phase, normal development (new features, bug fixes) follows the project's normal git workflow — old code is modifiable as usual.

---

## 17. OOP discipline

The goal is **scalable and maintainable**, which means clear, scannable, single-concern code. OOP is a tool, not a default. Use classes only when one of the following is true:

- The concern has **state that persists across calls** (Workdir, JobStore, SpendLedger, GPULock, LLM client).
- There are **several variants of the same thing** (DubWorkdir, RecaptionWorkdir, CleanSubsWorkdir).
- The **state and the behavior are the same concern** (a workdir knows where `final.mp4` is *and* knows how to promote a repair take to it; separating them creates a "where is this method?" question).

Otherwise use functions. Specifically:

- **Route handlers are functions** — Flask idioms, no `MethodView`, no `DubBlueprint` class.
- **Engine wrappers are functions** — the existing `engines/*.py` are scripts with a `main()`; the new wrappers follow the same shape.
- **Subprocess helpers are functions** — `def run_engine(cmd, **kwargs)`, not `class EngineRunner`.
- **Pydantic models are data classes, not behavior classes** — a `DubRequest` model with a `.start()` method is bad. Use `DubRequest` for shape, free `start_dub(req)` function for behavior.
- **No inheritance for engine variants** — `class LocalDubEngine(DubEngine)`, `class FalDubEngine(DubEngine)` looks clean but the engines share almost no code; the base becomes a bag of `NotImplementedError` stubs. Compose functions instead.
- **No service-locator / DI container** — `container.get("workdir_factory")` obscures the data flow. Pass dependencies explicitly.

### Where OOP applies in this refactor

| Module | Phase 1 shape | End-state shape | Why |
|---|---|---|---|
| `services/workdir.py` | **Class** | **Class** | State (paths) + behavior (operations) + variants (Dub/Recaption/CleanSubs). The strongest case for OOP in the project. Written in Phase 1 alongside the first blueprint that needs it. |
| `services/jobs.py` | **Module of functions** (moved as-is from `video-studio/app/jobs.py`) | **Class** (`JobStore`) | Phase 1 is a pure move — same shape as today. The class wrap is **Phase 4 work** (alongside type hints and bug fixes), not part of the move. |
| `services/gpu.py` | **Class** (`GPULock`) | **Class** (`GPULock`) | State (`GPU_LOCK` file, foreign-erase poll thread, keep-awake handle) + lifecycle. |
| `services/spend.py` | **Class** (`SpendLedger`) | **Class** (`SpendLedger`) | State (`fal_spend.json`, balance, `cloned_stems` cache) + many operations. |
| `services/llm.py` | **Class** (`ClaudeRunner`) | **Class** (`ClaudeRunner`) | Shared config (env pop, model, timeout, `--disallowedTools`) across 8+ call sites. |
| `services/config.py` | **Module of functions** | **Module of functions** | "State" is a cached dict loaded once at startup. A class adds no value. |
| `services/subprocess.py` | **Module of functions** | **Module of functions** | Stateless helper, called everywhere. |
| `routes/*.py` | **Module of functions** | **Module of functions** | Flask idioms. Per-request state is `request`, per-app state is `current_app.config`. No class needed. |
| `engines/*.py` (wrappers) | **Module of functions** | **Module of functions** | Thin layer around untouchable engine scripts. |

**Note on the table:** the "Phase 1 shape" column is what gets written during Phase 1. The "End-state shape" column is the target after Phase 4. The "Why" column explains the end-state rationale. The only module whose shape changes between phases is `services/jobs.py` (moved as a function module in Phase 1, wrapped in a class in Phase 4) — every other module's shape is stable from the moment it's written.

### Anti-patterns to avoid

- **`DubBlueprint` class** with `start()`, `stop()`, `status()` methods — adds 30 lines of class scaffolding to do what `@dub_bp.route(...)` does in 1 line. Net loss.
- **Engine inheritance** — `LocalDubEngine(DubEngine)` and `FalDubEngine(DubEngine)` look clean but the engines share almost no code. Composition is simpler.
- **Service locator / DI container** — `container.get("workdir_factory")` is one of the well-known OO anti-patterns. Pass the workdir object explicitly.

The principle in one sentence: **class for stateful services and variant workdirs; function for routes, engine wrappers, and helpers.**

---

## Summary — the 12 rules that matter most

If you only remember twelve things:

0. **Old code is immutable during refactor.** All changes happen on the new side; old files stay intact (no edits, no deletions, no shims). The user decides when to remove the old files.
1. **Scope is `/video-studio/` and `/backend-app/` only.** Sibling trees are read-only.
2. **The plan is the source of truth.** Deviations need a written note + confirmation.
3. **Git is yours.** Agent writes code; you commit.
4. **Pure-move vs pure-change.** Never both in one commit.
5. **No bug fixes during the refactor.** Blockers are flagged; the rest wait for Phase 4.
6. **Engines are untouchable.** Moved as-is, with byte-identical contents.
7. **No schema migration.** On-disk shapes are frozen.
8. **Workdir layout is owned by one class.** No other file constructs workdir paths.
9. **Golden run is per-commit.** You run it; output goes in `.hermes/golden-runs/`.
10. **When in doubt, ask.** Batched, with the trade-offs visible.
11. **Class for stateful services and variant workdirs; function for routes, engine wrappers, and helpers.** OOP is a tool, not a default.
