# Fairy Flame — 30-Day Microdose VSL

**Format:** 9:16 vertical, ~60s  
**Product:** Fairy Flame microdose gummies  
**CTA:** fairyflame.com  
**Mood:** A24 cinematic, warm, intimate, no hard sell

## Pipeline (all generation is FREE — Palmier is edit-only)

1. **VO** — `python3 scripts/generate-vo.py fairy-flame` (Edge TTS, $0)
2. **Video** — Hailuo / Luma / Kling free tiers (manual, see `docs/free-generation.md`)
3. **Music** — Suno free tier or Pixabay (manual)
4. **Assemble** — `scripts/assemble-vsl.sh` (ffmpeg, $0) OR Palmier import-only (no Generate)

**Never use Palmier's Generate buttons** — they cost ~$30/5min of credits.

## Negative prompt (all Kling shots)

```
neon, fast cuts, glossy skin, fake smiles, stock footage energy, hard sell, before/after split screen, EDM
```

## After generation

Drop files into:

- `media/video/` — `shot-01.mp4` through `shot-08.mp4`
- `media/audio/` — `vo-01.mp3` through `vo-08.mp3`
- `media/music/` — `background.mp3`

Then in Cursor Agent mode, say:

> Build the Fairy Flame VSL from `vsls/fairy-flame/timeline.json`. Palmier is open.
