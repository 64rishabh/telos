# TELOS Dashboard — Design Implementation Plan

A strict, anti-AI design plan for the frontend dashboard. The reference image lives at `public/website_inspiration.png`. Every section below is binding. Do not improvise outside it.

---

## 1. Reference Image Analysis

The reference is a **mission-control telemetry dashboard** for a CubeSat. Key visual properties:

- **Pure black space background** with a soft static starfield (small white/blue dots, low contrast, no nebula gradients).
- **Saturn silhouette** in the top-left corner (muted tones, partially behind the content) — **intentionally minimal**.
- **Central 3D CubeSat model** (white/light grey, fully visible, slightly off-center-right).
- **Seven subsystem cards arranged radially** around the satellite, each connected by a **thin straight line** to the central model.
- Cards: **EPS (Power)**, **ADCS**, **COMMS**, **OBC**, **PAYLOAD**, **THERMAL**, **PROP**.
- **Top-left header block**: project mark, `TELOS` wordmark, subtitle, navigation tabs (`Mission` / `Overview` / `Runbook`).
- **Bottom legend bar**: status symbols (`◉ Telemetry` / `◉ Command` / `◉ Idle`) and the phrase "Looking for an anomaly?".

---

## 2. Design Language — Strict Rules (DO NOT VIOLATE)

### Anti-AI rules — these are what separate this from a generic "AI generated" look

| Forbidden | Required instead |
|---|---|
| Drop shadows on cards | **No shadows at all** — cards are flat rectangles |
| Colored side/left border accent stripes | **No accent border on card sides** |
| Glowing neon glow effects | **No glow, no blur, no halo** |
| Gradient cards | Cards are **single solid dark fill**, not a gradient |
| Rounded corners with `border-radius > 4px` | Use **sharp 2px corners** (or none) |
| Vibrant saturated accent colors everywhere | Palette is **almost monochrome** with one or two muted accents max |
| Soft pastel backgrounds | Background is **pure black** (`#000`) or near-black (`#0A0A0A`) |
| Big bold sans-serif headings everywhere | Use a **technical mono/condensed sans** feel — uppercase, letter-spaced, smaller weights |
| Animated marquee / scrolling numbers | Numbers are **static, aligned in columns** |
| Glassmorphism / frosted blur | **No blur filters anywhere** |

### Color palette — use only these tokens

```css
--bg-deep         : #000000;  /* pure black, page background */
--bg-panel        : #0E0E10;  /* card fill — almost black, very slight lift */
--bg-panel-edge   : #1C1C1F;  /* 1px hairline around cards, NO accent color */
--ink-primary     : #E8E8E8;  /* main text */
--ink-secondary   : #8A8A8A;  /* labels */
--ink-muted       : #4A4A4A;  /* dividers, axis labels */
--rule            : #1F1F22;  /* thin connecting lines, hairline rules */
--wire            : #2A2A2E;  /* connecting lines from cards to satellite */
--status-ok       : #5FCF80;  /* single muted green — "Telemetry" */
--status-warn     : #E0B040;  /* single muted amber — "Command" */
--status-idle     : #6E6E72;  /* muted grey — "Idle" */
```

> Use at most **three semantic colors** (the status trio). Everything else is monochrome. Do not add purple/cyan/teal accents.

### Typography

- **Headings / labels**: uppercase, `letter-spacing: 0.08em`, weight 400–500, `font-size: 10–12px`.
- **Numeric values**: monospace stack (`'JetBrains Mono', 'IBM Plex Mono', monospace`), `font-size: 14–18px`.
- **Card title**: `11px` uppercase, tracked.
- Avoid display-bold weights; the look is a **technical readout**, not marketing.

---

## 3. Layout Grid (1440px reference frame)

```
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│  HEADER (56px tall, hairline bottom border #1F1F22)                                    │
│  ┌────────────┐ ┌─────┬──────┬───────┐                                                │
│  │ TELOS  ◆   │  CubeSat Telemetry · Live    Missions │ Overview │ Runbook            │
│  └────────────┘                                                                         │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                          │
│      ┌── EPS ──┐                                                ┌── PROP ──┐              │
│      │ Telemetry│                                               │ Telemetry│              │
│      │ Bus V 12.4│                                               │ Press 4.2│              │
│      └──────────┘                                               └──────────┘              │
│        ╱                                                                 ╲               │
│   ┌── ADCS ──┐  ◐ Saturn                                                                │
│   │ Idle     │                                       (blank)                            │
│   │ Yaw  +2° │                                                                        │
│   └──────────┘                                                                         │
│                                                                                          │
│                  ┌──────────────── 3D CUBESAT ────────────────┐                           │
│                  │                                              │                           │
│                  └──────────────────────────────────────────────┘                           │
│                                                                                          │
│   ┌── COMMS ──┐                                                                         │
│   │ Telemetry │                                                                         │
│   │ RSSI -87  │                                                                         │
│   └──────────┘                                                                         │
│                  ┌── OBC ──┐       ┌── PAYLOAD ──┐       ┌── THERMAL ──┐                 │
│                  │ Command │       │ Telemetry   │       │ Telemetry   │                 │
│                  │ CPU 32% │       │ Cam  ON     │       │ +18 °C      │                 │
│                  └─────────┘       └─────────────┘       └─────────────┘                 │
│                                                                                          │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ LEGEND BAR (40px tall, hairline top border)                                              │
│  ◉ Telemetry  ◉ Command  ◉ Idle                          Looking for an anomaly? →       │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Header Specification

**Container**: full-width, 56px high, background `#000`, 1px bottom border `#1F1F22`.

**Left cluster** (padding-left: 32px):

- **Mark**: a small geometric glyph (a `◆` rendered as a 14×14px square diamond) in `--ink-primary`.
- **Wordmark**: `TELOS` — 13px, weight 500, `letter-spacing: 0.16em`, uppercase, `--ink-primary`.
- **Divider**: 1px × 16px vertical rule in `--ink-muted`, margin `0 16px`.
- **Subtitle**: `CubeSat Telemetry · Live` — 11px, `--ink-secondary`, regular weight.
- A tiny **status dot** (6×6 px circle, `--status-ok`) before "Live", with a `pulse` animation 1.5s.

**Right cluster** (padding-right: 32px):

- Three nav tabs: `Mission` · `Overview` · `Runbook`.
- Tab style: 11px uppercase tracked, `--ink-secondary`.
- Active tab (`Mission`): `--ink-primary` with a 1px × 12px underline directly under the text in `--ink-primary`.
- Tabs separated by 24px horizontal spacing.
- **No borders, no fills** on tabs — pure typography.

---

## 5. Subsystem Card Specification (×7)

Each card is positioned **absolutely** on the canvas. Exact pixel positions (re-measured from the reference):

| Card    | Top-Left (px) | Size     |
| ------- | ------------- | -------- |
| EPS     | 168, 156      | 200×180  |
| PROP    | 1072, 156     | 200×180  |
| ADCS    | 132, 412      | 180×180  |
| COMMS   | 132, 704      | 200×180  |
| OBC     | 480, 836      | 180×180  |
| PAYLOAD | 728, 836      | 200×180  |
| THERMAL | 980, 836      | 200×180  |

### Card internal structure (top to bottom)

```
┌────────────────────────────────────┐
│  [ICON]  EPS    ◉ Telemetry       │  ← top row: 12px icon, title, status pill
│  ─────────────────────────────────│  ← 1px rule in #1F1F22
│                                    │
│  BUS V                  12.43 V    │  ← label (left, secondary) + value (right, mono)
│  BATT TEMP              +18.2 °C   │
│  CURRENT                 1.42 A   │
│  STATE OF CHARGE             87 %  │
│                                    │
│  ─────────────────────────────────│  ← hairline
│  ▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░     87 %       │  ← thin progress bar (2px tall)
└────────────────────────────────────┘
```

### Card component rules (must follow exactly)

1. **No `box-shadow`.** Use only a 1px solid border `#1C1C1F`. No `filter: drop-shadow`, no glow.
2. **No accent side borders.** The border is uniformly `#1C1C1F` on all four sides.
3. **No `border-radius`** or at most `2px`. The reference shows crisp, almost-square corners.
4. **No gradient background.** Solid fill `#0E0E10`.
5. **No `backdrop-blur` / glassmorphism.**
6. **Icon style**: line icons only, 14×14px, `stroke-width: 1.25px`, color `--ink-secondary`. No filled icons, no colored icons.
7. **Status pill**: text only (e.g. `◉ Telemetry`), 10px tracked uppercase. The `◉` symbol is a 6px filled circle, color depends on status.
8. **Numeric values**: monospace, right-aligned, `--ink-primary`. Labels left-aligned, `--ink-secondary`, uppercase 9–10px tracked.
9. **Progress bar (if present)**: 2px tall, no rounded ends, fill in status color, track in `#1F1F22`. Never glow.

---

## 6. Connecting Lines (the "wires" from each card to the satellite)

- **1px stroke**, color `--wire` (`#2A2A2E`).
- **Straight lines**, not curved. Each line goes from the **inner edge of the card** to a point on the satellite's body.
- Each line ends in a **3px filled square** (not a circle, not an arrow) where it meets the satellite — same color as the line.
- Lines are drawn **underneath** the cards in z-order but **above** the satellite so they appear to plug into it.

---

## 7. Central 3D Satellite

- Rendered with `@react-three/fiber` + `drei` `<Canvas>`.
- A simple low-poly CubeSat: central body box (1U × 1U × 1U, slightly off-center), two solar panel wings (thin boxes), one small antenna rod.
- **Material**: white-grey `MeshStandardMaterial`, color `#D8D8D8`, roughness 0.85, metalness 0.05. **No emissive, no bloom, no fancy PBR.**
- **Lighting**: a single `directionalLight` from top-front, intensity 0.9, white. One soft ambient `#222`. **No point lights, no rim lights, no neon.**
- **Background inside Canvas**: `null` (transparent) so the page starfield shows through.
- **Animation**: very subtle drift — slow rotation `0.05 rad/s` on Y, `0.02 rad/s` on X. **No spinning marquee, no flashy reveal.**
- **Camera**: perspective, position `[0, 0, 4]`, fov 35. Fixed (not orbiting).
- **Saturn placeholder** in the top-left canvas area: a flat SVG circle + ellipse ring, color `#7A7A82` at 25% opacity, **NOT 3D** — explicitly blank/static per spec.

---

## 8. Starfield Background

- Render with **Canvas2D** or a static SVG `<pattern>` — choose Canvas2D for performance.
- ~600 stars distributed across the full background.
- Each star: 1px to 1.5px radius, color picked randomly from `{ #FFFFFF, #BFD3FF, #FFE6BF }` with alpha 0.3–0.8.
- **No twinkling animation.** **No nebula clouds.** **No gradient sky.**
- Generated **once** on mount, then static.

---

## 9. Bottom Legend Bar

- Full-width, 40px high, `position: fixed; bottom: 0`, background `#000`, 1px top border `#1F1F22`.
- **Left**: three legend items spaced 24px apart:
  - `◉ Telemetry` — dot color `#5FCF80`
  - `◉ Command` — dot color `#E0B040`
  - `◉ Idle` — dot color `#6E6E72`
- **Right**: `Looking for an anomaly?` — 11px, `--ink-secondary`, regular weight, with a small `→` arrow after it.
- All text 10–11px, uppercase, tracked.

---

## 10. Tech Stack & File Layout

```
src/
  app/
      page.tsx                     # Main dashboard route
      layout.tsx
  components/
    dashboard/
      Header.tsx
      SubsystemCard.tsx
      SubsystemCanvas.tsx          # Absolute-positioned cards + connecting lines
      ConnectingLines.tsx
      LegendBar.tsx
    satellite/
      SatelliteScene.tsx           # R3F Canvas wrapper
      SatelliteModel.tsx           # The 3D mesh
    background/
      Starfield.tsx                # Canvas2D
    icons/
      PowerIcon.tsx
      AdcsIcon.tsx
      CommsIcon.tsx
      ObcIcon.tsx
      PayloadIcon.tsx
      ThermalIcon.tsx
      PropIcon.tsx
  data/
    subsystems.ts                  # Mock telemetry data
  styles/
    globals.css                    # CSS variables, resets
    tokens.css                     # Design tokens
public/
  fonts/
    JetBrainsMono-Regular.woff2
    Inter-Regular.woff2
    Inter-Medium.woff2
```

### Recommended libraries

- `next` (or `vite`) + `react` + `typescript`
- `three` + `@react-three/fiber` + `@react-three/drei`
- **No CSS framework** — use **plain CSS modules or vanilla CSS** with the design tokens. **Do NOT use Tailwind for this** — it will steer you toward AI-looking utility soup.

---

## 11. Interaction (minimal, on-spec)

- **Header tabs**: only `Mission` is active. Clicking others does nothing for now (or sets a `useState` — no router change).
- **Cards**: no hover effects. Hovering can change cursor to default. **No scale, no glow, no border highlight on hover.**
- **No tooltips, no modals, no charts** — pure static telemetry readout. The reference shows no interactive elements beyond the nav.

---

## 12. QA Checklist — MUST pass before delivery

A "pass" requires **every** item to be true:

1. ☐ Background is solid black or near-black — no gradient sky, no nebula.
2. ☐ Saturn area is intentionally minimal — a faint outline only, no 3D detail.
3. ☐ 3D satellite is centered, slightly off-center-right (matches reference framing), low-poly, monochrome material.
4. ☐ Seven subsystem cards present in the exact positions given in §5.
5. ☐ Each card has NO drop-shadow, NO accent side-border, NO border-radius > 2px.
6. ☐ Card border is uniformly `#1C1C1F` 1px on all sides.
7. ☐ Card background is solid `#0E0E10` (no gradient, no blur).
8. ☐ Connecting lines are 1px straight, no curves, with small terminal squares at the satellite end.
9. ☐ Typography is uppercase, tracked, monospace for numbers.
10. ☐ Color palette contains only the tokens listed in §2 — no extra hues.
11. ☐ Header is 56px tall, with project mark + `TELOS` + subtitle + 3 nav tabs.
12. ☐ Legend bar is at the bottom, 40px tall, with 3 status types + "Looking for an anomaly?".
13. ☐ No animations except: satellite very slow drift + a single small pulse on the "Live" dot in the header.
14. ☐ No emoji icons inside cards — only thin line icons.
15. ☐ Dev server runs without console errors.

If any check fails, fix and re-verify before reporting completion.

---

## 13. Deliverable

Return:

1. Path of the page file (e.g. `src/app/page.tsx`) and a tree of new files created.
2. A short confirmation that all 15 QA checks passed.
3. A single screenshot or note that the dev server runs without console errors.

**Do not** report "done" until the QA checklist passes.
