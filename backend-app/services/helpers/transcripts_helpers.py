"""Transcripts helper — read the local whisper .json sidecar.

Pure relocation of video-studio/app/server.py L737-743.

WHAT'S HERE (B6 push, 2026-07-20):
  - transcript_plain_text  — extract plain text from a stem's
                            faster-whisper .json sidecar (the one
                            course_pipeline/.venv writes)

Why a separate file from library.py?  library.py reads
``transcripts/<stem>.md`` (the human-readable transcript that
the Transcript & Script tab edits).  This file reads
``transcripts/<stem>.json`` (the whisper output with word
timings).  Different file, different parser, different caller
(B6's GET /api/script/<stem> uses this as a fallback when no
edited script exists yet).

server.py is unchanged. The helper stays at its original line
until the entire scripts/transcripts subsystem is retired.
Rule 16.
"""
from __future__ import annotations

from flask import current_app

from services.spend import read_json  # safe JSON loader, no exception


def transcript_plain_text(stem: str) -> str:
    """Plain transcript text (no timestamps) from the local
    faster-whisper .json sidecar.

    Returns the joined ``text`` of every non-empty segment, with
    a single space between segments.  Returns ``""`` if the
    sidecar is missing or unparseable.

    Pure relocation of server.py L737-743.  Reads ``TRANSCRIPTS``
    from ``app.config`` (computed in ``create_app`` at B4).
    """
    transcripts = current_app.config["TRANSCRIPTS"]
    sidecar = transcripts / f"{stem}.json"
    data = read_json(sidecar)
    if not data:
        return ""
    return " ".join(s["text"].strip() for s in data.get("segments", [])
                   if s.get("text", "").strip())
