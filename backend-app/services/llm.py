"""Claude CLI runner + bank-entry helpers — LLM concern, Phase 1 S4.

Pure move from video-studio/app/server.py across 4 non-contiguous
blocks (L22-25 CLAUDE_EXE, L695-709 load_bank_entry, L712-734
inspiration_block, L1274-1379 CHAT_SYSTEM/RESEARCH_SYSTEM/chats/
chats_lock/run_chat_turn). Total ~110 source lines, restructured
into a class per Rule 17.

Per BACKEND-REFACTOR-RULES.md Rule 17, the LLM concern is a
``ClaudeRunner`` class because it owns state that persists
across calls (the in-memory ``chats`` dict, the ``chats_lock``)
and shares configuration (CLI flags, env pop, the "pop
CLAUDECODE" rule) across 8+ call sites. Per the plan's S4 row:
"The 'pop CLAUDECODE' rule lives HERE per the plan, not
duplicated per route."

WHAT MOVED (6 symbols + 2 module functions):
  From video-studio/app/server.py:
    CLAUDE_EXE          L22-25 (constant)        — constructor arg
    CHAT_SYSTEM         L1274-1282 (constant)    — constructor arg
    RESEARCH_SYSTEM     L1286-1300 (constant)    — constructor arg
    chats: dict         L1302 (module state)     — instance state
    chats_lock          L1303 (threading.Lock)   — instance state
    run_chat_turn(...)  L1321-1379 (function)    — method (spawns thread)

  Inlined as module functions (no state, file-format helpers):
    load_bank_entry(bank, entry_id)        L695-709
    inspiration_block(autovsl_root, refs)  L712-734

  These two helpers live in llm.py (not a new banks.py) because
  the LLM is their only consumer; promoting them to a separate
  service would be a 6th service file the plan didn't authorize.

UNRESOLVED NAMES (per Rule 16 + Option E from the spend reference):
  - job_env (server.py L288) — passed as a Callable factory via
    the constructor's ``job_env_factory`` arg. Production callers
    pass ``server.job_env``; tests pass a stub. Resolution is
    deferred to B12 routes/chat.py, the first blueprint that
    uses ClaudeRunner.
  - ROOT (server.py L50) — ``inspiration_block`` takes
    ``autovsl_root: Path`` as its first arg; the call site reads
    it from ``app.config["AUTOVSL_ROOT"]`` (or the monolith's
    ROOT for the old-side callers until B12 wires the swap).

LIFECYCLE (per Rule 17 + Rule 8.1):
  - The app factory constructs one ``ClaudeRunner`` instance per
    app, passing in the resolved paths and system prompts from
    ``app.config``.
  - The instance is stashed at ``app.config["CLAUDE_RUNNER"]``.
  - Route handlers retrieve it via ``current_app.config["CLAUDE_RUNNER"]``.
  - Worker threads (chat turn spawn) receive the instance via
    ``self`` — the threading.Thread target is a bound method, so
    no explicit pass is needed.

server.py is unchanged. The 6 LLM symbols + 2 helpers all remain
at their original lines until B12 routes/chat.py wires the new
service and deletes the old copies. See
.hermes/decisions/phase-1-2026-07-20-llm-class-with-callable-factory.md.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


# --- module-level constants (CLI flags, shared by every chat call) -------

# Tools the LLM is allowed to call during a chat turn. Read-only by design:
# the chat agent advises, it does not edit the repo.
_CHAT_ALLOWED_TOOLS = "Read,Grep,Glob"
# Tools explicitly blocked (the agent must not edit/run/fetch).
_CHAT_DISALLOWED_TOOLS = "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch,Task"
# Hard kill switch for runaway turns (10 min — Claude CLI is fast but the
# user can be reading).
_CHAT_TURN_TIMEOUT_S = 600


# --- bank-entry helpers (module functions; no state) ---------------------

def load_bank_entry(autovsl_root: Path, bank: str, entry_id: str) -> dict | None:
    """Load a single entry from ``banks/<bank>.jsonl`` by id.

    Mirrors server.py L695-709 exactly except for taking
    ``autovsl_root`` explicitly instead of reading the
    module-level ROOT. The return shape is the parsed dict
    (or None if the file is missing / the id isn't found /
    a JSONL line fails to parse — the original is silent on
    the third case and so are we).
    """
    path = autovsl_root / "banks" / f"{bank}.jsonl"
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("id") == entry_id:
                return entry
    return None


def inspiration_block(autovsl_root: Path, refs: list) -> str:
    """Format selected bank hooks/angles/scripts as prompt inspiration.

    Mirrors server.py L712-734. Returns the empty string when refs
    is empty or no ref matches a known bank entry; otherwise returns
    the wrapped "PROVEN INSPIRATION..." block.
    """
    parts: list[str] = []
    for ref in refs[:40]:
        t = ref.get("type")
        if t == "hook":
            entry = load_bank_entry(autovsl_root, "hooks", ref.get("id", ""))
            if entry:
                parts.append(
                    f"[hook {entry['id']} · {entry.get('hook_class', '')}] "
                    f"\"{entry.get('text_verbatim', '')}\""
                    + (f" — visual: {entry['visual']}" if entry.get("visual") else "")
                )
        elif t == "angle":
            entry = load_bank_entry(autovsl_root, "angles", ref.get("id", ""))
            if entry:
                parts.append(
                    f"[angle {entry['id']} · {entry.get('name', '')}] "
                    f"{entry.get('argument', '')[:500]}"
                )
        elif t == "script":
            p = (autovsl_root / str(ref.get("path", ""))).resolve()
            if (
                str(p).startswith(str(autovsl_root / "products"))
                and p.suffix == ".md"
                and p.is_file()
            ):
                parts.append(
                    f"[script {p.name}]\n"
                    f"{p.read_text(encoding='utf-8', errors='replace')[:1500]}"
                )
    if not parts:
        return ""
    return (
        "\nPROVEN INSPIRATION FROM THE RESEARCH BANKS (adapt their structure, "
        "energy, and psychology to THIS script's product and audience — never "
        "copy competitor brand names, product names, or specific claims verbatim):\n"
        + "\n\n".join(parts)
        + "\n"
    )


# --- claude-exe resolution (server.py L22-25) ----------------------------

def resolve_claude_exe(explicit: str | None = None) -> str | None:
    """Resolve the claude CLI path.

    Resolution order: explicit arg → shutil.which("claude") →
    ~/.local/bin/claude.exe → ~/.local/bin/claude. Returns None
    if none of the candidates exist (the route layer checks this
    and aborts 500).
    """
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for cand in (home / ".local/bin/claude.exe", home / ".local/bin/claude"):
        if cand.exists():
            return str(cand)
    return None


# --- the runner (Rule 17 class) ------------------------------------------

class ClaudeRunner:
    """Owns the in-memory chat state + the CLI flags shared across calls.

    Construction is explicit: no module-level globals, no Flask app
    needed for instantiation. The app factory builds one instance
    from ``app.config`` and stashes it at ``app.config["CLAUDE_RUNNER"]``.

    Parameters
    ----------
    claude_exe : str | None
        Absolute path to the claude CLI, or None if not resolved.
        Callers (route handlers) must check for None and abort 500.
    autovsl_root : Path
        The autoVSL repo root. Used by ``inspiration_block`` to
        locate ``banks/`` and validate ``products/`` paths.
    chat_system, research_system : str
        The two system prompts. The route layer picks one based
        on the request's ``mode`` field.
    job_env_factory : Callable[[], dict]
        Returns the env dict for ``subprocess.Popen``. Production
        passes the monolith's ``server.job_env`` (until B7+ moves
        it to ``services/subprocess.py``); tests pass a stub.
    chat_turn_timeout_s : int, default 600
        Hard kill switch for runaway turns.
    """

    def __init__(
        self,
        *,
        claude_exe: str | None,
        autovsl_root: Path,
        chat_system: str,
        research_system: str,
        job_env_factory: Callable[[], dict],
        chat_turn_timeout_s: int = _CHAT_TURN_TIMEOUT_S,
    ) -> None:
        self.claude_exe = claude_exe
        self.autovsl_root = Path(autovsl_root)
        self.chat_system = chat_system
        self.research_system = research_system
        self._job_env_factory = job_env_factory
        self._chat_turn_timeout_s = chat_turn_timeout_s

        # Chat run-state (server.py L1302-1303 lives here now).
        # Keyed by turn_id (8-char hex); value is a dict with
        # id/status/events/session_id/started — same shape as
        # the original so polling routes don't have to change.
        self.chats: dict[str, dict] = {}
        self.chats_lock = threading.Lock()

    # --- the worker (server.py L1321-1379) -----------------------------

    def run_chat_turn(
        self,
        turn_id: str,
        message: str,
        session_id: str | None,
        model: str,
        mode: str = "dev",
    ) -> None:
        """Run one chat turn on a daemon thread (the caller starts it).

        The original function used module-level ``chats`` and
        ``chats_lock``; this method uses ``self.chats`` /
        ``self.chats_lock``. The CLI invocation, the env pop of
        ``CLAUDECODE`` (the rule-of-thumb that goes wrong when
        running nested Claude from inside a Claude Code session),
        the stream-json parsing, the 600s killer, and the
        error-event fallback are all byte-equivalent to the
        original.
        """
        chat = self.chats[turn_id]
        system_prompt = self.research_system if mode == "research" else self.chat_system
        cmd = [
            self.claude_exe, "-p", "--model", model,
            "--output-format", "stream-json", "--verbose",
            "--allowedTools", _CHAT_ALLOWED_TOOLS,
            "--disallowedTools", _CHAT_DISALLOWED_TOOLS,
            "--append-system-prompt", system_prompt,
        ]
        if session_id:
            cmd += ["--resume", session_id]
        env = self._job_env_factory()
        env.pop("CLAUDECODE", None)
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(self.autovsl_root), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            killer = threading.Timer(self._chat_turn_timeout_s, proc.kill)
            killer.start()
            assert proc.stdin is not None
            proc.stdin.write(message)
            proc.stdin.close()
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                with self.chats_lock:
                    t = ev.get("type")
                    if t == "system" and ev.get("subtype") == "init":
                        chat["session_id"] = ev.get("session_id") or chat["session_id"]
                    elif t == "assistant":
                        for block in (ev.get("message") or {}).get("content", []):
                            if block.get("type") == "text" and block.get("text", "").strip():
                                chat["events"].append({"kind": "text", "text": block["text"]})
                            elif block.get("type") == "tool_use":
                                inp = block.get("input") or {}
                                detail = inp.get("file_path") or inp.get("pattern") or inp.get("path") or ""
                                chat["events"].append({
                                    "kind": "tool",
                                    "text": f"{block.get('name')} {detail}".strip(),
                                })
                    elif t == "result":
                        chat["session_id"] = ev.get("session_id") or chat["session_id"]
                        if ev.get("subtype") != "success":
                            chat["events"].append({
                                "kind": "error",
                                "text": str(ev.get("result") or "chat turn failed"),
                            })
            proc.wait()
            killer.cancel()
            assert proc.stderr is not None
            stderr = proc.stderr.read()
            if proc.returncode != 0 and not any(e["kind"] == "text" for e in chat["events"]):
                with self.chats_lock:
                    chat["events"].append({
                        "kind": "error",
                        "text": f"claude CLI failed (rc={proc.returncode}): {stderr[:300]}",
                    })
            chat["status"] = "done"
        except Exception as exc:
            with self.chats_lock:
                chat["events"].append({"kind": "error", "text": f"chat error: {exc}"})
            chat["status"] = "done"

    # --- the public start_turn (the route layer's entry point) ---------

    def start_turn(
        self,
        message: str,
        *,
        session_id: str | None,
        model: str,
        mode: str = "dev",
    ) -> str:
        """Allocate a turn_id, spawn the worker thread, return the id.

        Mirrors the original /api/chat (L1382-1398) shape: the
        route handler validates inputs, then delegates to this
        method. The thread target is the bound method
        ``self.run_chat_turn`` so the worker can mutate
        ``self.chats`` directly.
        """
        turn_id = uuid.uuid4().hex[:8]
        self.chats[turn_id] = {
            "id": turn_id,
            "status": "running",
            "events": [],
            "session_id": session_id,
            "started": time.time(),
        }
        threading.Thread(
            target=self.run_chat_turn,
            args=(turn_id, message, session_id, model, mode),
            daemon=True,
        ).start()
        return turn_id
