"""Video Studio job runner — the shared engine subprocess executor.

Pure move from video-studio/app/server.py L296-343 (the `run_job`
function, ~48 lines). This is the worker that every route module
spawns a thread on: the dub route, the caption/recaption route,
the erase route, the Ads Factory actions, the chat turn worker,
etc. Centralizing it here removes the last big dependency on
server.py.

Per BACKEND-REFACTOR-RULES.md Rule 17, the runner is a class
because it owns the two config values every run needs
(``cwd`` + the ``job_env`` factory) and is used by 5+ blueprints
(B7 Dubbing, B8 Captions, B9 Subtitles, B11 Dubsync, B12 Chat,
B13 Ads Factory). Class form mirrors the ClaudeRunner pattern
(LLM service) and the SpendLedger pattern (cost service) so the
app factory wiring is uniform.

WHAT MOVED (the only function):
  From video-studio/app/server.py:
    run_job(job_id, cmd)  L296-343 (function)  — method ``run()``

WHAT IS NOT HERE (deliberately):
  - The GPU helper trio (wait_for_gpu / acquire_gpu / needs_gpu)
    stays in services/jobs.py — those are about *arbitration*
    across jobs, not about *running* a single subprocess.
    Keeping them out of this module means JobRunner can be
    constructed and tested without the GPU machinery.
  - The `run_dub_job` wrapper (server.py L394-396) is the
    dubbing route's responsibility — it threads the spend
    recording onto a successful run. The wrapper lives in
    routes/dubbing.py, not here, so the runner stays generic.
  - The `job_env` factory — passed in via constructor (Callable
    pattern, mirroring ClaudeRunner). Resolution is the app
    factory's responsibility. See the LLM decision doc for why
    we chose factory over inline (Rule 5.1 + Rule 8.5: no
    cross-tree imports, pass dependencies explicitly).
  - `cwd` — passed in via constructor. server.py runs every
    subprocess with `cwd=str(ROOT)` (the autoVSL repo root);
    the route's call to ``run()`` doesn't need to know this.
    Centralized here means the value is set once in the app
    factory, not duplicated per route.

LIFECYCLE (per Rule 17 + Rule 8.1):
  - The app factory constructs one ``JobRunner`` instance per
    app, passing in the resolved ``cwd`` and the
    ``job_env_factory`` from ``app.config``.
  - The instance is stashed at ``app.config["JOB_RUNNER"]``.
  - Route handlers retrieve it via
    ``current_app.config["JOB_RUNNER"]`` and call ``runner.run(...)``
    on a daemon thread they spawn themselves.
  - Worker threads don't need a separate reference — the
    thread target is the bound method ``runner.run``.

server.py is unchanged. The ``run_job`` function stays at
L296-343 until B7 wires the new service and deletes the old
copy. See .hermes/decisions/phase-2-2026-07-20-b7-dubbing.md
(TBD on commit).
"""
from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path


class JobRunner:
    """Shared engine subprocess runner. Owns the two config values
    every run needs (``cwd`` and the ``job_env`` factory).

    Construction is explicit: no module-level globals, no Flask
    app needed for instantiation. The app factory builds one
    instance from ``app.config`` and stashes it at
    ``app.config["JOB_RUNNER"]``.

    Parameters
    ----------
    cwd : Path
        The repo root every engine subprocess runs from. In
        practice this is the autoVSL repo root, mirroring
        server.py's ``cwd=str(ROOT)`` line.
    job_env_factory : Callable[[], dict]
        Returns the env dict for ``subprocess.Popen``. The
        factory pattern mirrors ClaudeRunner (LLM service) and
        keeps this module free of cross-tree imports. The app
        factory wires the production factory; tests pass a stub.

    Methods
    -------
    run(job_id, cmd)
        Thread target. Body is byte-equivalent to the old
        ``run_job`` function in server.py L296-343 (the
        process loop, the GPU lock, the status-mapping, the
        fal.ai post-failure hint lines).
    """

    def __init__(self, *, cwd: Path,
                 job_env_factory: Callable[[], dict]) -> None:
        self._cwd = Path(cwd)
        self._job_env_factory = job_env_factory

    def run(self, job_id: str, cmd: list[str]) -> None:
        """Run the engine subprocess for ``job_id``, streaming its
        stdout into the job's log lines.

        Mirrors server.py L296-343. The body is a near-byte-
        equivalent of the original; the only deltas are
        (a) ``self._cwd`` replaces the module-level ROOT,
        (b) ``self._job_env_factory()`` replaces the
        module-level ``job_env()`` call, and (c) the
        ``services.jobs`` symbols are imported locally so
        the import surface is explicit.
        """
        # Local imports keep the module surface minimal and
        # make the dependency on the job store visible.
        from services.jobs import (GPU_LOCK, acquire_gpu, jobs,
                                   jobs_lock, needs_gpu, wait_for_gpu)

        job = jobs[job_id]
        job["cmd"] = [str(c) for c in cmd]   # recorded so the job can be resumed
        gpu = job["gpu"] if "gpu" in job else needs_gpu(job["cmd"])
        got_gpu = False
        try:
            if gpu:
                wait_for_gpu(job)    # yield to subtitle-studio / the old dashboard
                acquire_gpu(job)     # one Video Studio GPU job at a time (4 GB card)
                got_gpu = True
            proc = subprocess.Popen(
                cmd, cwd=str(self._cwd), env=self._job_env_factory(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            job["pid"] = proc.pid
            for line in proc.stdout:
                with jobs_lock:
                    job["lines"].append(line.rstrip("\n"))
            proc.wait()
            job["returncode"] = proc.returncode
            if job["status"] == "stopped":
                pass                       # user pressed Stop — keep that status, skip the failure paths
            elif proc.returncode == 0:
                job["status"] = "done"
            elif job["action"] == "check-media":
                job["status"] = "issues"  # check-media exits 1 when files are missing — a report, not a crash
            else:
                job["status"] = "failed"
                tail = "\n".join(job["lines"][-60:])
                if "Exhausted balance" in tail or "User is locked" in tail:
                    with jobs_lock:
                        job["lines"].append("")
                        job["lines"].append(">>> fal.ai BALANCE IS EMPTY — nothing was charged for this run. <<<")
                        job["lines"].append(">>> Fix: top up at fal.ai/dashboard/billing, or create YOUR OWN key at fal.ai/dashboard/keys and replace FAL_KEY=... in autoVSL/.env <<<")
                elif "401" in tail and "fal" in tail.lower():
                    with jobs_lock:
                        job["lines"].append(">>> fal.ai key rejected — check FAL_KEY in autoVSL/.env <<<")
        except Exception as exc:  # surface launcher errors in the log panel
            with jobs_lock:
                job["lines"].append(f"[dashboard] failed to run: {exc}")
            if job["status"] != "stopped":
                job["status"] = "failed"
            job["returncode"] = -1
        finally:
            if got_gpu:
                GPU_LOCK.release()
            job["ended"] = time.time()
