"""Dub workdir layout — one class owns every path inside
``autoVSL/output/script-swap/<stem>/``.

Pure-move per BACKEND-REFACTOR-RULES.md Rule 16. The 12 filename
constants in this module are the **complete set** found by grepping
``video-studio/app/server.py`` for ``SWAP_WORK / stem / "..."`` and
the f-string templates that build the same paths. Cross-checked
against ``AGENTS.md`` §4.6 (the canonical workdir layout doc).

WHY A CLASS (Rule 17):
  The plan calls workdir "the strongest case for OOP in the project"
  because it combines three Rule 17 triggers:
    1. State that persists across calls — ``(swap_root, stem)``
       defines the workdir; every method reads it.
    2. Variants of the same thing — Dub / Recaption / CleanSubs /
       CleanErase workdirs have different filenames (added later).
    3. State and behavior are the same concern — knowing where
       ``final.mp4`` is *and* knowing how to promote a repair take
       to it are inseparable (behavior methods land in blueprint
       commits that call them; see the S5 decision doc).

WHAT IS NOT HERE (deferred, per Rule 16):
  - No behavior methods (``promote_repair_take``, ``read_script``,
    ``write_voice``, ``append_version``, etc.). These are added in
    the blueprint commit that needs them — never speculatively.
  - No wiring in ``app.py`` — no caller exists yet. Wiring is
    B5 Exports / B7 Dubbing's decision.
  - No ``RecaptionWorkdir`` / ``CleanSubsWorkdir`` classes — those
    layouts aren't being moved in this commit.

THE CONTRACT:
  ``DubWorkdir(swap_root, stem)`` is the only object that constructs
  paths inside a dub workdir. Every other module reads
  ``wd.final`` / ``wd.script_edited`` / etc. — never
  ``swap_root / stem / "final.mp4"``.

LIFECYCLE (per Rule 17 + Rule 8.1):
  The class is constructed per-request by route handlers, with
  ``swap_root`` read from ``current_app.config["AUTOVSL_ROOT"]`` +
  ``"output/script-swap"``. No shared state, no global instance
  needed for S5. Future commits may move to a factory if construction
  gets repetitive.

server.py is unchanged. ``SWAP_WORK`` and the 12 string-literal
filenames remain at their original lines until B5 Exports wires
the new class and deletes the old path constructions. See
``.hermes/decisions/phase-1-2026-07-20-workdir-class-skeleton.md``.
"""
from __future__ import annotations

from pathlib import Path


class DubWorkdir:
    """Owns the layout of ``autoVSL/output/script-swap/<stem>/``.

    The 12 filename constants below are the complete, canonical
    inventory of files in a dub workdir (cross-checked against
    ``AGENTS.md`` §4.6). New workdir files MUST be added here, not
    sprinkled as bare string literals across route handlers.

    Variants:
      This is the **Dub** workdir. Sibling classes for the recaption,
      clean-subs, clean-erase layouts will live in this same module
      when those blueprints move (B8 / B9). They are NOT subclasses
      of this one — Rule 17 forbids inheritance for engine variants.
    """

    # --- the 12 canonical filenames ---------------------------------------
    # Every one of these was a bare string literal somewhere in server.py
    # before this commit. Adding a new workdir file = add a constant here.
    FINAL = "final.mp4"
    FINAL_CAPTIONED = "final-captioned.mp4"
    SCRIPT_EDITED = "script-edited.txt"
    TRANSCRIPT = "transcript.txt"
    NEW_VO = "new-vo.mp3"
    VOICE_JSON = "voice.json"
    DUB_CONFIG_JSON = "dub-config.json"
    VERSIONS_JSON = "versions.json"
    CLONE_INFO_JSON = "clone-info.json"
    SOURCE_TXT = "source.txt"
    PREVIEW_VISUAL = "preview-visual.jpg"
    PREVIEW_OBJECT = "preview-object.jpg"

    def __init__(self, autovsl_root: Path, stem: str) -> None:
        """
        Args:
            autovsl_root: absolute path to the ``autoVSL/`` data root.
                ``swap_root`` (``<autovsl_root>/output/script-swap``)
                is derived from this. Per Rule 8.1, this is read from
                ``app.config["AUTOVSL_ROOT"]`` at the call site — not
                hardcoded here.
            stem: the workdir name — usually the source video's stem
                (e.g. ``"1783501512600.publer.com-2"``).
        """
        self._autovsl_root = Path(autovsl_root)
        self._swap_root = self._autovsl_root / "output" / "script-swap"
        self._stem = stem

    # --- identity ---------------------------------------------------------

    @property
    def stem(self) -> str:
        """The workdir's stem (the source video's filename without ext)."""
        return self._stem

    @property
    def dir(self) -> Path:
        """The workdir itself: ``<swap_root>/<stem>/``."""
        return self._swap_root / self._stem

    # --- path properties (one per filename) -------------------------------
    # These are the public API. Every consumer of the workdir reads
    # ``wd.final`` / ``wd.script_edited`` / etc. — never reconstructs
    # the path from a string literal.

    @property
    def final(self) -> Path:
        return self.dir / self.FINAL

    @property
    def final_captioned(self) -> Path:
        return self.dir / self.FINAL_CAPTIONED

    @property
    def script_edited(self) -> Path:
        return self.dir / self.SCRIPT_EDITED

    @property
    def transcript(self) -> Path:
        return self.dir / self.TRANSCRIPT

    @property
    def new_vo(self) -> Path:
        return self.dir / self.NEW_VO

    @property
    def voice(self) -> Path:
        return self.dir / self.VOICE_JSON

    @property
    def dub_config(self) -> Path:
        return self.dir / self.DUB_CONFIG_JSON

    @property
    def versions(self) -> Path:
        return self.dir / self.VERSIONS_JSON

    @property
    def clone_info(self) -> Path:
        return self.dir / self.CLONE_INFO_JSON

    @property
    def source(self) -> Path:
        """Absolute path to the source footage (written into source.txt)."""
        return self.dir / self.SOURCE_TXT

    @property
    def preview_visual(self) -> Path:
        return self.dir / self.PREVIEW_VISUAL

    @property
    def preview_object(self) -> Path:
        return self.dir / self.PREVIEW_OBJECT

    # --- URL helper -------------------------------------------------------
    # The API responses use the project-relative form
    # ``output/script-swap/<stem>/<name>`` so the frontend can serve
    # them via the /media route without leaking absolute paths. This
    # helper is the one place that produces that string from a Path.

    def relative(self, path: Path) -> str:
        """Return the API-relative URL for ``path`` if it lives inside
        this workdir, else raise ValueError.

        Used by the /api/library and /api/dubsync responses that
        today do ``f"output/script-swap/{stem}/{f.name}"``. Anchored
        to ``autovsl_root`` (not ``swap_root``) so the output is the
        canonical ``output/script-swap/<stem>/<name>`` form the
        /media route expects.
        """
        path = Path(path)
        try:
            rel = path.relative_to(self._autovsl_root)
        except ValueError as e:
            raise ValueError(
                f"{path} is not inside {self._autovsl_root}"
            ) from e
        # POSIX form so the URL is consistent on Windows and POSIX.
        return rel.as_posix()
