#!/usr/bin/env python3
"""The one reader of prompts.json — every LLM prompt in this workspace.

Consumers (video-studio, autoVSL dashboard, subtitle-studio, course_pipeline) live in
subfolders and each run in a different venv, so this is stdlib-only and imported by
path:

    import sys; from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # repo root
    from prompts import prompts

    prompt = prompts.render("copy_rewrite", length_rule=..., text=...)
    subprocess.run([claude, "-p", "--model", prompts.model("copy_rewrite")],
                   input=prompt, timeout=prompts.timeout("copy_rewrite"), ...)

Edits to prompts.json are picked up on the next render — no server restart. A
gitignored prompts.local.json is merged key-by-key on top, so a machine can override
the text or the model of one prompt without touching the committed file.

CLI:
    python prompts.py              validate + list every prompt
    python prompts.py copy_rewrite print one, with its placeholders
"""
from __future__ import annotations

import json
import string
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "prompts.json"
LOCAL_PATH = ROOT / "prompts.local.json"

_FORMATTER = string.Formatter()


class PromptError(RuntimeError):
    """Bad prompts.json, unknown prompt name, or a render with the wrong variables."""


def _placeholders(text: str) -> set[str]:
    """The {names} the caller must supply. Literal {{braces}} are not placeholders."""
    try:
        return {f for _, f, _, _ in _FORMATTER.parse(text) if f}
    except ValueError as e:                       # unbalanced brace in the text
        raise PromptError(f"malformed placeholder syntax: {e}") from e


class Prompt:
    def __init__(self, name: str, spec: dict, defaults: dict):
        if not isinstance(spec, dict):
            raise PromptError(f"prompt '{name}' must be an object, got {type(spec).__name__}")
        raw = spec.get("text")
        if isinstance(raw, list):
            text = "\n".join(str(line) for line in raw)
        elif isinstance(raw, str):
            text = raw
        else:
            raise PromptError(f"prompt '{name}' has no 'text' (list of lines or string)")
        if not text.strip():
            raise PromptError(f"prompt '{name}' has empty text")
        self.name = name
        self.text = text
        # null in the file = "the caller decides" (chat model from the UI, distill --model);
        # anything else falls back to the file's defaults block.
        self.model = spec["model"] if "model" in spec else defaults.get("model")
        self.timeout = spec["timeout"] if "timeout" in spec else defaults.get("timeout")
        self.description = spec.get("description", "")
        self.used_by = spec.get("used_by") or []
        self.notes = spec.get("notes", "")
        self.vars = _placeholders(text)            # validates brace syntax at load time

    def render(self, **kwargs) -> str:
        missing = self.vars - set(kwargs)
        if missing:
            raise PromptError(f"prompt '{self.name}' needs {sorted(missing)} "
                              f"(got {sorted(kwargs)})")
        extra = set(kwargs) - self.vars
        if extra:
            raise PromptError(f"prompt '{self.name}' has no placeholder for {sorted(extra)} — "
                              f"it takes {sorted(self.vars)}")
        return self.text.format(**kwargs)


class PromptStore:
    """Loads prompts.json once, reloads it when the file changes on disk."""

    def __init__(self, path: Path = CONFIG_PATH, local: Path = LOCAL_PATH):
        self.path = Path(path)
        self.local = Path(local)
        self._prompts: dict[str, Prompt] = {}
        self._stamp: tuple = ()
        self.load()

    # ------------------------------------------------------------------ loading
    def _mtimes(self) -> tuple:
        def stamp(p: Path):
            try:
                st = p.stat()
                return (st.st_mtime_ns, st.st_size)
            except OSError:
                return None
        return (stamp(self.path), stamp(self.local))

    def load(self) -> None:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise PromptError(f"prompt config not found: {self.path}") from None
        except json.JSONDecodeError as e:
            raise PromptError(f"{self.path.name} is not valid JSON: {e}") from None
        specs = doc.get("prompts")
        if not isinstance(specs, dict) or not specs:
            raise PromptError(f"{self.path.name} has no 'prompts' object")
        defaults = doc.get("defaults") or {}

        if self.local.is_file():
            try:
                over = (json.loads(self.local.read_text(encoding="utf-8")) or {}).get("prompts") or {}
            except json.JSONDecodeError as e:
                raise PromptError(f"{self.local.name} is not valid JSON: {e}") from None
            for name, patch in over.items():
                # per-key merge: an override may change only the model, or only the text
                specs[name] = {**specs.get(name, {}), **(patch or {})}

        self._prompts = {name: Prompt(name, spec, defaults) for name, spec in specs.items()}
        self._stamp = self._mtimes()

    def _fresh(self) -> None:
        if self._mtimes() != self._stamp:
            self.load()

    # ------------------------------------------------------------------ lookup
    def get(self, name: str) -> Prompt:
        self._fresh()
        try:
            return self._prompts[name]
        except KeyError:
            raise PromptError(f"unknown prompt '{name}' — {self.path.name} has: "
                              f"{', '.join(sorted(self._prompts))}") from None

    def render(self, name: str, **kwargs) -> str:
        return self.get(name).render(**kwargs)

    def text(self, name: str) -> str:
        """Raw text — for prompts with no placeholders (the chat system prompts)."""
        return self.get(name).text

    def model(self, name: str, default: str | None = None) -> str | None:
        return self.get(name).model or default

    def timeout(self, name: str, default: int | None = None) -> int | None:
        t = self.get(name).timeout
        return default if t is None else t

    def names(self) -> list[str]:
        self._fresh()
        return sorted(self._prompts)

    def __contains__(self, name: str) -> bool:
        self._fresh()
        return name in self._prompts

    def __iter__(self):
        self._fresh()
        return iter(self._prompts.values())


prompts = PromptStore()


# ---------------------------------------------------------------------- CLI
def _main(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv:
        try:
            p = prompts.get(argv[0])
        except PromptError as e:
            print(f"ERROR: {e}")
            return 1
        print(f"# {p.name} — {p.description}")
        print(f"# model: {p.model or '(caller decides)'} · timeout: {p.timeout or '-'}s · "
              f"vars: {', '.join(sorted(p.vars)) or '(none)'}")
        for u in p.used_by:
            print(f"# used by: {u}")
        print("-" * 78)
        print(p.text)
        return 0

    print(f"{prompts.path}\n")
    if prompts.local.is_file():
        print(f"(overridden by {prompts.local.name})\n")
    width = max(len(n) for n in prompts.names())
    for p in sorted(prompts, key=lambda x: x.name):
        model = p.model or "-"
        print(f"  {p.name:<{width}}  {model:<7} {str(p.timeout or '-'):>5}s  {p.description}")
        if p.vars:
            print(f"  {'':<{width}}  vars: {', '.join(sorted(p.vars))}")
    print(f"\n{len(prompts.names())} prompts, all valid.")
    return 0


if __name__ == "__main__":
    import sys
    try:
        sys.exit(_main(sys.argv[1:]))
    except PromptError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
