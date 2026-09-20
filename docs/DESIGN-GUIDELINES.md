# Faces — Design Guidelines

*The look and feel of the source galleries, applied to a people index. This file is the source of truth for the UI; `static/index.html` implements it and nothing else.*

**Provenance.** Tokens below were read from the reference photographer site's live stylesheets (SmugMug theme, `Roboto` at weight 400) on 2026-09-20. Where the source site had no opinion — progress states, two-tier match trays, face crops — the rule extends the same vocabulary rather than inventing a new one.

---

## 1. Principles

1. **The photos are the design.** Chrome recedes; a light neutral field, thin hairlines, one accent. Nothing competes with a face or a photo.
2. **Quiet typography.** One family, regular weight everywhere — including headings. Hierarchy comes from size and space, never from bold.
3. **Flat and square.** No shadows, no rounded cards, no gradients. Edges are hairlines or nothing.
4. **Honest states.** Progress, "possible" matches, exclusions and failures are always visible and labeled — in the same quiet voice, not with alarm colors.
5. **Light only.** The source site has no dark mode; neither does Faces.

---

## 2. Tokens

### Color

| Token | Value | Use |
|---|---|---|
| `--page` | `#f0f0f0` | Page background |
| `--surface` | `#f7f7f7` | Panels, inputs, list rows |
| `--surface-2` | `#ffffff` | Photo tiles while loading, selected states |
| `--ink` | `#0d0b0b` | All primary text, headings, links |
| `--ink-2` | `#7a7a7a` | Secondary text, captions, counts |
| `--ink-3` | `#bdbdbd` | Placeholders, disabled |
| `--hair` | `#dedede` | Hairline borders |
| `--hair-2` | `#d6d6d6` | Input borders, stronger dividers |
| `--accent` | `#6385ff` | The one accent: primary buttons, focus, progress, active tile |
| `--accent-hover` | `#4a63bd` | Button hover |
| `--accent-down` | `#4259ab` | Button pressed |
| `--overlay` | `rgba(205,207,210,.6)` | Tile info overlay (source: `.sm-tile-info`) |
| `--overlay-ink` | `rgba(13,11,11,.6)` | Text on the overlay |
| `--danger` | `#cc1e1e` | Errors only. Never for emphasis |

No other colors. "Possible" matches are *not* amber — they are the same neutral, in a separate, labeled section. Status is carried by words, not hue.

### Type

| | |
|---|---|
| Family | `Roboto, Helvetica, Arial, sans-serif` |
| Weights | **400** for everything. 300 is permitted for the wordmark only |
| Base | 15px / 1.55 |
| Wordmark | 22px, weight 300, letter-spacing .12em, uppercase |
| Page title (h1) | 28px, weight 400, letter-spacing −.005em |
| Section (h2) | 17px, weight 400 |
| Caption / meta | 12–13px, `--ink-2` |
| Label (pill) | 11px, uppercase, letter-spacing .08em |

### Space & shape

| | |
|---|---|
| Radius | **0** everywhere. (2px on inputs is tolerated; nothing else.) |
| Hairline | 1px `--hair` |
| Content width | 1200px max, 24px gutters (16px at phone width) |
| Vertical rhythm | 8px base; sections separated by 40px |
| Grid gap | 6px in the face wall, 8px in photo grids — tight, like a gallery |
| Shadows | None |

### Motion

Opacity and border-color only, 120ms ease. No transforms, no lifts. A tile shows its overlay on hover; nothing moves.

---

## 3. Layout

```
┌──────────────────────────────────────────────────────────────┐
│ FACES                                        Galleries       │  ← header, hairline below
├──────────────────────────────────────────────────────────────┤
│ Gallery title (h1, regular)                                  │
│ source URL · status line                                     │
│ ─────────────────────────────────── progress (2px accent) ── │
│                                                              │
│ Find me                          [ drop a selfie ]           │
│                                                              │
│ People · 210                                                 │
│ ▢ ▢ ▢ ▢ ▢ ▢ ▢ ▢ ▢ ▢ ▢ ▢   square crops, 6px gap             │
│                                                              │
│ Person 1 · 142 photos                              close     │
│ ▭ ▭ ▭ ▭   4:3 tiles, hover overlay "Open original ↗"         │
└──────────────────────────────────────────────────────────────┘
```

- **Header** is a single line: wordmark left, one text link right. It is the only element with a full-width hairline.
- **Home** is the gallery list: rows on `--surface`, hairline-separated, each with title / URL / meta and the short link on the right.
- **Gallery page** stacks: title → status → selfie → wall → person. Sections appear as they become relevant; nothing is a modal.

---

## 4. Components

**Button (primary).** `--accent` background, white text, 0 radius, 11px 18px padding, weight 400. Hover `--accent-hover`, pressed `--accent-down`, disabled 50% opacity. One per view.

**Button (text).** `--ink` text, no background, underline on hover. Used for *close*, *back*, *copy link*.

**Input.** `--surface` background, 1px `--hair-2`, 0 radius, 12px 14px padding. Focus: border `--accent`, no glow.

**Label (pill).** 11px uppercase, letter-spacing .08em, 1px hairline, 3px 8px padding, 0 radius, `--ink-2` text. Variants change *text only*, not color — except `danger`, which uses `--danger` text for real failures.

**Progress.** 2px bar in `--accent` on a `--hair` track, full content width. Counts beside it in `--ink-2`. Never a spinner.

**Face tile.** Square crop, 0 radius, no border. Count underneath in 12px `--ink-2`. Hover: 1px `--accent` outline. Active: 2px `--accent` outline.

**Photo tile.** 4:3, `object-fit: cover`, 0 radius. Hover: `--overlay` band at the bottom with "Open original ↗" in `--overlay-ink`. Match score, when shown, sits in that same band.

**Gallery row.** `--surface`, hairline border, 14px 16px padding. Title in `--ink`, URL and meta in `--ink-2`, short link in a hairline-boxed `code` on the right with a text-button *Copy link*.

**Empty / error.** Plain sentence in `--ink-2`, centered, 40px padding. Errors use `--danger` text and say what to do next.

---

## 5. Do / don't

| Do | Don't |
|---|---|
| Let a face crop be the largest element on the wall | Put a card around it |
| Use words for state: *ready*, *indexing*, *possible* | Use amber/green/red to mean the same thing |
| Regular weight, larger size for hierarchy | Bold anything |
| One accent, used for one action per view | Accent-colored counts, links, or decoration |
| Tight gallery-style gaps | Generous "app" padding between tiles |
| Hairlines | Shadows, gradients, rounded corners |

---

## 6. Checklist for any UI change

- [ ] Uses only the tokens in §2 — no new hex values.
- [ ] No `font-weight` above 400 anywhere.
- [ ] No `border-radius` above 2px, no `box-shadow`.
- [ ] Every state (loading, empty, partial, failed) has a plain-language sentence.
- [ ] Works at 375px with 16px gutters and no horizontal scroll.
- [ ] Face wall and photo grid gaps unchanged (6px / 8px).
