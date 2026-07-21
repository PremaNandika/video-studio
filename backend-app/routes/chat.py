"""Chat / LLM route module — the first ClaudeRunner consumer.

Pure move of the LLM cluster from video-studio/app/server.py:

  1. ``POST /api/chat``            (server.py L1382-1398) — start a chat
     turn. Delegates to ``ClaudeRunner.start_turn`` (the streaming,
     stateful worker moved into the service in S4).
  2. ``GET  /api/chat/<turn_id>``  (server.py L1401-1410) — poll a turn's
     events (reads ``runner.chats`` under ``runner.chats_lock``).
  3. ``POST /api/copywrite``       (server.py L1413-1491) — rewrite a
     script (inline one-shot Claude call, opus).
  4. ``POST /api/agent-note``      (server.py L1306-1318) — pin a fact
     into research/agent-notes.md (no LLM; chat-adjacent).
  5. ``POST /api/aifix/<stem>``    (server.py L2343-2390) — proofread
     caption lines (inline one-shot Claude call, haiku).

FIRST CLAUDE_RUNNER CONSUMER
  ``ClaudeRunner`` (services/llm.py) has existed since S4 with zero
  consumers. B12 is where it comes alive: ``app.py`` constructs it and
  stashes ``app.config["CLAUDE_RUNNER"]`` (B12 commit 1); these routes
  read it (commit 2). The chat worker (``run_chat_turn``) already lives
  in the service and is thread-safe by construction — it's a bound
  method using ``self.chats`` / ``self.chats_lock`` and NEVER touches
  ``current_app``, so the B9 worker-thread trap cannot recur.

LLM CALL SHAPES
  - Streaming chat (``/api/chat``): ``ClaudeRunner.start_turn`` spawns the
    daemon thread; ``/api/chat/<turn_id>`` polls the accumulated events.
  - One-shot (``/api/copywrite``, ``/api/aifix``): SYNCHRONOUS inline
    ``subprocess.run([claude, "-p", ...])`` — faithful to the monolith
    (no shared runner for one-shots). A shared ``ClaudeRunner.run_oneshot``
    is deferred DRY work; see
    ``.hermes/decisions/phase-2-2026-07-21-defer-run-oneshot.md``.

  All routes read ``claude_exe`` from the CLAUDE_RUNNER instance
  (``runner.claude_exe``) — NOT from register_clone's ``CLAUDE_EXE``
  stash — so this module depends only on the service B12 wires, and
  register_clone stays untouched.

DEPENDENCIES:
  - services.prompts:        COPY_PROMPT (the copywrite prompt; the aifix
                             proofreader prompt stays inline, as in the
                             monolith — it was never a named constant).
  - services.llm:            inspiration_block (bank-refs → prompt block;
                             takes autovsl_root explicitly).
  - services.helpers.common: read_json (aifix lines.json loader).
  - app.config:              CLAUDE_RUNNER, JOB_RUNNER (env factory),
                             AUTOVSL_ROOT, SUBSTUDIO_OUT.

LIFECYCLE (per Rule 8.1):
  - Every route resolves config in the REQUEST context. The only worker
    thread (chat) is owned by ClaudeRunner (bound method, no current_app).
    copywrite / aifix / agent-note are synchronous.
  - ``register_chat(app)`` is the wire-up point — it stashes NO config
    keys (everything it needs is already on app.config from app.py /
    earlier register_* helpers). Register after the CLAUDE_RUNNER is
    constructed (it is, early in create_app).

server.py is unchanged. The chat/copywrite/agent-note/aifix blocks stay
at their original lines until the B12c cutover lands. Rule 16.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request

from services.helpers.common import read_json
from services.llm import inspiration_block
from services.prompts import COPY_PROMPT


# ---------------------------------------------------------------- HTTP routes

chat_bp = Blueprint("chat", __name__)


# -- 1. POST /api/chat (start a streaming chat turn) ---------------------

@chat_bp.post("/api/chat")
def api_chat():
    """Start a chat turn on the embedded dev/research assistant.

    Pure relocation of server.py L1382-1398. The turn-id allocation,
    the ``self.chats`` record, and the daemon thread are all owned by
    ``ClaudeRunner.start_turn`` (moved in S4). Response: ``{"turn_id"}``.
    """
    body = request.get_json(force=True)
    message = (body.get("message") or "").strip()
    session_id = body.get("session_id") or None
    model = body.get("model") if body.get("model") in ("sonnet", "opus", "haiku") else "sonnet"
    if not message:
        abort(400, "empty message")
    runner = current_app.config["CLAUDE_RUNNER"]
    if not runner.claude_exe:
        abort(500, "claude CLI not found")
    mode = "research" if body.get("mode") == "research" else "dev"
    turn_id = runner.start_turn(message, session_id=session_id, model=model, mode=mode)
    return jsonify({"turn_id": turn_id})


# -- 2. GET /api/chat/<turn_id> (poll turn events) -----------------------

@chat_bp.get("/api/chat/<turn_id>")
def api_chat_poll(turn_id):
    """Poll a chat turn's accumulated events from a byte offset.

    Pure relocation of server.py L1401-1410. Reads the turn state from
    the ClaudeRunner's ``chats`` dict (the same store the worker thread
    appends to) under its lock.
    """
    runner = current_app.config["CLAUDE_RUNNER"]
    chat = runner.chats.get(turn_id)
    if not chat:
        abort(404)
    offset = int(request.args.get("offset", 0))
    with runner.chats_lock:
        events = chat["events"][offset:]
        return jsonify({"status": chat["status"], "session_id": chat["session_id"],
                        "events": events, "next_offset": offset + len(events)})


# -- 3. POST /api/copywrite (inline one-shot rewrite) --------------------

@chat_bp.post("/api/copywrite")
def api_copywrite():
    """Rewrite a script with Claude (headless CLI — the user's subscription).

    Pure relocation of server.py L1413-1491. Inline one-shot (opus). The
    length rule is driven by the video duration + the original speaker's
    pace; ``brand`` mode pulls every bank hook/angle + a product offer.
    """
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    text = (body.get("text") or "").strip()
    instruction = (body.get("instruction") or "").strip() or "Punch this up: stronger hook, more vivid and concrete language, keep it authentic."
    slug = Path(body.get("slug") or "").name
    if not text:
        abort(400, "empty script")
    runner = current_app.config["CLAUDE_RUNNER"]
    if not runner.claude_exe:
        abort(500, "claude CLI not found — install Claude Code or add it to PATH")

    context_block = ""
    offer = autovsl / "products" / slug / "offer.md" if slug else None
    if offer and offer.is_file():
        context_block = "\nPRODUCT CONTEXT (ground claims and voice in this):\n" + \
            offer.read_text(encoding="utf-8", errors="replace")[:2500] + "\n"

    refs = body.get("bank_refs") or []
    if body.get("brand"):
        # full research awareness: every hook + angle in the banks, plus the brand offer
        for bank, t in (("hooks", "hook"), ("angles", "angle")):
            p = autovsl / "banks" / f"{bank}.jsonl"
            if p.is_file():
                for line in p.read_text(encoding="utf-8").splitlines():
                    try:
                        refs.append({"type": t, "id": json.loads(line).get("id")})
                    except (json.JSONDecodeError, AttributeError):
                        pass
        if not context_block:
            for offer in sorted((autovsl / "products").glob("*/offer.md")):
                context_block = ("\nPRODUCT/BRAND CONTEXT (ground every claim and word choice in this):\n"
                                 + offer.read_text(encoding="utf-8", errors="replace")[:2500] + "\n")
                break
        instruction += ("\nThis is a paid AD with real budget behind it — optimize for conversion: "
                        "a scroll-stopping first line, one clear promise, concrete sensory language, "
                        "and a reason to keep watching. Use the proven hooks/angles as patterns, never verbatim.")
    # length target driven by the VIDEO duration AND the ORIGINAL speaker's pace, so the dub
    # both fits the footage and talks at the same speed as the person on screen
    orig = len(text.split())
    try:
        secs = float(body.get("target_seconds") or 0)
    except (TypeError, ValueError):
        secs = 0.0
    try:
        rate = float(body.get("rate") or 0)          # original words-per-second (measured)
    except (TypeError, ValueError):
        rate = 0.0
    rate = rate if 1.0 <= rate <= 5.0 else 2.5        # clamp to a sane speaking range
    if secs > 0:
        target = max(8, round(secs * rate))
        lo, hi = round(target * 0.9), round(target * 1.05)
        length_rule = (f"the video is {secs:.0f}s long and the ORIGINAL speaker talks at "
                       f"~{rate:.1f} words/sec — MATCH THAT PACE: write {lo}-{hi} words "
                       f"(target ~{target}) so the new voice runs at the same speed and fills the "
                       f"same time. Fewer is safe; going over makes the voice rush or overrun.")
    else:
        lo, hi = round(orig * 0.9), round(orig * 1.1)
        length_rule = f"match the original length: {lo}-{hi} words (original is {orig})."
    prompt = COPY_PROMPT.format(
        length_rule=length_rule, context_block=context_block,
        inspiration_block=inspiration_block(autovsl, refs[:16]),   # enough pattern coverage; keeps rewrites fast
        instruction=instruction, text=text,
    )
    env = current_app.config["JOB_RUNNER"]._job_env_factory()
    env.pop("CLAUDECODE", None)  # allow nested headless run from inside a Claude Code session
    try:
        result = subprocess.run(
            [runner.claude_exe, "-p", "--model", "opus",
             "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch"],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=240, cwd=str(autovsl), env=env,
        )
    except subprocess.TimeoutExpired:
        abort(504, "Claude took too long — try again")
    out = (result.stdout or "").strip()
    if result.returncode != 0 or not out:
        abort(502, f"claude CLI failed (rc={result.returncode}): {(result.stderr or '')[:300]}")
    return jsonify({"text": out})


# -- 4. POST /api/agent-note (pin a fact; no LLM) ------------------------

@chat_bp.post("/api/agent-note")
def api_agent_note():
    """Pin a fact/idea from the research agent chat into its permanent memory.

    Pure relocation of server.py L1306-1318. Appends to
    ``research/agent-notes.md`` (creating it with a header on first use).
    No Claude call — grouped here because the research chat consumes it.
    """
    autovsl = current_app.config["AUTOVSL_ROOT"]
    agent_notes = autovsl / "research" / "agent-notes.md"
    text = (request.get_json(force=True).get("text") or "").strip()
    if not text:
        abort(400, "empty note")
    agent_notes.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    with open(agent_notes, "a", encoding="utf-8") as f:
        if agent_notes.stat().st_size == 0:
            f.write("# Agent notes — facts the founder taught the research agent\n")
        f.write(f"\n## {stamp}\n{text[:4000]}\n")
    return jsonify({"saved": str(agent_notes.relative_to(autovsl)).replace("\\", "/")})


# -- 5. POST /api/aifix/<stem> (inline one-shot caption proofread) -------

@chat_bp.post("/api/aifix/<stem>")
def api_aifix(stem):
    """Proofread caption lines with the local Claude CLI (free): fixes
    speech-to-text mishearings using context. Keeps line count/order/timing.

    Pure relocation of server.py L2343-2390. Inline one-shot (haiku); the
    proofreader prompt is inline (it was never a named constant in the
    monolith). Reads/writes ``SUBSTUDIO_OUT/<stem>/lines.json``.
    """
    stem = Path(stem).name
    substudio_out = current_app.config["SUBSTUDIO_OUT"]
    body = request.get_json(force=True) or {}
    lines = body.get("lines")
    if not lines:
        lines = read_json(substudio_out / stem / "lines.json")
        if not lines:
            abort(404, "no captions to fix — caption the video first")
    runner = current_app.config["CLAUDE_RUNNER"]
    if not runner.claude_exe:
        abort(500, "local Claude CLI not found — AI fix unavailable")
    texts = [str(ln.get("text", "")) for ln in lines]
    prompt = (
        "You are a subtitle proofreader. Below is a JSON array of subtitle lines from "
        "speech-to-text; they are short ALL-CAPS lines shown in sequence, so read them as one "
        "continuous script to infer intended words. Fix ONLY transcription errors: misheard or "
        "misspelled words, broken punctuation, nonsense fragments. Do NOT rephrase, do NOT "
        "change style, keep ALL-CAPS, keep the SAME number of lines in the SAME order (each "
        "line keeps its timing). Reply with ONLY the corrected JSON array — no commentary, no "
        "code fences.\n\n" + json.dumps(texts, ensure_ascii=False)
    )
    env = current_app.config["JOB_RUNNER"]._job_env_factory()
    env.pop("CLAUDECODE", None)   # nested-run guard for the CLI
    try:
        r = subprocess.run([runner.claude_exe, "-p", "--model", "haiku"], input=prompt,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        abort(504, "AI took too long — try again")
    out = r.stdout or ""
    i, j2 = out.find("["), out.rfind("]")
    if i < 0 or j2 <= i:
        abort(500, f"AI reply unusable: {out[:150]}")
    try:
        fixed = json.loads(out[i:j2 + 1])
    except Exception:
        abort(500, "AI reply was not valid JSON — try again")
    if not isinstance(fixed, list) or len(fixed) != len(lines):
        abort(500, f"AI returned {len(fixed) if isinstance(fixed, list) else '?'} lines, "
                   f"expected {len(lines)} — try again")
    changed = sum(1 for a, b in zip(texts, fixed) if str(a).strip() != str(b).strip())
    new_lines = [{**ln, "text": str(fixed[k])} for k, ln in enumerate(lines)]
    (substudio_out / stem).mkdir(parents=True, exist_ok=True)
    (substudio_out / stem / "lines.json").write_text(
        json.dumps(new_lines, indent=1), encoding="utf-8")
    return jsonify({"lines": new_lines, "changed": changed})


# ---------------------------------------------------------------- wire-up

def register_chat(app) -> None:
    """Register the chat/LLM Blueprint.

    Stashes NO config keys — every dependency is already on app.config:
    ``CLAUDE_RUNNER`` + ``JOB_RUNNER`` + ``AUTOVSL_ROOT`` + ``SUBSTUDIO_OUT``
    (all from app.py). Must be registered after the CLAUDE_RUNNER is
    constructed (it is, early in create_app).
    """
    app.register_blueprint(chat_bp)
