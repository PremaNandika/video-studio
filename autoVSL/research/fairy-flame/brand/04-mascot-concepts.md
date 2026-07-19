# Mascot Concepts — Fairy Flame (liitt)

> **Brand-architect Phase 4.** Brand world: **The Glow** (approved). Constraint honored: every direction below lives inside the existing visual system (dark indigo `#0B0A1C` / gold `#E8B84A` / bioluminescent glow tokens) — no redesign required. Direction A is the explicitly-requested "no-redesign" candidate.

## Direction A: "Glim" — character (a wisp of living light)

- **Personality:** gentle, knowing, unhurried.
- **Relationship to avatar:** **guide** — the small light that finds her in the fog and leads her back to herself. Glim never performs for her; it accompanies her. (Not a mirror — she doesn't want to see herself yet; not a comic foil — her pain is never the joke.)
- **Visual brief:** a thumb-sized wisp of warm golden light (`--ff-accent-gold` core, `--ff-glow-gold-soft` halo) — think firefly-meets-candleflame, not Tinker Bell. No face in the base form; expression comes from *behavior*: it brightens when near her, dims respectfully in heavy moments, pulses slowly like calm breathing (≈6 breaths/min — the pace itself is the message). Trails a faint particle shimmer on movement. Renders as pure light on dark surfaces — which is why it drops into every existing dark-indigo layout with zero new tokens. In rare hero moments it may suggest a tiny figure inside the light, never fully drawn.
- **Example usages:**
  1. **Ad hook:** dark kitchen, 5:47am, a tired woman at the counter — a single gold wisp drifts into frame and hovers by the pouch. On-screen text: "Something small found her first." (Pattern-interrupt: a point of light moving on a dark feed-native frame is a 2-second scroll-stopper.)
  2. **Packaging moment:** Glim as a subtle gold foil spark beside the flame mark — visible on pouch-flip, "guarding" the COA QR code (the light that checks the batch).
  3. **Email sign-off:** a small animated glow-pulse beneath the signature: "— keep your flame lit. · liitt"

## Direction B: "The Twin Wicks" — embedded device (the ii in liitt)

- **Personality:** quiet, clever, ownable.
- **Relationship to avatar:** **mirror** — two small lights standing together; her flame and the brand's, side by side.
- **Visual brief:** the double-i in the **liitt** wordmark rendered as two lit wicks — the dots become tiny flames/glows (`--ff-accent-gold-bright`) that can flicker, breathe, or relight in animation. Static contexts: the dots simply carry a soft glow. This is the Grüns-ü move: the wordmark IS the mascot; every logo placement becomes a personality moment. Works today with a logo-file revision only — no layout or token changes.
- **Example usages:**
  1. **Ad hook:** the wordmark's two dots are dark; one flickers on, then the other. Text: "It comes back. Watch." → cut to testimonial.
  2. **Packaging moment:** on-shelf/pouch, the wicks' glow is the only luminance on the matte dark pouch — the 1-second shelf read.
  3. **Email sign-off:** the wicks relight at the footer on scroll-into-view.

## Direction C: "The Ember" — personified product (the flame gummy, barely)

- **Personality:** warm, patient, understated.
- **Relationship to avatar:** **mirror** — the gummy is a small flame that hasn't gone out. Neither has she.
- **Visual brief:** the deep-magenta flame-shaped gummy (`--ff-accent-raspberry` family) with a faint inner ember glow at its core — NO face, ever. Personality is entirely in light behavior: it glows slightly brighter in hand than in pouch (responds to her), pulses at the calm-breath pace. Product photography convention more than character: every gummy hero shot is lit from within.
- **Example usages:**
  1. **Ad hook:** macro shot, dark background, one gummy's ember slowly brightening. Text: "This is a microdose. This is what 'gentle by design' looks like."
  2. **Packaging moment:** front-of-pack product window (or illustration) showing the lit-from-within gummy — instantly not-another-candy-gummy.
  3. **Email sign-off:** the ember as a bullet/divider glyph in replenishment emails ("Day 21 — your ember check-in").

## Recommendation: **Direction A ("Glim"), with Direction B adopted as the wordmark treatment**

A and B are not competitors — Glim is the *character in the world*, the Twin Wicks are the *world in the wordmark*, and they share one visual language (small warm lights in the dark). Glim traces directly to the identity ladder: she is someone whose light went out while everyone watched; the brand's character is literally a small light that comes back and stays. It creates the first-2-seconds video hook the census says nobody in the category has (the mascot lane is empty), and it's infinitely reusable at near-zero production cost (a light source composites into any UGC/b-roll footage). C survives as the product-photography convention (lit-from-within gummy) regardless.

## Usage Rules

- **Appears:** ad opens/transitions, unboxing, email moments, PDP scroll moments, packaging accents, the day-45 "arc complete" moment.
- **Never appears:** claims/COA/lab sections (the lab report needs zero whimsy), price/guarantee/cancel flows, apology or service-recovery emails, anything adjacent to the founder story's tragedy beats (Glim may appear *after* the turn in that story — the light coming back — never during the loss).
- **Premium guardrail (tonal floor):** Glim is never cute, never chatty, never merch-mascot-hyperactive. It does not speak, wink, or dance. It is light behaving kindly. If a use makes a 48-year-old woman feel marketed-at-like-a-child, it dies. The magic stays atmospheric; the promise stays concrete.

## Designer Handoff Brief (self-contained, for Marc)

**Project:** Fairy Flame mascot/personality system — "Glim" (light-wisp character) + "Twin Wicks" (liitt wordmark treatment).

**Context you need (no other docs required):** Fairy Flame is liitt's microdose gummy for high-functioning women 40–55 whose core promise is "feel like yourself again." Brand world = **The Glow**: the light coming back on inside her. Visual system is locked and dark-luxe: bg `#0B0A1C`, surfaces `#1C1A3A`/`#252247`, gold `#E8B84A` (hover `#F5C542`), indigo `#4B40C9`, raspberry `#B23A6E`; fonts Bricolage Grotesque / Newsreader / Hanken Grotesk; glow shadows exist as tokens (`0 8px 30px rgba(232,184,74,.45)` and `.22`). Tone: premium, intimate, A24-warm — never stoner, never cutesy.

**Deliverables:**
1. **Glim character sheet** — the wisp at 3 scales (favicon-size spark / UI-size / hero), 5 behavior states (resting pulse, approach-brighten, respectful dim, guiding drift with particle trail, relight), on dark surfaces only. No face in base form; an optional "suggested figure inside the light" exploration for hero use.
2. **Twin Wicks wordmark revision** — liitt's ii dots as lit wicks: static (soft glow) + animated (flicker-on, breathe, relight) variants; must degrade to flat print (foil/spot-gloss spec) and to the existing knockout/brightness-0 logo rule.
3. **Motion spec** — pulse rate ≈ calm breathing (~10s cycle); brighten/dim eases; nothing bouncy or fast.
4. **File formats:** SVG masters + Lottie/MP4 for motion states + PNG fallbacks; favicon set (32/180/512) can derive from the Twin Wicks spark (note: brand kit already lists favicons as pending — this completes that task).

**Hard rules:** never white text on gold; Glim never appears in claims/lab/price/guarantee contexts; no faces on the gummy; nothing that reads "kids' brand" — reference register: Studio Ghibli soot-sprite restraint, not Pixar sidekick.
