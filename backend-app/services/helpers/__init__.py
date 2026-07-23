"""Helpers — stateless, pure-data functions used by route modules.

Per the user's rule (2026-07-20), every helper that isn't
stateful or class-shaped (Rule 17) goes here, not in
``routes/`` (those are route handlers) and not at the top of
``services/`` (those are stateful services like SpendLedger
and ClaudeRunner).

Pattern: helpers are read-only query functions. They read
files / the jobs dict / app.config and return dicts or lists
of dicts. No mutation, no subprocess, no class state.

Each module in here is named by the subsystem it serves,
with a ``_helpers`` suffix: ``library_helpers.py`` for the
Library tab, ``exports_helpers.py`` for the Exports tab, etc.
Helpers that are reused across subsystems live in
``common.py`` (the one module that keeps its bare name — it
is not tied to a single service/route).
"""
