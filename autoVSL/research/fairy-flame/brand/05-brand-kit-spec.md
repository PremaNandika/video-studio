# Brand Kit Spec — Fairy Flame (liitt) · Platform Audit

> **Brand-architect Phase 5 — AUDIT, not rebuild.** Existing system audited: `Projects/līītt/liitt.co/brand-kit/` (tokens.css v1.0, components.md, BRAND-KIT-HANDOFF.md, Direction D — Editorial). **No production file has been edited.** Every proposed change below is flagged ⚑ and requires Colton's sign-off. Implementation notes for Gabe are self-contained at the end.
>
> Correction to the brief: the brief described the system as "Playfair Display + Mulish" — the shipped kit explicitly supersedes those as placeholders. Actual faces: **Bricolage Grotesque** (display), **Newsreader** (body), **Hanken Grotesk** (UI).

## Audit verdict: the system already IS the Glow

The existing kit traces to the brand world with almost eerie precision — it should be kept essentially as-is:

| Existing element | Traces to platform? | Verdict |
|---|---|---|
| Dark indigo world (`--ff-bg-base #0B0A1C` → surfaces) | YES — the fog/night the Glow lights up inside. The dark isn't a style choice anymore; it's the world's "before" state | **Keep** |
| Gold accent + `--ff-glow-gold` / `--ff-glow-gold-soft` shadow tokens | YES — the tokens are literally named glow; gold-on-dark IS the Glow rendered in CSS. CTA hover glowing = the door lighting up | **Keep** (these become semantically load-bearing) |
| `--ff-accent-raspberry #B23A6E` | YES — the deep-magenta flame gummy; the Ember (Phase 4, Direction C) | **Keep**, role clarified below ⚑ |
| `--ff-accent-indigo #4B40C9` | Neutral — secondary highlight, no conflict | **Keep** |
| Bricolage/Newsreader/Hanken editorial stack | YES — "enchanted but grounded": characterful display + literary serif body = premium mystic without trippy clichés | **Keep** |
| Primitive/semantic-ish split, plain CSS, Olympus/BEM commerce rules | YES — re-themeable for sibling liitt brands (the skill's multi-brand mandate) | **Keep** |
| Contrast rule (never white on gold) | Independent of platform | **Keep** (inherited into all new specs) |
| Soft-rectangle CTAs, 8-based spacing, radius scale | No conflict | **Keep** |

**Conflicts found: none.** The platform work (Phases 1–3) validates the existing system rather than fighting it. What follows are (a) three small ⚑ proposals and (b) the additive spec sections the kit doesn't yet have (photography, packaging reads, claims zones) — new material, not changes.

## ⚑ Proposed changes (approval required — nothing edited)

1. **⚑ Add three semantic aliases to tokens.css** (additive lines only, zero visual change):
   ```css
   --ff-glow-state:   var(--ff-glow-gold-soft);  /* "Glow moments": selected states, day-45 arc UI, Glim halo */
   --ff-ember:        var(--ff-accent-raspberry); /* product/gummy contexts — the lit-from-within Ember */
   --ff-fog-text:     var(--ff-text-muted);       /* "before-state" copy: pain-mirror sections, old-photo captions */
   ```
   *Why:* gives designers/devs platform-language handles so the brand world survives handoffs; primitives untouched.
2. **⚑ Twin Wicks wordmark revision** (Phase 4, Direction B): liitt's ii dots as lit wicks — a logo-asset task for Marc, not a token/CSS change. Also completes the kit's own pending item (SVG master + favicons).
3. **⚑ Clarify raspberry's role in components.md** from "flavour tag, tertiary" to "flavour tag + product/Ember contexts" — one line, so the gummy's magenta reads as intentional product identity rather than a stray tertiary.

## Additive spec sections (new material — the kit's open gaps)

### Photography & illustration direction
- **The rule: one light source, and it's warm.** Every frame has a single believable warm source (window dusk, lamp, the ember'd gummy) in a dark-leaning environment. The Glow is *photographed*, not filtered — no HDR brightness, no candy-bright studio white (the category code we break).
- **People:** real women 40–55, intimate documentary distance, "after" states shown as behavior (laughing at dinner, singing in the car) — never skin-glow beauty conventions (the Glow ≠ complexion, per platform guardrail).
- **Product:** the Ember convention — gummy lit from within (`--ff-ember` family) on dark surfaces; pouch shot with wicks' glow as the only luminance.
- **Illustration:** light-behavior only (Glim, particles, glow edges) — no trippy swirls, no mushroom iconography (census: every scaled leader avoids it; it drags toward the TRE House end).
- **UGC/ads:** phone-native capture stays raw (legitimacy code); the world enters through lighting, Glim composites, and end-cards — premium lives in the container, not a polished capture.

### Packaging hierarchy — front-of-pack in 3 reads
1. **1-second shelf read:** matte dark-indigo pouch, Twin Wicks glow as the only luminance + flame mark. On a shelf of candy-bright competitors, the dark pouch with two small lights IS the differentiation (positioning map, empty quadrant).
2. **3-second pickup read:** "Fairy Flame" + "Microdose Gummies" + one line: "Feel like yourself again." + 30 count. Eyebrow style per `.ff-eyebrow` (Hanken, 2px tracking, gold).
3. **10-second flip read:** the trust stack — named actives with real mg doses, COA QR ("this batch's lab report"), the 45-day arc strip (days 1–7 / 8–21 / 22–45 / 45+), dosing guide pointer, 60-day guarantee, made-in-USA/GMP. Newsreader body, no whimsy in this zone (mascot exclusion rule).

### Compliance-safe claim phrasing zones
- **Zone A — World copy** (hero, ads, story): identity/restoration language, "supports" verbs only, actives stay general ("microdose gummies"). Banned words apply (wellness, journey, holistic).
- **Zone B — Mechanism copy** (PDP mid-scroll): the third door framing + named supporting actives with mg + arc language ("gentle by design"). "Supports calm focus / a steady baseline" phrasing bank — never treatment/cure/disease terms.
- **Zone C — Proof copy** (COA, guarantee, testimonials): zero magic, zero mascot, maximum specificity. Only real testimonials (Jordan T., Maya S.) until real numbers exist; no invented counts/press.

## Multi-brand theming note (liitt sub-universe readiness)

The primitive/semantic split means sibling products (Deep Glow sleep, Morning Glow focus — Phase 6 sub-universes) re-theme by swapping the semantic accent layer (e.g., a cooler bioluminescent accent for sleep) over identical primitives — same world, new room, per the Grüns-kids pattern. No structural work needed now; the ⚑1 aliases make it cleaner later.

## Implementation brief (self-contained, for Gabe)

**What this is:** the brand platform (The Glow) has been audited against the shipped Fairy Flame brand kit at `līītt/liitt.co/brand-kit/` (tokens.css, components.md, BRAND-KIT-HANDOFF.md). **Result: no rebuild. Do not restructure anything.** Pending Colton's sign-off, the only code change is ⚑1: append the three semantic alias tokens above to `tokens.css` (additive, no existing lines modified, zero visual regression — verify by diffing rendered pages before/after, expect identical). All existing rules stay binding: Campaign Cart SDK markup untouched (`.os-card__*`, `data-next-*` style-only), never white on gold, breakpoints 1280/991/767/479, mobile sticky-bar padding 88px. New assets (Twin Wicks SVG/favicons, Glim Lottie files) arrive from Marc per the Phase 4 handoff brief; wire favicons per the kit's §7 when delivered. Photography/packaging/claims sections above are creative-production specs, not dev tasks.

---
**⚑ Awaiting sign-off on items 1–3. No production files have been touched.**
