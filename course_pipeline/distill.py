#!/usr/bin/env python3
"""
Distillation layer: turn raw course transcripts into structured knowledge
(SKILL.md per course) using the `claude` CLI in headless mode — no API key
needed, runs on your Claude subscription.

Reads:   <out>/transcripts/<course>/*.md      (produced by transcribe.py)
Writes:  <out>/skills/<course>/SKILL.md       the distilled, agent-ready document
         <out>/skills/<course>/notes/*.md     per-video condensed notes (only for long courses)

A course = an immediate subdirectory of the transcripts root. Transcripts
sitting directly in the root are treated as one course named after the root.

Idempotent: a course is skipped when its SKILL.md is newer than every
transcript in it. Use --force to redo.

Usage:
    python distill.py                       # distill everything under ../output/transcripts
    python distill.py --course "Cold Email Mastery"
    python distill.py --model opus --force
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# both prompts live in the workspace-level prompts.json (see prompts.py)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prompts import prompts                                    # noqa: E402

# Transcripts up to this many characters are distilled in one pass; longer
# courses get a map-reduce: per-video notes first, then one synthesis call.
SINGLE_PASS_MAX_CHARS = 300_000


def find_claude() -> str:
    exe = shutil.which("claude")
    if exe:
        return exe
    for candidate in (Path.home() / ".local/bin/claude.exe", Path.home() / ".local/bin/claude"):
        if candidate.exists():
            return str(candidate)
    sys.exit("`claude` CLI not found. Install Claude Code or add it to PATH.")


def resolve_model(name: str, model: str | None) -> str:
    """--model wins; otherwise whatever prompts.json sets for this prompt."""
    return model or prompts.model(name) or "sonnet"


def run_claude(claude: str, name: str, model: str | None, **fields) -> str:
    # Text-generation only: forbid tools so headless claude never tries to
    # write files itself (which stalls on permissions and corrupts output).
    result = subprocess.run(
        [claude, "-p", "--model", resolve_model(name, model),
         "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch"],
        input=prompts.render(name, **fields), capture_output=True, text=True,
        encoding="utf-8", timeout=prompts.timeout(name, 1800),
    )
    out = result.stdout.strip()
    if result.returncode != 0 or not out:
        raise RuntimeError(f"claude CLI failed (rc={result.returncode}): {result.stderr.strip()[:500]}")
    if len(out) < 800 or "#" not in out:
        raise RuntimeError(f"claude output looks wrong (too short / no markdown): {out[:200]}")
    return out


def slugify(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-").replace("--", "-")


def find_courses(transcripts_root: Path) -> dict[str, list[Path]]:
    """Map course name -> sorted transcript .md files."""
    courses: dict[str, list[Path]] = {}
    for sub in sorted(p for p in transcripts_root.iterdir() if p.is_dir()):
        mds = sorted(sub.rglob("*.md"))
        if mds:
            courses[sub.name] = mds
    root_mds = sorted(p for p in transcripts_root.glob("*.md"))
    if root_mds:
        courses[transcripts_root.parent.parent.name or "misc"] = root_mds
    return courses


def needs_distill(skill_path: Path, transcripts: list[Path]) -> bool:
    if not skill_path.exists():
        return True
    skill_mtime = skill_path.stat().st_mtime
    return any(t.stat().st_mtime > skill_mtime for t in transcripts)


def distill_course(claude: str, course: str, transcripts: list[Path],
                   skills_dir: Path, model: str | None) -> None:
    course_dir = skills_dir / course
    course_dir.mkdir(parents=True, exist_ok=True)
    skill_path = course_dir / "SKILL.md"

    texts = {t: t.read_text(encoding="utf-8") for t in transcripts}
    total = sum(len(v) for v in texts.values())
    video_list = ", ".join(t.stem for t in transcripts)
    print(f"  {len(transcripts)} transcripts, {total:,} chars")

    if total <= SINGLE_PASS_MAX_CHARS:
        content = "\n\n".join(f"===== VIDEO: {t.stem} =====\n\n{v}" for t, v in texts.items())
    else:
        # Map-reduce: condense each video first (cached in notes/), then synthesize.
        notes_dir = course_dir / "notes"
        notes_dir.mkdir(exist_ok=True)
        notes = []
        for i, (t, v) in enumerate(texts.items(), 1):
            note_path = notes_dir / f"{t.stem}.md"
            if note_path.exists() and note_path.stat().st_mtime > t.stat().st_mtime:
                print(f"  [{i}/{len(texts)}] notes cached: {t.stem}")
            else:
                print(f"  [{i}/{len(texts)}] condensing: {t.stem}")
                note = run_claude(claude, "course_condense", model, video=t.stem, content=v)
                note_path.write_text(note, encoding="utf-8")
            notes.append(f"===== VIDEO NOTES: {t.stem} =====\n\n{note_path.read_text(encoding='utf-8')}")
        content = "\n\n".join(notes)

    print(f"  synthesizing SKILL.md ({resolve_model('course_skill', model)})...")
    skill = run_claude(claude, "course_skill", model, course=course, slug=slugify(course),
                       video_list=video_list, content=content)
    skill_path.write_text(skill + "\n", encoding="utf-8")
    print(f"  -> {skill_path}")


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    default_root = Path(__file__).resolve().parent.parent / "output"
    ap = argparse.ArgumentParser(description="Distill course transcripts into SKILL.md documents via the claude CLI.")
    ap.add_argument("--transcripts", default=str(default_root / "transcripts"))
    ap.add_argument("--out", default=str(default_root / "skills"))
    ap.add_argument("--model", default=None,
                    help="claude CLI model alias (sonnet, opus, haiku); default: prompts.json")
    ap.add_argument("--course", default=None, help="Only distill this course (folder name)")
    ap.add_argument("--force", action="store_true", help="Redo even if SKILL.md is up to date")
    args = ap.parse_args()

    transcripts_root = Path(args.transcripts).resolve()
    skills_dir = Path(args.out).resolve()
    if not transcripts_root.is_dir():
        sys.exit(f"No transcripts directory at {transcripts_root} — run transcribe.py first.")

    courses = find_courses(transcripts_root)
    if args.course:
        courses = {k: v for k, v in courses.items() if k == args.course}
        if not courses:
            sys.exit(f"Course folder not found: {args.course}")
    if not courses:
        sys.exit("No transcripts found to distill.")

    claude = find_claude()
    done = skipped = failed = 0
    for course, transcripts in courses.items():
        skill_path = skills_dir / course / "SKILL.md"
        if not args.force and not needs_distill(skill_path, transcripts):
            print(f"SKIP (up to date): {course}")
            skipped += 1
            continue
        print(f"DISTILLING: {course}")
        try:
            distill_course(claude, course, transcripts, skills_dir, args.model)
            done += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            failed += 1

    print(f"\nDone: {done} distilled, {skipped} up to date, {failed} failed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
