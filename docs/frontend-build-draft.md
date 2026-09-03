# Draft BIBLE Updates — Frontend Build

> **This is a draft for user review.** The actual `BIBLE.md` is NOT modified yet.
> Once you approve, I'll apply these edits to the BIBLE, commit, and push.
>
> Branch: `frontend/intitial_dashboard`
> BIBLE section affected: **§3.2 "What we're building now"** — full replacement.

---

## §3.2 — What we're building now (REPLACES the existing section)

> **Strict instructions for the implementing agent.** This
> section is dense, endpoint-specific, and built to be read
> by a fresh agent without prior context. Read every line
> before starting work.

### 1. What this is

The telos backend runs a 4-stage pipeline
(**Detect → Diagnose → Propose → Validate**) and exposes
a single live WebSocket stream from `live/ws_server.py` on
port 8000. We are building the **operator-facing frontend**
that visualizes this stream in real time and renders the
verdict the pipeline produces.

The frontend is a **3-route React app** (intro / dashboard
/ runbook) served by the existing FastAPI server, backed
by Zustand for state, with a **3D rotating satellite model**
at the center of the dashboard. Black-space background
with a star field and a planet/Earth limb behind it. Lines
from each subsystem body on the satellite to its
corresponding **subsystem card** (the cards are NOT
stacked vertically on the right — they are positioned
around the satellite so each card sits next to its
subsystem body; the operator sees the spatial relationship
between the satellite and the data per subsystem).

### 2. Tech stack (fixed)

- **Vite + React + TypeScript** for the build/dev server.
- **React Router** for the 3 routes.
- **Zustand** for state (4 module-level singleton stores).
- **react-three-fiber + drei** for the 3D satellite model.
- **No CSS framework.** Hand-written CSS in
  `src/styles/globals.css` + per-component CSS modules.
  Tokens (colors, spacing) as CSS custom properties.
- **FastAPI serves the built Vite output** from
  `live/static/app/` at `/`. The current `live/static/index.html`
  is replaced by the Vite shell.

### 3. Build phases (THIS is the order — collapsed from a prior 3-phase incremental plan into a single coordinated implementation, with 3 agent-facing reviewable milestones)

> The prior 3-phase plan (Phase 1 = live data, Phase 2 =
> verdict panel, Phase 3 = trajectory) has been collapsed
> into a single coordinated implementation because the
> backend extensions needed by Phase 2/3 are small enough
> to land in one commit up front, and the frontend is
> naturally additive (each commit leaves the app runnable).
> The user has broken the delivery into **3 reviewable
> milestones** (3 git pushes), with the agent's work
> gated between each. The milestones are:

#### Milestone 1 — React router, intro page, 3D satellite, websocket, live data
**Scope:** `frontend/` skeleton, all 3 routes wired, intro
page (static), dashboard with rotating 3D satellite on
black-space background, websocket connection live,
`/stream` tick data flowing into `telemetryStore`,
`ChannelRow` showing `t | value | pred | ewma_error |
threshold` per channel, subsystem cards connected to the
satellite via lines, terminal log streaming tick lines.

**Acceptance:** `npm run dev` works, `npm run build`
outputs to `live/static/app/`, FastAPI serves the Vite
shell at `/`, navigating to `/dashboard` opens the
WebSocket, the 3D satellite rotates, tick lines scroll in
the terminal, each of the 5 subsystem cards shows its
channels' live values and EWMA error color. No runbook
page content yet — it can render a "Runbook page TBD"
placeholder.

**Git push:** user reviews, then push. No verdict
plumbing, no inject modal, no runbook sections.

#### Milestone 2 — Backend verification, inject modal, runbook (9 sections + trajectory)
**Scope:** verify each backend stage (Detect, Diagnose,
Propose, Validate) with unit tests, ensure data and
information flowing through is accurate end-to-end. Wire
the inject modal (`POST /inject_twin_fault`), wire the
`m.twin_verdict` payload into `verdictStore`, write the
log narrative lines on anomaly and verdict arrival,
auto-redirect to `/runbook` 1 second after "RUNBOOK
GENERATED". Render all 9 runbook sections (see §11 below)
and the 3-field trajectory canvas.

**Acceptance:** the full happy path works: inject a shift
on T-1, the terminal logs the anomaly → diagnose →
propose → validate → runbook sequence, the route
navigates to `/runbook` after 1s, all 9 sections render
with accurate data, the trajectory canvas shows
predicted vs baseline for 3 fields over 1h.

**Git push:** user reviews, then push.

#### Milestone 3 — Polish, intro content, inconsistencies, final review
**Scope:** final intro page content (project goals, title,
summary — to be written by the user later, agent leaves
the route scaffolded with placeholder text), fix any
inconsistencies discovered during Milestone 2, mobile-
responsive pass, color token stabilization, accessibility
checks, final review.

**Git push:** final review, then push.

### 4. Endpoints the frontend will hit

| Method | Path | When | Request body | Response |
|---|---|---|---|---|
| `GET` | `/` | On page load | — | Vite shell (HTML) |
| `WS` | `ws://<host>/stream` | On first `/dashboard` mount | — | Stream of JSON messages at 5 Hz |
| `POST` | `/inject_twin_fault` | When operator clicks "Inject" in the inject modal | `{kind, channel, magnitude}` (JSON) | `{status, fault_type, ...}` — current behavior, unchanged |
| `GET` | `/` (Vite SPA fallback) | On any client-side route (`/dashboard`, `/runbook`) | — | Same Vite shell; React Router handles the route |

No other endpoints. The frontend does **not** call
Diagnose / Propose / Validate directly — it only reads
`twin_verdict` from the WebSocket broadcast.

### 5. WebSocket message shape (every tick at 5 Hz)

```ts
type TickMessage = {
  t: number;                  // live-stream tick index (1-based)
  channel: ChannelId;         // "P-1" | "P-2" | "B-1" | "T-1" | "T-2" | "A-1" | "G-1" | "D-1"
  value: number;              // actual sensor reading
  pred: number;               // LSTM prediction
  error: number;              // EWMA-smoothed |value - pred|
  threshold: number;          // current dynamic threshold from find_epsilon
  injected: boolean;          // true on ticks that consumed a queued injection
  anomaly: null | {           // null on most ticks
    score: number;            // severity score from score_anomalies
    seq: [number, number];    // (start_t, end_t) in live-stream indices
    channel: ChannelId;       // the channel the operator injected on
  };
  twin_verdict: null | Proposal;  // null on most ticks; populated on the next tick after an inject that triggered a verdict
};
```

The `kind` field (operator's injection kind) is **NEVER**
included in the WebSocket message in Phase 1 — see §10.

### 6. `Proposal` shape (full payload, exactly what the runbook renders)

The full BIBLE §2 / §8 / §9 contract. The frontend's
TypeScript `Proposal` type matches this shape 1:1:

```ts
type Proposal = {
  cause: Cause;                                 // enum from digital-twin/twin/procedures.py
  cause_score: number;                          // 0..5, threshold 0.5
  procedure: Procedure;                        // enum
  procedure_params: Record<string, any>;       // the chosen procedure's params
  risk_score: number;                           // 0..1
  validation: ValidationResult;                 // see below
  verdict: Verdict;                             // OK | REJECT | INCONCLUSIVE
  candidates_ranked: Array<{                    // validation-based ranking (D-12)
    procedure: Procedure;
    risk_score: number;
  }>;
  // -- runbook enrichment (added in Milestone 1 backend commit) --
  affected_subsystems: SubsystemId[];           // from Cause.affected_subsystems()
  expected_channels: ChannelId[];               // from Cause.expected_channels()
  risk_class: "low" | "medium" | "high" | "critical";
  approval_required: "auto" | "operator" | "director";
  procedure_description: string;                // human-readable
  symptom_window: Array<{                       // the SymptomEvents the bridge seeded
    t: number;
    channel: ChannelId;
    subsystem: SubsystemId;
    kind: "spike" | "shift" | "dropout" | "anomaly";
    score: number;
    seq: [number, number];
  }>;
  twin_state_endpoints: {                       // last value of each field in predicted_trajectory
    battery_soc: number;
    battery_voltage_v: number;
    battery_temp_c: number;
    payload_temp_c: number;
    electronics_temp_c: number;
    pointing_error_deg: number;
    link_margin_db: number;
  };
  // -- trajectory (3 fields for the runbook canvas) --
  trajectory: {
    battery_soc: Array<[number, number]>;       // 31 points, [[t0,v0], ..., [t60,v60]]
    battery_temp_c: Array<[number, number]>;
    payload_temp_c: Array<[number, number]>;
    baseline: {                                 // same shape, no-procedure baseline
      battery_soc: Array<[number, number]>;
      battery_temp_c: Array<[number, number]>;
      payload_temp_c: Array<[number, number]>;
    };
  };
};
```

`Verdict`:

```ts
type Verdict = {
  proposal_id: string;            // "" in Phase 1 (Phase 3: Merkle chain)
  status: "OK" | "REJECT" | "INCONCLUSIVE";
  reason: string;                 // human rollup, e.g. "thermal_throttle_payload OK for thermal_heater_stuck_on: risk=0.12 < threshold=0.30"
  per_step_outcomes: Array<Record<string, any>>;  // constraint violations (populated for REJECT)
  twin_simulation_digest: string; // "" in Phase 1
  notes: string[];
};
```

`ValidationResult` (subset used by the frontend):

```ts
type ValidationResult = {
  feasible: boolean;
  risk_score: number;
  violations: Array<{ field: string; at_t: number; value: number; limit: number; }>;
  predicted_trajectory: Record<string, Array<[number, number]>>;  // all 7 state fields, 31 points each
  baseline_trajectory: Record<string, Array<[number, number]>>;
  summary: string;
};
```

### 7. Channel → subsystem → state-field map (EXACT — copy this into the frontend)

The frontend has **5 subsystem cards**. Each card knows
which channels it displays, which `twin_state_endpoints`
field it shows in its footer, and which range check
governs its green/amber/red color.

| Subsystem card | Channels displayed (in this order) | State field(s) shown in footer | Range check (green / amber / red) |
|---|---|---|---|
| **EPS** | P-1, P-2 | `battery_soc`, `battery_voltage_v` | SoC: ≥0.5 / 0.2–0.5 / <0.2. Voltage: 26–30V / 24–26 or 30–32 / <24 or >32 |
| **Battery** | B-1 | `battery_temp_c` | 0–40°C / –10–0 or 40–55 / <–10 or >55 |
| **Thermal** | T-1, T-2 | `payload_temp_c`, `electronics_temp_c` | per channel's nominal range; amber at edges, red at constraint violation |
| **ADCS** | A-1, G-1 | `pointing_error_deg` | <1° / 1–5° / >5° |
| **Comms** | D-1 | `link_margin_db` | >6dB / 3–6dB / <3dB |

The full channel → state-field cross-mapping (for
debugging, NOT for the UI):

| Channel | Subsystem card it lives in | Twin state field it influences |
|---|---|---|
| P-1 | EPS | `battery_voltage_v` (primary) |
| P-2 | EPS | `battery_soc` (charge rate) |
| B-1 | Battery | `battery_temp_c` |
| T-1 | Thermal | `payload_temp_c` |
| T-2 | Thermal | `electronics_temp_c` |
| A-1 | ADCS | `pointing_error_deg` |
| G-1 | ADCS | `pointing_error_deg` (via reaction wheel torque) |
| D-1 | Comms | `link_margin_db` |

### 8. The 3D satellite model (what to build)

Rendered with **react-three-fiber** inside a `<Canvas>`.
The scene is:

- **Background:** black space with a subtle star field
  (small white dots, randomly placed, slow parallax on
  mouse orbit) and an **Earth limb / planet silhouette**
  in the lower-left distance (a large textured sphere
  with `meshBasicMaterial`, partially visible at the
  bottom-left of the viewport).
- **Satellite** floats in the center, slowly rotating on
  its Y-axis (auto-rotation, ~0.1 rad/sec). Mouse-orbit
  enabled (drei `<OrbitControls>` with damping).

The satellite is composed of these meshes (all
`meshStandardMaterial`, no GLTF import — primitive
geometry only):

| Mesh | Geometry | Position | Color (per-subsystem) |
|---|---|---|---|
| Body | BoxGeometry(1, 1, 1.5) | center | worst-case across all subsystems |
| SolarPanel L | BoxGeometry(3, 0.05, 1) | (-2, 0, 0) | EPS |
| SolarPanel R | BoxGeometry(3, 0.05, 1) | (+2, 0, 0) | EPS |
| BatteryIndicator | BoxGeometry(0.4, 0.4, 0.1) | (0, 0, -0.8) | Battery |
| Radiator ×4 | BoxGeometry(0.05, 0.6, 0.6) | (±0.55, 0, 0), (0, 0, ±0.8) | Thermal |
| WheelCone ×4 | ConeGeometry(0.15, 0.3) | (±0.5, ±0.5, 0) | ADCS |
| Antenna | CylinderGeometry(0.05, 0.05, 0.4) | (0, 0.7, 0) + small disc | Comms |

**Lines from satellite to subsystem cards.** Each
subsystem card has a fixed 2D position on the viewport
(an absolute `position: absolute` style). A `<line>`
or `<svg>` line is drawn from the screen-projected
center of the subsystem mesh to the card's anchor
point. The line highlights (brighter color) on hover of
either the mesh or the card. **Cards are NOT stacked
vertically on the right** — they are positioned around
the satellite so each card sits next to its subsystem
body (e.g. EPS card on the left, near the solar panels;
Comms card at the top, near the antenna; Battery card
at the bottom, near the battery indicator; Thermal
card on the right, near the radiators; ADCS card
centered, near the wheel cones). The exact card
positions are tuned at build time; the constraint is
"no vertical stack."

**Color logic:** every frame, the SatelliteScene reads
`twinStateEndpoints` from `telemetryStore` and updates
each subsystem mesh's `material.color` via
`useFrame`. The body's overall color is the worst-case
(red if any subsystem is red, else amber if any is
amber, else green if any is green, else neutral gray
when no verdict has arrived).

### 9. The subsystem cards (the source of truth for per-subsystem state)

Each card is a fixed-position panel (~280px wide, varies
in height) showing:

- **Header** — subsystem name (e.g. "EPS") in human
  form ("Electrical Power"), connection status dot
  (green if any channel's latest EWMA error is below
  threshold, amber if within 50% of threshold, red if
  above).
- **ChannelRows** — one per channel the subsystem owns
  (see §7). Each `ChannelRow` shows:
  - Channel name (e.g. "P-1") + subsystem tag
  - Latest value (big numeric readout with units)
  - Sparkline (last 60 ticks, 120×30px) — actual (blue)
    + pred (green) overlaid
  - EWMA error color indicator (green / amber / red
    based on `error` vs `threshold`)
  - Anomaly badge (small red chip) if the latest sample
    on this channel has an `anomalyScore`
- **State footer** — shows the most recent verdict's
  `twin_state_endpoints` for this subsystem's field(s),
  formatted as e.g. "State: SoC 87% · V 28.4V" with the
  green/amber/red color from §7. Before any verdict:
  "**State: awaiting first verdict**" in neutral gray.

### 10. The `kind` rule (NEVER show the operator's injection kind as a label)

The WebSocket's anomaly `kind` field is hardcoded to
`"anomaly"` in Phase 1 (BIBLE §2 Stage 1, forward-
looking). The operator's injection kind (spike/shift/
dropout) is the operator's *action*, not the system's
*detection* or *diagnosis*. Three separate visual regions
in the dashboard, no cross-pollination:

1. **Canvas / per-channel display** renders "Anomaly ·
   score X · seq [a, b] · channel Y" only. Never the
   operator's kind.
2. **Inject modal** shows the operator's chosen kind as
   a *form input* (the operator picked it; it's their
   action), not as a *detection claim* on the canvas.
3. **Verdict / runbook** renders the system's own
   `cause` (e.g. "thermal_heater_stuck_on") from
   Diagnose's output. Never the operator's kind.

The `symptom_window[].kind` in the verdict payload is
internal to Diagnose and is shown in the runbook's
Evidence section as a debug aid, NOT on the dashboard's
anomaly badge.

### 11. The runbook page (Milestone 2 — full BIBLE §2 / §8 / §9 contract)

Route: `/runbook`. Reads `verdictStore.latest`. Renders
9 sections in order:

1. **Verdict status badge** — `OK` (green) / `REJECT`
   (red) / `INCONCLUSIVE` (amber), from `verdict.status`.
2. **Verdict reason** — one-line from `verdict.reason`.
3. **Diagnosis** — cause (humanized), cause score bar
   (0–5 with the 0.5 `MIN_DIAGNOSE_SCORE` marker),
   affected subsystems chips (from
   `proposal.affected_subsystems`), expected channels
   chips (from `proposal.expected_channels`).
4. **Procedure** — procedure (humanized), description
   (from `proposal.procedure_description`), parameters
   table (key-value with units, from
   `proposal.procedure_params`), risk score gauge
   (0–1 with the 0.3 `RISK_THRESHOLD_INCONCLUSIVE`
   marker), risk class pill (color-coded by
   `proposal.risk_class`), approval required pill
   (color-coded by `proposal.approval_required`).
5. **Twin prediction** — linked small multiples
   trajectory canvas: 3 canvases stacked vertically,
   all sharing the x-axis (0–60 min, 31 points). Fields:
   `battery_soc`, `battery_temp_c`, `payload_temp_c`.
   Each canvas shows predicted (solid) and baseline
   (dashed) series, with constraint lines drawn as
   horizontal threshold lines. An injection band drawn
   as a translucent vertical band at t=0.
6. **Why this procedure** — candidates ranked table
   (validation-based ranking per D-12). Each row:
   procedure | risk_score | highlighted if it's the
   winner.
7. **Evidence** — symptom window list
   (`proposal.symptom_window` rendered as
   `t | channel | subsystem | kind | score` rows) and
   constraint violations table (only populated for
   REJECT verdicts; renders a red-bordered table with
   the field name and the timestep it broke).
8. **Approval** — Approve / Reject buttons (Milestone 2
   stub: the buttons are visible, tappable, colored by
   risk class; on click they show a toast: "Approval
   deferred to Phase 2 (OPA/Rego). Verdict recorded.").
   This is intentional: the UI rehearses the Phase 2
   surface today so the operator's mental model carries
   forward unmodified.
9. **Footer metadata** — fault_id (e.g. "F-001"),
   timestamp, runbook_id stub ("(Phase 3)"). The
   `twin_simulation_digest` and `proposal_id` fields
   are empty strings in Phase 1; the footer reserves
   the slot.

### 12. The terminal log (the operator's narrative surface)

Bottom of the dashboard, ~200px tall, monospace, dark
background, auto-scroll, 200-line ring buffer. Two
kinds of lines:

- **Tick lines** (one per WS message, ~5/sec):
  `14:23:01.234  t=1234  ch=P-1  val=28.4  pred=28.3  err=0.10  thr=0.15`
- **Narrative lines** (on anomaly and verdict arrival):
  ```
  >>> ANOMALY DETECTED  score=12.34  seq=[1238,1240]  ch=P-1
  >>> DIAGNOSE  cause=eps_load_step  score=1.5
  >>> PROPOSE   procedure=eps_shed_non_essential_load  risk=0.12  rank=1/2
  >>> VALIDATE  verdict=OK
  >>> RUNBOOK GENERATED · cause=eps_load_step
                     procedure=eps_shed_non_essential_load
                     → redirecting to /runbook in 1s
  ```

The narrative lines are appended at the time the WS
message arrives (not at the time of the HTTP inject),
because the backend attaches the verdict to the **next
anomaly tick** (`live/ws_server.py:303-304`). The 1s
pause between "RUNBOOK GENERATED" and
`navigate('/runbook')` lets the operator see the
message before the route swap.

### 13. State management (4 Zustand stores, all module-level singletons)

- **`telemetryStore`** — `channels: Record<ChannelId,
  Sample[]>` (ring buffer, max 800), `anomalies:
  Array<...>` (max 100), `twinStateEndpoints:
  Record<SubsystemId, { ...fields } | null>`.
- **`verdictStore`** — `latest: Proposal | null`,
  `history: Proposal[]` (max 20).
- **`logStore`** — `lines: string[]` (max 200),
  `appendTick(msg)`, `appendNarrative(text)`, `clear()`.
- **`connectionStore`** — `status: "connecting" |
  "live" | "disconnected" | "failed"`, totalTicks,
  totalAlerts.

The WebSocket client (`lib/wsClient.ts`) is also a
module-level singleton. It opens once on first
`/dashboard` mount, never closes on route change,
exponential backoff reconnect (1s → 2s → 4s → … → 30s,
max 10 attempts). On reconnect, the backend's producer
loop restarts from `state.error_stream.t`; the client
sees a gap in the data, the ring buffer's left edge is
preserved, the gap is visible as a break in the line.

### 14. Backend extension (Milestone 1, single commit before frontend work)

5 small surgical edits, all in a single commit:

1. `digital-twin/twin/propose.py` — extend
   `Proposal.to_dict()` with `affected_subsystems`,
   `expected_channels`, `risk_class`, `approval_required`,
   `procedure_description`, `symptom_window`, and
   `twin_state_endpoints` (last value of each field in
   `validation.predicted_trajectory`).
2. `live/twin_bridge.py` — pass `symptom_window` to
   `propose()` and attach 3-field trajectory arrays
   (`battery_soc`, `battery_temp_c`, `payload_temp_c`,
   each as 31-point `[[t,v], ...]`) to the verdict dict,
   plus the same 3 fields for the no-procedure baseline.
3. `live/ws_server.py` — add `channel` and `threshold`
   to every tick message; add `channel` to the anomaly
   payload; add `state.current_channel` and
   `state.latest_fault_channel` to `AppState`.
4. `live/error_stream.py` — add `latest_epsilon`
   property (1 line) so the threshold is broadcastable.
5. `digital-twin/twin/tests/test_propose.py` — one new
   test asserting the enriched payload fields.

### 15. What the frontend is NOT

- Not a new pipeline stage. It's a *window* onto the
  existing Detect → Diagnose → Propose → Validate
  pipeline.
- Does not run a new simulator. The twin still runs
  only at injection time, server-side, per BIBLE §2.
- Does not call Diagnose / Propose / Validate directly.
  Only reads `m.twin_verdict` from the WS broadcast.
- Does not modify the cause or procedure catalogs.
  Catalogs are repo-only (D-5).
- Does not implement Phase 2 (OPA/Rego) or Phase 3
  (Merkle chain). Approval buttons are stubbed in
  Milestone 2.

### 16. Test gate

- **Backend:** the existing 4 `test_injection_bridge.py`
  tests stay green; one new `test_propose.py` test for
  the enriched payload. The 2 pre-existing keras-gated
  failures (`test_load_predict`,
  `test_inject_shift_triggers_anomaly_over_websocket`)
  remain expected and unchanged.
- **Frontend:** vitest unit tests for `lib/humanize.ts`,
  `lib/riskClass.ts`, store reducers, log formatter.
  No canvas tests (rendering verified manually).

### 17. Working artifact

`/home/rishabh/.claude/plans/snug-forging-tome.md` —
the detailed implementation plan. Source of truth for
the build order; this BIBLE section is the human-
readable summary for the implementing agent.
