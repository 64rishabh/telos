# telos — The Bible

> The complete, ever-updated reference for the telos project.
> Written for two audiences: the human owner, and any future AI agent that
> needs to understand what telos is, how it's built, why each decision was
> made, and what's planned for the future.
>
> **If you change the architecture, update the Bible in the same commit.**
> The Bible is the project's source of truth for "what is telos and why."
> Code may drift, but the Bible must not.

---

## Table of contents

1. [What telos is](#1-what-telos-is)
2. [The 7-stage pipeline](#2-the-7-stage-pipeline)
3. [Current implementation state](#3-current-implementation-state)
4. [Decisions we took (and the alternatives we rejected)](#4-decisions-we-took-and-the-alternatives-we-rejected)
   - [D-1 through D-15 (as previously documented)](#d-1-through-d-15-as-previously-documented)
   - [D-16. Catalog-level editorial fields](#d-16-catalog-level-editorial-fields-effort_score-mission_impact-reversibility)
   - [D-17. Propose runs top-K simulations in parallel](#d-17-propose-runs-top-k-simulations-in-parallel-and-streams-progress-to-the-frontend)
   - [D-18. RankedCandidate replaces the candidates_ranked tuple](#d-18-rankedcandidate-replaces-the-candidates_ranked-tuple)
   - [D-19. Propose ranks by multi-axis lexicographic key](#d-19-propose-ranks-by-multi-axis-lexicographic-key-not-by-risk_score)
5. [The hand-authored knowledge catalogs](#5-the-hand-authored-knowledge-catalogs)
6. [The digital twin](#6-the-digital-twin)
7. [Trust and approval](#7-trust-and-approval)
8. [Audit and runbooks](#8-audit-and-runbooks)
9. [The human-in-the-loop model](#9-the-human-in-the-loop-model)
10. [Future branches (Phase 2/3/4)](#10-future-branches-phase-234)
11. [Repo conventions](#11-repo-conventions)
12. [Tooling and dependencies](#12-tooling-and-dependencies)
13. [Glossary](#13-glossary)
14. [Incoming: the digital twin branch](#14-incoming-the-digital-twin-branch)

---

## 1. What telos is

### The problem

Modern satellite and constellation operations are telemetry-heavy but
human-triage-heavy. Anomaly *detection* is largely solved (LSTM-based
streaming detectors work well in production at NASA, ESA, and most
commercial operators). What is not solved is everything *after* detection:

- **Diagnosing** the cause across interdependent subsystems
  (power affects comms affects thermal affects …).
- **Proposing** a recovery procedure that is known-safe and known-effective
  for that cause.
- **Validating** that the procedure will not make things worse, before
  committing it.
- **Approving** the action with the right human-in-the-loop gate for its
  risk class.
- **Executing** the procedure with per-step receipts.
- **Verifying** the outcome with a tamper-evident, replayable runbook that
  survives the 7-year retention window regulators and insurers require.

Each of these steps is a separate tool, a separate team, a separate context
switch in the operator's day. As constellations scale from 1 to 10 to 100
to 1000 satellites, the human-triage model breaks.

### The intended outcome

**telos is a single multi-agent pipeline that takes you from a raw telemetry
tick to a verified, auditable runbook with a human-in-the-loop safety model
at every irreversible step.** The agents are specialized (each does one
stage well), the orchestration is deterministic (not LLM-mediated), and
the LLM (when added in Phase 4) is a narrator — never an authority.

### Scope (what telos is and is not)

**telos is:**
- A reference architecture for ops centers that want to automate the
  post-detection pipeline.
- A runnable prototype demonstrating Detect → Diagnose → Propose →
  Validate against synthetic satellite telemetry.
- A vehicle for exploring how the trust, approval, and audit pieces fit
  together before they get wired into a real ops center.

**telos is not:**
- A production-ready ops system. No real flight hardware. No real
  regulatory certification.
- An LLM-agents framework. The LLM is a future narrator, not a peer
  agent. The architecture is LLM-agnostic.
- A physics-accurate simulator. The twin is a deterministic state
  machine — enough to demonstrate the validation contract, not enough to
  replace mission sims.
- A general anomaly-detection library. Detection (Stage 1) is reused from
  the existing `live/` pipeline; telos focuses on Stages 2–7.

---

## 2. The 7-stage pipeline

```
[Telemetry source: live/generator.py producer or replay]
                  | tick(actual, predicted)
                  v
1. DETECT      live/error_stream.py — LSTM + EWMA + dynamic threshold
               emits AlertEvent {t, score, seq, kind}
                  |
                  v  (re-typed by live/twin_bridge.py)
2. DIAGNOSE    digital-twin/twin/diagnose.py — pattern-matches
               SymptomEvent window against the Cause registry
               returns ranked CandidateCause[] with evidence chain
                  |
                  v
3. PROPOSE     digital-twin/twin/propose.py — VALIDATION-BASED
               RANKING with catalog-level pre-filter: trims the
               candidate set to top-K by effort_score /
               mission_impact / reversibility (D-16), then calls
               validate_procedure() in parallel in a
               ThreadPoolExecutor, ranks by risk_score, returns
               Proposal with the full ranking + per-candidate
               catalog fields attached (D-17, D-18)
                  |
                  v
4. VALIDATE    digital-twin/twin/validate.py — projects the twin
               forward horizon_s with the procedure applied,
               TWO-TRAJECTORY model (predicted vs no-action baseline)
               returns ValidationResult
                  |  wrapped by
                  v
            digital-twin/twin/verdict.py — to_verdict() maps
               ValidationResult to Verdict = OK | REJECT | INCONCLUSIVE
                  |
                  v
─────────────────────────────────────── Phase 1 boundary ───────────────────────────────────────
                  |
                  v
5. APPROVE   (deferred) policy-gated operator approval; 4-class risk; LLM cannot reach this
6. EXECUTE   (deferred) walks approved procedure through mock CommandBus; per-step receipts
7. VERIFY    (deferred) Merkle-chained runbook + content-addressed evidence + replay harness
```

The Phase 1 boundary is the line between "the system can propose what
should happen" and "the system can act on what should happen." Stages
1–4 produce structured recommendations; Stages 5–7 turn recommendations
into auditable actions. The boundary is structural — there is no code
path that crosses it in Phase 1.

### Stage 1 — Detect

**What it does:** continuously scores live telemetry against an
LSTM-predicted baseline, smooths the prediction error with an EWMA
filter, runs the telemanom thresholding/pruning/scoring pipeline on
the smoothed-error window, and emits a per-channel deviation signal
when the smoothed error breaks the dynamically-chosen threshold.

**Where it lives:** the `live/` package. Five modules totaling ~1,000
lines:
- `live/generator.py` (125 lines) — synthetic sinusoid + thread-safe
  injection queue. The HTTP `/inject` endpoint queues a
  `spike | shift | dropout` anomaly that the producer loop picks up on
  the next tick. `shift` auto-expires after `shift_duration_ticks=30`
  so the demo shows both the onset and the recovery.
- `live/model_runner.py` (81 lines) — wraps the LSTM (`keras.models.load_model`)
  with a `predict(window) -> np.ndarray` interface. The actual model
  is a 2-layer LSTM(80) with dynamic thresholding (Hundman et al., 2018).
- `live/error_stream.py` (445 lines) — the streaming port of the
  telemanom `Errors` class. Maintains an EWMA-smoothed error buffer
  (`deque(maxlen=l_s + n_predictions)`) and runs the four-stage
  pipeline (`find_epsilon` → `compare_to_epsilon` → `prune_anoms` →
  `score_anomalies`) on the trailing window each tick. This is the
  *port* — the math is identical, the I/O is per-tick instead of batch.
- `live/ws_server.py` (362 lines) — the FastAPI surface. `GET /`
  serves the dashboard HTML; `WS /stream` broadcasts one JSON
  message per tick; `POST /inject` queues an anomaly; `POST
  /inject_twin_fault` runs the Phase 1 pipeline (see "Bridge" below).
- `live/config.py` (95 lines) — the `LiveConfig` dataclass. Default
  tick rate is 5 Hz (`TICK_HZ = 5.0`); the integration test tunes
  this to 25-50 Hz so the demo warms up fast.

**What it consumes:** `(actual_value, predicted_value)` per tick, plus
the LSTM's window of past values.

**What it produces:** `AlertEvent {t, score, seq, kind}` (defined in
`live/error_stream.py:41-46`). The `t`, `score`, and `seq` fields are
the live-stream indices and the severity score from
`score_anomalies`. The `kind` field is forward-looking ("anomaly" today,
"shift"/"spike"/"dropout" once the generator attaches kind metadata).

**Important constraint:** the live `AlertEvent` does **not** carry
`channel` or `subsystem` — those are derived later by the bridge from
the `channel_hint` passed in by the caller (typically the
`channel` query param on `/inject_twin_fault`). A Phase 2 polish item
is to plumb `channel` into `AlertEvent` itself (see §3.3 and
the `alerts_to_symptom_events` legacy hardcode in
`live/twin_bridge.py:138-180`).

**LLM involvement:** none. Detect is pure-Python + numpy + pandas + the
keras LSTM. No LLM SDK is imported anywhere in `live/`.

### Stage 2 — Diagnose

**What it does:** given a window of `SymptomEvent`s, score every Cause
in the registry against the window's evidence and return the ranked
list. The score is a sum of three terms; the threshold is
`MIN_DIAGNOSE_SCORE = 0.5`.

**Where it lives:** `digital-twin/twin/diagnose.py` (152 lines).
Defines the `SymptomEvent` dataclass (frozen, with `channel`,
`subsystem`, `kind`, `score`, `seq`, `ts`), the `CandidateCause`
dataclass, the scoring constants, and the `diagnose(window: list[SymptomEvent])
-> list[CandidateCause]` function.

**What it consumes:** a window of `SymptomEvent`s. There is no hard
window-size; the consumer (the bridge) decides how many events to
pass. The `slight_shift_demo.py` script passes 3; the
`phase1_demo_deep.py` script passes 3; the live pipeline passes
whatever the live ErrorStream has flagged in its recent window.

**What it produces:** `list[CandidateCause]` ranked by score, where
each `CandidateCause` carries `cause: Cause`, `score: float`,
`matched_events: list[SymptomEvent]`, and
`evidence_subsystems: list[str]`. The top of the list is the system's
best guess; ties are broken by stable sort (catalog order).

**Scoring rubric** (per Cause, per event in the window; the actual
`+0.25` / `+0.5` / `+1.0` numbers are in `diagnose.py:13-20`):

- **`+1.0`** if `SymptomEvent.channel in Cause.expected_channels()`
  — the strongest signal: an anomaly on a channel the cause is
  *known* to affect.
- **`+0.5`** if `SymptomEvent.subsystem in Cause.affected_subsystems()`
  but the channel is not in the expected list — the cause might
  explain this through cross-subsystem coupling (e.g., `eps_internal_r_ramp`
  affecting `B-1` battery temperature even though `B-1` is owned by
  Thermal).
- **`+0.25`** "stickiness bonus" per additional event in the same
  window that already scored for this cause — a small bonus that
  prefers causes that explain the *whole* window, not just the
  latest event. This is what lets Diagnose prefer a cause that
  scored 1.0 once and 0.25 twice (total 1.5) over a cause that
  scored 1.0 once on a different channel (total 1.0).
- **0** if neither channel nor subsystem overlaps — the cause is
  ruled out for that event.

**Threshold:** if no cause scores `>= MIN_DIAGNOSE_SCORE` (default
0.5), the diagnosis is `Cause.NO_FAULT_DETECTED` (a sentinel; the
pipeline returns the OK verdict and stops).

**Tie behavior:** when multiple causes score identically (the
common case for thermal causes on a single channel, or EPS causes
on `P-1`), stable sort picks the first by catalog order. The Phase 1
demo is honest about this — the system surfaces "we have 3
plausible thermal causes tied at score 5.0; we picked
`thermal_heater_stuck_off` because it was first in the registry."
A future Diagnose improvement is to add a `kind` field to the
`expected_channels` rubric so a `shift` event scores higher for
`thermal_heater_stuck_on` than for `thermal_heater_stuck_off` (see
§3.3 deferred).

**Independence property:** Diagnose does not know how the symptom
events were generated. It scores the same window the same way
whether the events came from a live LSTM alert or a synthetic
seeding. The bridge translates *how the data was injected* (via
`INJECTION_TO_FAULT`) into a starting state for Validate, but
Diagnose itself only sees the symptom window.

**LLM involvement:** none.

### Stage 3 — Propose

**What it does:** given a `Cause` and the current twin state, pick the
best `Procedure` by twin-validated risk. **Propose does not pick by
catalog rank or `default_procedure_id`.** It runs a two-stage
selection:

1. **Pre-filter** the candidate set returned by
   `get_candidate_procedures(cause)` to `PROPOSE_TOP_K = 5` using
   cheap catalog-level signals (`effort_score`, `mission_impact`,
   `reversibility` — D-16). This is a stable lexicographic sort;
   no twin simulation runs during pre-filter.
2. **Validate in parallel** the top-K candidates in a
   `ThreadPoolExecutor` (one worker per candidate). Each
   `validate_procedure()` call returns a `risk_score`; Propose
   sorts the survivors by `risk_score` ascending and picks the
   lowest.

This is the **validation-based ranking** decision (D-12), now with
the pre-filter and parallel sims that D-12's "Future evolution"
called for. The reasoning: the twin is the source of truth for
"what will happen if I apply procedure X to state S." Letting it
do the ranking means Propose is honest — it picks what the twin
says is best, not what the catalog says should be best. The
pre-filter keeps Validate's cost bounded as the catalog grows
(D-16); the parallel sims keep the wall time bounded as the top-K
grows (D-17).

**Where it lives:** `digital-twin/twin/propose.py` (now ~290 lines
after the D-16/D-17/D-18 changes). Defines:

- `PROPOSE_TOP_K: int = 5` — the maximum number of candidates
  fed to Validate.
- `_coarse_rank_key(proc) -> tuple` — the lexicographic pre-filter
  key: `(effort_score, mission_impact_score, reversibility_score)`.
  Lower is better. Two small dicts map the categorical values to
  floats (`MISSION_IMPACT_SCORE`, `_REVERSIBILITY_SCORE`).
- `RankedCandidate` dataclass — one row in
  `Proposal.candidates_ranked`. Carries `procedure`, `risk_score`,
  `validation`, and the three catalog-level fields
  (`effort_score`, `mission_impact`, `reversibility`).
- `Proposal` dataclass — `cause`, `cause_score`, `procedure`,
  `procedure_params`, `risk_score`, `validation`, `verdict`,
  `candidates_ranked: list[RankedCandidate]`.
- `propose(cause, starting_state, ..., progress_cb=None) ->
  Proposal` — the main entry point.

**What it consumes:** a `Cause` (from Stage 2), a starting twin
state (a `dict` matching the `twin.state.make_default_state()` shape),
optional `horizon_s` / `dt_s` for the validation horizon (defaults:
3600s / 120s = 1 hour at 2-minute steps; the integration test uses
14400s / 60s = 4 hours at 1-minute steps), and an optional
`progress_cb: Callable[[procedure_value, step, total, snapshot],
None]` that fires from worker threads as each parallel sim
advances. The bridge/WS server passes the callback in §2 Bridge.

**What it produces:** a `Proposal` with:

- the winning `procedure` (lowest `risk_score` from the top-K),
- the chosen `procedure_params` (read from
  `PROCEDURE_REGISTRY[procedure].default_params` via
  `get_default_params(procedure)`),
- the full top-K ranking in `candidates_ranked: list[RankedCandidate]`,
  ordered by `risk_score` ascending (so the winner is index 0),
- the `Verdict` from `to_verdict()` on the winner.

The `to_dict()` method produces a JSON-serializable form for the
WebSocket broadcast. The `candidates_ranked` entries become
`{procedure, risk_score, effort_score, mission_impact, reversibility}`
dicts — the frontend's candidates table consumes these directly
without re-reading the registry.

**The selection loop** (`propose.py:propose`):

```python
candidates = get_candidate_procedures(cause)

# 1. Pre-filter to top K by catalog-level signals (cheap, no twin).
scored = [(_coarse_rank_key(p), p) for p in candidates]
scored.sort(key=lambda x: x[0])
top_k = [p for _, p in scored[:PROPOSE_TOP_K]]

# 2. Validate all top-K in parallel in a ThreadPoolExecutor.
#    Each worker calls validate_procedure() and reports per-step
#    progress via progress_cb.
workers = min(PROPOSE_TOP_K, len(top_k))
results: List[RankedCandidate] = []
with ThreadPoolExecutor(max_workers=workers) as pool:
    for rc in pool.map(
        lambda p: _run_one(p, starting_state, horizon_s, dt_s, progress_cb),
        top_k,
    ):
        results.append(rc)

# 3. Sort by risk_score ascending; lowest wins.
results.sort(key=lambda x: x.risk_score)
best = results[0]
best_verdict = to_verdict(best.validation, best.procedure, cause)
```

**Why the pre-filter is catalog-level, not twin-level.** The
pre-filter runs in O(N log N) over `len(candidates)` entries and
touches only the static `ProcedureSpec` fields. It does not call
`validate_procedure()`, so it costs effectively nothing (sub-ms
for the 9-procedure catalog). The runtime `risk_score` from
Validate is the *final* arbiter — the pre-filter only changes
*which* candidates reach Validate, not *how* they are ranked once
there. This is the cheapest place to remove a candidate and the
safest place to do so: a procedure dropped by the pre-filter
never had its twin simulation run, so it can never produce a
lower `risk_score` than a survivor.

**Why parallel sims.** Per-candidate `validate_procedure()` is
~30-50ms at the default 1h/120s horizon. For 2-3 candidates
(this is Phase 1's range), sequential is fine. For the future
catalog (10+ procedures per cause), parallel brings wall time
back to ~one sim's duration instead of N. The structural test
`test_propose_runs_candidates_in_parallel` in
`digital-twin/twin/tests/test_propose.py` asserts the executor
is created exactly once with the correct `max_workers` count.
The Python GIL limits the speedup (numpy releases the GIL but
the per-step bookkeeping in `_run_forward` does not) — observed
wall time on a 3-candidate run is ~1.5-2x the single-candidate
time, not 1x. This is acceptable for Phase 1 and will improve
if the per-step math is moved to a GIL-releasing primitive in
the future.

**Why `RankedCandidate` is a dataclass, not a tuple.** The
original 3-tuple `(Procedure, float, ValidationResult)` lost
information: the catalog-level fields (`effort_score`,
`mission_impact`, `reversibility`) live in the registry but
were not propagated to the proposal, so the frontend had to
re-read the registry to render the candidates table. Promoting
the rank entry to a `RankedCandidate` dataclass (D-18) carries
those fields through. The `to_dict()` output is `{procedure,
risk_score, effort_score, mission_impact, reversibility}` — a
strict superset of the old `{procedure, risk_score}` shape.

**Edge case:** if a cause has no candidate procedures (defensive
guard, shouldn't happen for any of the 13 Cause values), Propose
returns a `WAIT` proposal as the safe default. The pipeline never
blocks. The `WAIT` candidate's `RankedCandidate` is still
constructed with the full `RankedCandidate` shape, so the
frontend's candidates table renders correctly even in this
fallback path.

**Progress callback contract.** The optional `progress_cb` is
invoked from worker threads inside `validate_procedure()`. It
receives `(procedure_value, step, total, snapshot_state)` after
each sim step, in both the baseline and the predicted run (so the
total fires per sim is `2 * n_steps`). Consumers MUST be
thread-safe — the bridge/WS server wraps it in a `queue.Queue`
on `AppState.sim_progress_queue` and the producer loop drains
that queue once per tick. The callback may be `None` (legacy
callers like the offline demos don't pass one).

**LLM involvement:** none. Propose is pure-Python + the twin
subroutines. The LLM is a Phase 4 future narrator; it does not
participate in procedure ranking.

### Stage 4 — Validate

**What it does:** project the twin forward from a starting state
with a proposed procedure applied, return the predicted state at
each timestep **plus a no-action baseline** for direct comparison,
and surface constraint violations, a risk score, and a postcondition
check.

**Where it lives:**
- `digital-twin/twin/validate.py` (306 lines) — the actual
  simulation. Defines the `ValidationResult` dataclass
  (`feasible`, `violations`, `risk_score`, `predicted_trajectory`,
  `baseline_trajectory`, `summary`, `postcondition_check`) and the
  `validate_procedure(starting_state, procedure, params, ...)` function.
- `digital-twin/twin/verdict.py` (136 lines) — the BIBLE-shaped
  wrapper. Defines the `Verdict` dataclass (`proposal_id`, `status`,
  `reason`, `per_step_outcomes`, `twin_simulation_digest`, `notes`),
  the `VerdictStatus` enum (`OK | REJECT | INCONCLUSIVE`), the
  `RISK_THRESHOLD_INCONCLUSIVE = 0.3` constant, and the
  `to_verdict(validation, procedure, cause) -> Verdict` function.

**What it consumes:** a `Procedure` (from Stage 3), validated
parameters (the `ProcedureSpec.validate_params(params)` call at the
top of `validate_procedure` raises `ValueError` on bad params; the
defaults always validate by construction), and the same
`starting_state` Stage 3 used. Also accepts an optional
`on_step: Callable[[int, int, float, dict], None]` callback that
fires from the worker thread after each sim step in BOTH the
baseline and the predicted run.

**What it produces:** `ValidationResult` with both
`predicted_trajectory` (the procedure applied) and
`baseline_trajectory` (no action) as `dict[str, np.ndarray]` keyed
by state field (`battery_soc`, `battery_voltage_v`, `battery_temp_c`,
`payload_temp_c`, `electronics_temp_c`, `radiator_temp_c`,
`pointing_error_deg`, etc.). Each array has `horizon_s / dt_s + 1`
entries. The `summary` field is a one-line human-readable roll-up;
the integration test asserts the `predicted` and `baseline` arrays
have the same shape so consumers can plot them side-by-side.

**The two-trajectory model** (`validate.py:_run_forward`):

The function runs the twin forward twice — once with
`apply_procs=[]` (the baseline) and once with the procedure applied
for its full duration. The twin's `step_eps`, `step_thermal`, and
`step_adcs` are called at every `dt_s` step; the procedure's
`apply_procedure(state, procedure, params)` is called once at
`start_t` and the state is restored to a pre-procedure snapshot at
`end_t`. This is what makes the trajectory table in
`phase1_demo_deep.py` and `slight_shift_demo.py` show the
*delta* — the columns are always
`pred <field>` vs `base <field>`.

**The per-step progress callback** (`on_step`, added in
D-17). Both `_run_forward` and `validate_procedure` accept an
optional `on_step: Callable[[int, int, float, dict], None]`
argument. The callback fires after each physics step in BOTH the
baseline and the predicted run, with the signature
`(step_index, total_steps, t_s, current_state)`. The callback
**does not fire on the final +1 step** (the post-horizon state is
conceptually outside the sim) — total fires per `validate_procedure`
call is `2 * n_steps` where `n_steps = horizon_s / dt_s`.

The callback is invoked from whichever thread called
`validate_procedure()`. When Validate is called from Propose's
`ThreadPoolExecutor` (D-17), the callback fires from the worker
thread; consumers MUST be thread-safe. The bridge/WS server wraps
it in a `queue.Queue` and the producer loop drains that queue
once per tick (see "Bridge" below and §3.2's
`sim_progress` field in the WebSocket contract). When Validate is
called directly (offline demos, the unit tests), the callback
fires from the caller's thread and no thread-safety wrapping is
required.

The callback's intended consumer is the WebSocket `sim_progress`
field (D-17): one event per completed sim step, so the frontend
can render "this procedure is at step N/M" indicators as the
parallel sims run. See the Bridge section for the full
producer/consumer flow.

**Constraint checking** (`validate.py:_check_violations`): six
fields are checked per timestep:
- `battery_soc`: `[0.10, 1.00]` (never below 10%)
- `battery_voltage_v`: `[24.0, 32.0]` (24V is LVC for 28V bus)
- `battery_temp_c`: `[-20.0, 60.0]` (Li-ion survival)
- `electronics_temp_c`: `[-20.0, 70.0]`
- `payload_temp_c`: `[-20.0, 60.0]`
- `pointing_error_deg`: `[0.0, 10.0]` (use absolute value; 10°
  is unacceptable)

A trajectory that hits any of these (one violation per field is
enough) is `feasible=False`.

**Risk score** (`validate.py:_compute_risk`): 0 to 1, lower is
safer. Computed as `0.5 * violation_rate + 0.5 * min(1.0, soc_diff * 2.0)`
where `soc_diff` is the SoC drop relative to the baseline (a
procedure that doesn't help the battery is risky even if it doesn't
violate a hard constraint).

**Verdict mapping** (`verdict.py:to_verdict`): the wrapper maps
`ValidationResult` to `Verdict` with three branches:

1. **`REJECT`** if `!feasible` or there are constraint violations —
   the twin predicted the procedure cannot complete safely. The
   `reason` names the first 3 violation fields.
2. **`INCONCLUSIVE`** if `feasible` but `risk_score >= 0.3` — the
   twin says it works, but the margin is thin; operator review is
   warranted. The `reason` names the procedure, cause, risk score,
   and threshold.
3. **`OK`** otherwise.

The threshold `0.3` is a demo default; it's tunable. A future
refactor moves it into the `LiveConfig` (or, in Phase 2, into the
Rego policy bundle).

**LLM involvement:** none. Validate is the deterministic twin
simulation; the LLM is a future narrator that writes prose around
the structured `Verdict` (Phase 4).

### Bridge — `live/twin_bridge.py`

The bridge is the **single** module in `live/` that imports from
`twin/`. It exists so that `live/ws_server.py` can boot on system
Python without the CHESS astropy chain, so the live test suite can
run in any environment, and so the integration test exercises only
this module's surface.

**Where it lives:** `live/twin_bridge.py` (now ~360 lines after
D-17). The bridge exposes two public functions and two public
tables:

- `inject_fault(scheduler, kind, magnitude, channel) -> dict` —
  translates an HTTP injection into a twin `FaultScheduler.inject()`
  call. Returns a JSON-serializable dict with `fault_id`,
  `fault_type`, and the scaled `parameters`.
- `run_phase1_pipeline(fault_type, fault_params, alerts, channel_hint=None, horizon_s=3600.0, dt_s=120.0, progress_cb=None) -> Optional[dict]`
  — runs Diagnose → Propose → Verdict for the current injection.
  Returns `None` if the twin can't be imported (CHESS venv missing
  on system Python); the live server keeps running, the operator
  just doesn't see a verdict this tick. The optional
  `progress_cb: Callable[[procedure_value, step, total, snapshot],
  None]` is forwarded to `propose()` and fires from the worker
  threads running the parallel twin sims (D-17); the WS server
  uses it to populate the `sim_progress` field of the WebSocket
  tick message. The bridge does NOT add thread-safety to the
  callback — the caller (the WS server) is responsible for
  forwarding events to the asyncio event loop.
- `INJECTION_TO_FAULT` — a 13-entry mapping from `(kind, channel)`
  to `(twin fault_type, default params)`. The legacy 3 entries with
  `channel=None` keep backward compat with the original `/inject`
  endpoint; the 10 channel-specific entries are the demo helpers.
- `CHANNEL_TO_SUBSYSTEM` — the 8-channel → 4-subsystem map
  (`P-1`/`P-2` → `eps`, `B-1`/`T-1`/`T-2` → `thermal`, `A-1`/`G-1`
  → `adcs`, `D-1` → `comms`).

**`progress_cb` contract** (D-17). The callback's signature is
`(procedure_value: str, step: int, total: int, snapshot: dict)`.
- `procedure_value` is the `Procedure.value` string (e.g.,
  `"eps_shed_non_essential_load"`), so consumers can attribute
  the event to one of the parallel sims without bookkeeping.
- `step` is the 0-indexed step in the current sim run;
  `total` is the total step count for that run
  (`horizon_s / dt_s`).
- `snapshot` is the post-step twin state dict; the WS server
  reads `t_s`, `battery_soc`, `battery_temp_c`, and
  `payload_temp_c` from it to populate the broadcast event.

The callback fires from worker threads. It may be called
`2 * (horizon_s / dt_s) * len(top_k_candidates)` times per
pipeline run (twice per sim step, once for the baseline run and
once for the predicted run, for each top-K candidate). For the
default 1h/120s horizon with 3 candidates, that's 60 fires per
pipeline run — well within a `queue.Queue(maxsize=2000)` budget.

**Lazy-import pattern** (`twin_bridge.py:_ensure_twin_on_path`):
the `_ensure_twin_on_path()` call lives at the top of every
twin-using function, not at module load. The bridge module itself
imports only stdlib + `os` + `logging` + `sys`; the `twin.*`
imports happen inside `run_phase1_pipeline()` after the path is
set. This is what lets the live server boot on system Python
without pulling in the CHESS simulation. If the CHESS venv is
missing, the `from twin.diagnose import ...` line raises; the
bridge logs a warning and returns `None`; the live server keeps
running; the `/inject_twin_fault` endpoint returns 503
(`live/ws_server.py:167-173`).

**The bridge is the seam the BIBLE §14 (now §14 "Landed") calls
for.** The reason it's the *only* importer in `live/` is D-14:
keeping the cross-boundary knowledge in one file makes it
auditable, testable in isolation, and replaceable (to a gRPC twin
in Phase 2) without touching the rest of `live/`.

### Stage 5 — Approve *(deferred to Phase 2)*

**What it does:** given a `Proposal` and a `Verdict`, route the
action through the operator approval flow that matches its
`risk_class`. Issue an `ApprovalToken` if and only if the policy
says so.

**Where it will live:** `mission_ops/stages/approve.py` (Phase 2).

**LLM involvement:** none. **The LLM has no path to the approval
gate.** This is enforced structurally (the LLM's tool surface
does not include the gate), not by prompt. See
[§7 Trust and approval](#7-trust-and-approval).

### Stage 6 — Execute *(deferred to Phase 2)*

**What it does:** given an `ApprovalToken` and a `Procedure`, walk
the procedure's steps through a `CommandBus`, recording per-step
input/output hashes and operator signatures.

**Where it will live:** `mission_ops/stages/execute.py` (Phase 2).

**LLM involvement:** none.

### Stage 7 — Verify *(deferred to Phase 3)*

**What it does:** build a Merkle-chained runbook from the per-step
receipts, content-address every evidence blob (telemetry, twin
state, twin simulation trace, model weights digest, Rego policy
bundle), and register the runbook with the replay harness.

**Where it will live:** `mission_ops/stages/verify.py` (Phase 3).

**LLM involvement:** none.

### Orchestration

The live server's `live/ws_server.py` is the Phase 1 orchestrator
(it plays the role the supervisor was designed for). The producer
loop in `_step_once()` runs Detect every tick; the
`/inject_twin_fault` endpoint runs the Diagnose → Propose →
Validate sequence synchronously when the operator injects. The
verdict is stashed on `app.state.live.latest_verdict` and attached
to the next WebSocket broadcast that has an anomaly. The pipeline
is *predictive* — it runs at injection time on the fault's
expected behavior, not on the (not-yet-detected) LSTM alerts
(BIBLE §2 contract; the bridge seeds a synthetic `SymptomEvent`
with `score=1.0` when `alerts=[]` and `channel_hint` is set, so
Diagnose has something to score).

All four stages are pure functions
`(state_in) -> (state_out, side_effects)` from the perspective of
the caller. The Detect stage has a side effect (it logs to
`alerts.jsonl`), but the side effect is append-only and does not
affect the function's return value. This makes replay trivial and
audit natural.

---

## 3. Current implementation state

This section is the project's *journal* for "what is runnable and
what is in flight." It's organized as three sub-sections: what
landed (Phase 1 is shipping), what we're building next (the
frontend), and what's left for the future (Phase 2/3/4 and the
Phase 1 polish items). The owner updates this section as the work
progresses; the BIBLE is the source of truth for "what's
implemented and what isn't."

### 3.1 — What we finished

Phase 1 is shipping. All four stages of the pipeline (Detect →
Diagnose → Propose → Validate) are runnable end-to-end, the test
suite is green, the demos work, and the `feature/digital-twin`
branch has been integrated.

#### Stage 1 — Detect (`live/`)

The Detect stage is the LSTM streaming detector. It lives in
`live/` (5 modules, ~1,000 lines total) and is the surface the
operator interacts with. Everything in `live/` is shipping:

- **FastAPI server** (`live/ws_server.py`, 362 lines): serves
  `GET /` (dashboard HTML), `WS /stream` (one JSON message per
  tick), `POST /inject` (queue a `spike | shift | dropout` anomaly),
  and `POST /inject_twin_fault` (the Phase 1 pipeline endpoint —
  runs Diagnose → Propose → Validate, returns the verdict).
- **LSTM runner** (`live/model_runner.py`, 81 lines): wraps
  `keras.models.load_model` with a `predict(window) -> np.ndarray`
  interface. The default model is the 2-layer LSTM(80) trained on
  SMAP/MSL (Hundman et al., 2018).
- **Streaming error pipeline** (`live/error_stream.py`, 445 lines):
  the port of telemanom's batch `Errors` class to a per-tick
  streaming API. Maintains an EWMA-smoothed error buffer and runs
  `find_epsilon` → `compare_to_epsilon` → `prune_anoms` →
  `score_anomalies` on the trailing window. The math is
  identical to telemanom; the I/O is per-tick instead of batch.
- **Synthetic generator** (`live/generator.py`, 125 lines): a
  sinusoid + Gaussian noise generator with a thread-safe injection
  queue. `shift` injections auto-expire after 30 ticks so the demo
  shows both the onset and the recovery.
- **Config** (`live/config.py`, 95 lines): the `LiveConfig`
  dataclass. Defaults: `TICK_HZ = 5.0`, `n_predictions = 10`,
  `window_size = 30`, `smoothing_perc = 0.15`, `l_s = 250`. The
  integration test tunes these for fast warm-up.

**Test surface:** 5 test files, 13 tests in `live/tests/`
plus the 4 new integration tests in
`live/tests/test_injection_bridge.py`. Two of the pre-existing
tests (`test_load_predict`, `test_inject_shift_triggers_anomaly_over_websocket`)
require the real keras model and fail on system Python without
the CHESS venv; they pass when the venv is active and are the
regression gate for when the production LSTM model is loaded.

**Keras stub pattern:** `live/tests/test_injection_bridge.py`
installs a minimal keras stand-in in `sys.modules` at import
time, then monkey-patches `live.model_runner.LSTMRunner` with a
`MockLSTMRunner` that always returns the last window value as
the prediction. The same pattern is used in
`digital-twin/examples/phase1_demo.py`. This is what lets the
live test suite and the live demo run on system Python without
the CHESS venv — the suite stays runnable in any environment,
and the production model is loaded automatically when the venv
is present.

#### Stage 2 — Diagnose (`digital-twin/twin/diagnose.py`)

The Diagnose stage is a pure function of a `SymptomEvent` window.
It lives in `digital-twin/twin/diagnose.py` (152 lines) and
defines:

- `SymptomEvent` (frozen dataclass: `channel`, `subsystem`,
  `kind`, `score`, `seq`, `ts`)
- `CandidateCause` (`cause`, `score`, `matched_events`,
  `evidence_subsystems`)
- `MIN_DIAGNOSE_SCORE = 0.5` (the threshold below which the
  diagnosis is `NO_FAULT_DETECTED`)
- `diagnose(window: list[SymptomEvent]) -> list[CandidateCause]`

The scoring rubric is `+1.0` per channel-overlap event,
`+0.5` per subsystem-overlap event, `+0.25` stickiness bonus
per additional matching event. See §2 Stage 2 for the rubric
and the independence property (Diagnose does not know how the
events were generated).

**Test surface:** `digital-twin/twin/tests/test_diagnose.py`
plus the tests in `test_propose.py` that exercise the Diagnose
edge. Diagnose is a small surface — the tests focus on the
threshold, the tie behavior, the `NO_FAULT_DETECTED` path, and
the stickiness math.

#### Stage 3 — Propose (`digital-twin/twin/propose.py`)

Propose is the **validation-based ranking** layer (D-12). It
calls `validate_procedure()` for every candidate procedure
returned by `get_candidate_procedures(cause)`, ranks them by
**multi-axis lexicographic key** (D-19), and returns the
lowest-key candidate as the winning `Proposal`.

**Multi-axis ranking (D-19).** The winner is the row with the
lowest tuple on `(mission_impact_score, effort_score,
reversibility_score, risk_score)`, ascending. Risk is the
final tie-breaker, not the primary axis: when two procedures
are tied on mission_impact / effort / reversibility, the
lower `risk_score` wins; otherwise the catalog fields decide
and the runtime risk_score is never consulted. The reasoning
is D-12's "future evolution" — the catalog fields the operator
sees in the runbook are the *same logic* that picked the
winner, not just descriptive labels. The pre-filter
(`_coarse_rank_key`, also lexicographic on the catalog fields
but without runtime `risk_score`) keeps the candidate set to
`PROPOSE_TOP_K = 5` before the expensive twin sims run.

The `Proposal` dataclass (`cause`, `cause_score`, `procedure`,
`procedure_params`, `risk_score`, `validation`, `verdict`,
`candidates_ranked`, **`winner_rank_key`** — the
4-tuple that picked the winner) carries enough for the
operator's view, the runbook, and Phase 3. The `to_dict()`
method produces the JSON-serializable form the WebSocket
broadcast carries, including `winner_rank_key` as a list of
floats.

**Test surface:** `digital-twin/twin/tests/test_propose.py`
(19 tests; covering the ranking math, the top-K pre-filter,
the `RankedCandidate` shape, the default-params fall-back,
the empty-candidate edge case, the `progress_cb` callback
contract, the structural executor test that asserts the
top-K candidates run in a `ThreadPoolExecutor` with the
correct `max_workers`, plus 4 multi-axis tests that pin the
D-19 lexicographic key, the `winner_rank_key` propagation,
the risk-score tie-breaker behavior, and the `to_dict()`
ordering) and
`test_procedures_defaults.py` (12 tests; covering the
`PROCEDURE_REGISTRY.default_params`, the
`get_default_params(procedure)` accessor, and the
import-time validation of the D-16 catalog-level editorial
fields — `effort_score` in `[0.0, 1.0]`, `mission_impact`
in the allowed set, `reversibility` in the allowed set, plus
a spot-check of the `WAIT` / `MODE_CHANGE_TO_SAFE` extremes).

#### Stage 4 — Validate (`digital-twin/twin/validate.py` + `verdict.py`)

Validate is the **two-trajectory** simulator. It runs the twin
forward twice from the same starting state — once with the
procedure applied, once without — and returns both trajectories
side-by-side. The Verdict wrapper maps the `ValidationResult`
to a BIBLE-shaped `Verdict {proposal_id, status, reason,
per_step_outcomes, twin_simulation_digest, notes}` with status
`OK | REJECT | INCONCLUSIVE`.

The `to_verdict()` decision order is: `REJECT` if the trajectory
violated any of six constraints (SoC, voltage, battery temp,
electronics temp, payload temp, pointing error), then
`INCONCLUSIVE` if `risk_score >= 0.3`, then `OK`. See §2 Stage 4
for the constraint table and the risk-score formula.

**Test surface:** `digital-twin/twin/tests/test_verdict.py`
(covering the three verdict branches and the threshold
behavior). Validate itself is tested via the
`phase1_demo_deep.py` and `slight_shift_demo.py` scripts,
which print the actual trajectory tables and assert the
predictions are within tolerance.

#### The twin core (`digital-twin/twin/`)

The digital twin is a lumped, deterministic state machine that
implements the 5 subsystems (EPS, Battery, Thermal, ADCS, Comms)
across 8 channels. **~3,012 lines total** (was 2,791 before the
D-16/D-17/D-18 additions; the new fields, the parallel sims, and
the `RankedCandidate` shape added ~220 lines), organized as:

| File | Lines | Role |
|---|---|---|
| `state.py` | 199 | `make_default_state()` factory + the state-dict shape |
| `eps.py` | 196 | `step_eps(state, dt_s, ...)` — terminal voltage, SoC bookkeeping, temperature-coupled internal resistance |
| `thermal.py` | 204 | `step_thermal(state, dt_s, ...)` — 4-node RC network (battery, payload, electronics, radiator) with PASEOS-derived solar + albedo + Earth-IR + Stefan-Boltzmann emission |
| `adcs.py` | 134 | `step_adcs(state, dt_s, ...)` — 2nd-order damped pointing + reaction wheel dynamics with saturation |
| `fault_injection.py` | 265 | `FaultScheduler` — the 9 fault types and the per-step `apply()` callback |
| `channel_shaper.py` | 102 | Telemanom-shaped `.npy` per channel (scaled to (-1, 1) per column) |
| `procedures.py` | 785 | **The single source of truth** for `Cause`, `Procedure`, `ProcedureSpec`, `PROCEDURE_REGISTRY`, `apply_procedure()`, `get_candidate_procedures()` (D-10). +73 lines for the D-16 catalog-level editorial fields and their import-time validation |
| `validate.py` | 326 | `validate_procedure()` + `ValidationResult` + the two-trajectory forward loop. +20 lines for the `on_step` callback parameter (D-17) |
| `propose.py` | 282 | `propose()` + `Proposal` + `RankedCandidate` + the top-K pre-filter + parallel sims (D-16/D-17/D-18). +128 lines |
| `diagnose.py` | 152 | `diagnose()` + `SymptomEvent` + `CandidateCause` |
| `verdict.py` | 136 | `to_verdict()` + `Verdict` + `VerdictStatus` + the `0.3` threshold |
| `run_sim.py` | 231 | The CLI entry point: `python -m twin.run_sim` runs the full 4-hour twin sim offline |

**The catalog is one file** (D-10):
`digital-twin/twin/procedures.py` defines the `Cause` enum (13
values), the `Procedure` enum (9 values), the `ProcedureSpec`
dataclass (param bounds, preconditions, postconditions,
`apply_fn`, `risk_class`, `approval_required`), and the
`PROCEDURE_REGISTRY: dict[Procedure, ProcedureSpec]`. There is
no separate YAML or JSON file; the catalog and the twin are
the same Python module. The drift-prevention rationale is in
D-10.

**Determinism:** the twin is fully deterministic. Same
`starting_state` + same `procedure` + same `params` →
byte-identical `ValidationResult`. This is the invariant
that makes the Phase 3 Merkle chain work. There is no
wall-clock or random input in the step functions; the only
randomness in the system is in the live `generator.py`
(which is for demo data, not for the twin).

#### The bridge (`live/twin_bridge.py`)

`live/twin_bridge.py` (~470 lines) is the **single seam** between
`live/` and `twin/` (D-14). It exposes two public functions
(`inject_fault()` and `run_phase1_pipeline()`) and two public
tables (`INJECTION_TO_FAULT` with 13 entries,
`CHANNEL_TO_SUBSYSTEM` with 8 entries). The lazy-import pattern
(`_ensure_twin_on_path()` at the top of every twin-using
function) keeps `live/ws_server.py` bootable on system Python
without the CHESS astropy chain. If the CHESS venv is missing,
`run_phase1_pipeline()` returns `None`; the live server keeps
running; `/inject_twin_fault` returns 503. See §2 Bridge for
the full surface.

**D-19 return shape.** `run_phase1_pipeline()` now returns
`{"proposal": <dict>, "runbook": <dict or None>}`. The
`proposal` block is the same JSON-serializable dict
`proposal.to_dict()` produced before D-19 — the WS alert tick
and the `/inject_twin_fault` response consume this directly.
The `runbook` block is the new enriched view (see the
"Runbook API (D-19)" section below). The bridge captures
pipeline timestamps at each stage (`stage_1_detect`,
`stage_2_diagnose`, `stage_3_propose`, `stage_4_validate`)
and stamps them on the runbook's `pipeline` block so the
operator can see when each stage ran. If the builder fails
(e.g. numpy scope issue), the `runbook` block is `None` and
the rest of the pipeline keeps working.

#### Runbook API (D-19)

The runbook is the operator-facing artifact the dashboard's
`/runbook` route renders. It is built once at inject time
(per the BIBLE §2 predictive-pipeline contract) and persists
across page refreshes via the in-memory `RunbookStore`. The
frontend (Milestone 2) wires its runbook page to the three
endpoints below; the API surface is what landed in D-19.

**`live/runbook_store.py` (~135 lines).** Thread-safe FIFO
with id minting. Every `add(runbook)` returns a fresh
`runbook_id` of the form `RB-YYYYMMDD-NNNN` (per-day counter
resets at midnight UTC). The store caps at 50 entries
(older ones evicted FIFO). Reads (`get(id)`,
`list_recent(limit=20)`) are O(n) on the dict and take the
same lock briefly. The cap is small because runbooks carry
twin trajectory arrays (31 points × 3 fields × 2 series ≈
2 KB each); Phase 2 moves the cap to the durable DB layer.

**`live/runbook_builder.py` (~595 lines).** The enrichment
layer. Given a `proposal_dict` + a `candidates_considered`
list (every procedure the cause maps to, each with its
`ProcedureSpec` attached under `_spec`) + the symptom
window + the predicted/baseline trajectories from the
winner's `ValidationResult`, builds the full runbook
payload. Joins:
- Cause catalog: `affected_subsystems()`, `expected_channels()`
- Procedure catalog: `description`, `risk_class`,
  `approval_required`, plus the editorial `effort_score` /
  `mission_impact` / `reversibility` already on the
  `RankedCandidate`
- The multi-axis rank key on every candidate, the winner's
  per-axis comparison list, and a human-readable "why this
  won" reason
- The 3 trajectory fields the runbook canvas renders:
  `battery_soc`, `battery_temp_c`, `payload_temp_c`, each
  with `baseline[]` and `predicted[]` arrays of length
  `horizon_s / dt_s + 1` (31 points for the default
  1h/120s horizon)
- A `candidates_considered_detail` list that preserves
  every procedure the cause mapped to, with `simulated:
  true|false` and a `drop_reason` (`"coarse_rank above
  top-K"`) for the ones the pre-filter dropped
- Pipeline timestamps (`pipeline.stage_1..stage_4.at_unix`)
  for the operator's "when this happened" view
- An `approval` block (status `PENDING`, the required role
  from the winner's `approval_required`); the operator's
  Approve/Reject click fills this in (Phase 2)

**`live/ws_server.py` additions.** The `AppState` holds a
`RunbookStore` and a `latest_runbook_id` (the id of the most
recent runbook minted). Three new endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/runbooks?limit=N` | List recent runbooks (most recent first); each entry is the **summary view** (id, fault_id, cause, subsystem, winner, verdict_status, generated_at) |
| `GET` | `/api/runbooks/{runbook_id}` | Full payload (all 9 sections + trajectory arrays + approval block) |
| `GET` | `/api/runbooks/{runbook_id}/trajectory` | Just the 3-field trajectory (lightweight for the chart components to poll) |

`/inject_twin_fault` now mints a `runbook_id` and stamps it
on the response (alongside the existing `fault_id` /
`verdict`). The `latest_runbook_id` is also attached to
**every WebSocket tick** so the dashboard can link to
`/runbook` without polling. Unknown ids return 404.

**Test surface:**
- `test/test_71_runbook_store.py` (6 tests: id minting, cap,
  thread safety, list limit, unknown-id 404, type validation)
- `test/test_70_runbook_builder.py` (12 tests: cause catalog
  fields, procedure catalog fields, candidates_considered
  preservation, simulated-marking, winner-factors explanation,
  trajectory shape, pipeline-timestamp monotonicity, verdict
  passthrough, JSON-serializability, ranking-algorithm string,
  single-candidate fallback, summary/trajectory views)
- `test/test_72_runbook_endpoints.py` (8 tests: list-after-
  inject, get-by-id, unknown-404, trajectory-endpoint shape,
  WS carries runbook_id, ranking-algorithm in payload, list
  limit, id format/uniqueness)

#### Test gate

**49 tests pass on system Python in the relevant scope (twin
+ bridge + legacy propose); the 2 keras-gated live tests
fail pre-existingly without the CHESS venv and pass when
the venv is active.** The inventory:

- **Twin tests:** 42 in `digital-twin/twin/tests/`
  (test_diagnose, test_propose, test_verdict,
  test_procedures_defaults) — all green. The 6 new
  test_propose.py tests (test_propose_filters_to_top_k,
  test_propose_catalog_fields_in_to_dict,
  test_propose_progress_callback_fires,
  test_propose_progress_callback_distinguishes_procedures,
  test_propose_no_progress_callback_works,
  test_propose_runs_candidates_in_parallel) cover
  D-16/D-17/D-18. The 4 new test_procedures_defaults.py
  tests (effort_score in unit range, valid
  mission_impact, valid reversibility, wait-is-lowest /
  mode_change_to_safe-is-highest spot checks) cover
  D-16.
- **Live tests, pre-existing:** 13 in `live/tests/`
  (test_error_stream, test_generator, test_model_runner,
  test_integration). 11 pass; 2 fail (`test_load_predict`,
  `test_inject_shift_triggers_anomaly_over_websocket`) because
  they need the real keras model. The failures are *pre-existing*
  and *expected* on system Python — they pass when the CHESS venv
  is active.
- **Live tests, new integration:** 4 in
  `live/tests/test_injection_bridge.py` —
  `test_phase1_inject_twin_fault_returns_bible_verdict` (the
  Phase 1 success gate), plus `test_inject_twin_fault_returns_503_without_twin_scheduler`,
  `test_inject_twin_fault_rejects_bad_channel`, and
  `test_inject_twin_fault_passthrough_fault_type`. All 4 green.

The 2 keras-gated failures are not regressions from the digital-
twin work; they're the test suite's way of saying "the real LSTM
model isn't loaded." The fix is to set up the CHESS venv in CI
(not in scope for Phase 1). The new tests for the
D-16/D-17/D-18 changes (catalog fields, parallel sims, progress
callbacks, RankedCandidate shape) are in `twin/tests/test_propose.py`
and `twin/tests/test_procedures_defaults.py`; all pass on
system Python.

**Legacy tests:** 3 in `test/test_30_propose.py` (T13, T14, T15)
also pass. The T14 test was extended in the same commit to
assert the new `effort_score` / `mission_impact` / `reversibility`
keys appear in every `candidates_ranked` entry, guarding the
WS contract change for any consumer of the broadcast.

#### Demo surface

**4 runnable demos** in `digital-twin/examples/`, all of which
work today:

- `smoke_test.py` (198 lines) — the pre-existing regression
  test. Exercises the `FaultScheduler` end-to-end with the
  CHESS simulation. Not run on system Python (it needs the
  venv); kept for the regression suite.
- `phase1_demo.py` (360 lines) — the **full live loop**.
  Boots the FastAPI server in-process, POSTs 6 scenarios over
  HTTP, reads from the WebSocket, prints a roll-up table.
  Runs on system Python with the keras stub and the
  `MockLSTMRunner`. This is the demo for "show me everything
  connected end-to-end."
- `phase1_demo_deep.py` (196 lines) — the **deep-dive
  detail**. Runs the pipeline for 2 scenarios (P-1 and T-1)
  without the HTTP layer, prints the per-cause Diagnose
  scoring, the Propose ranking with risk scores, and the
  Validate trajectory with 7 columns (`pred/base` SoC, V,
  T_bat). Runs on system Python.
- `slight_shift_demo.py` (189 lines) — the **single-scenario
  story**. 0.2-unit shift on T-1 → `thermal_heater_stuck_on` →
  Diagnose scores 5 thermal causes tied → Propose ranks 2
  procedures → Validate projects 1 hour with 9 columns
  (adds `T_pay`). All 4 stages labeled in one screen. Runs
  on system Python.

The "show the judges" demo is `slight_shift_demo.py` if the
question is "tell me the story of one anomaly" and
`phase1_demo_deep.py` if the question is "is the twin
*really* simulating?" `phase1_demo.py` is the demo for
"show me the live loop with the WebSocket broadcast."

#### Branch state

The `feature/digital-twin` work has been integrated. The
BIBLE update documented in this section is part of the
post-merge commit; the previous BIBLE §14 (the "Incoming:
the digital twin branch" section) is rewritten as a
"Landed" changelog in the same commit. The pre-integration
`glowing-painting-possum.md` plan files are kept for
historical context (see the end of this section).

**D-19 runbook API** (commit `87a4b0b`, branch
`frontend/intitial_dashboard`) — the second post-merge
landing. Adds multi-axis lexicographic ranking in
`twin/propose.py`, the thread-safe `RunbookStore`, the
`runbook_builder` enrichment layer, three new HTTP
endpoints (`/api/runbooks`, `/api/runbooks/{id}`,
`/api/runbooks/{id}/trajectory`), and a `runbook_id` field
on every WebSocket tick. 30 new tests (4 multi-axis propose
+ 6 store + 12 builder + 8 endpoint). All 116 tests green
(70 contract + 46 twin).

**Live: ensure twin on sys.path before FaultScheduler
import** (commit `ed33c34`) — the fix for the
`No module named 'twin'` import error on
`uvicorn live.ws_server:app` boot. Adds
`_ensure_twin_on_path()` to `ws_server.init_twin()` so the
CHESS venv isn't required for the live server to come up.

The frontend (Vite + React + R3F) work has not yet started;
`live/static/app/` is the placeholder shell from the
pre-frontend era. The 3-route SPA, 3D satellite, subsystem
cards, and runbook page wire to the data shapes documented
in §3.2 below; the API surface D-19 added is what they'll
consume.

### 3.2 — What we're building now

> **Strict instructions for the implementing agent.** This
> section is dense, endpoint-specific, and built to be read
> by a fresh agent without prior context. Read every line
> before starting work.

The terminal demos (`slight_shift_demo`, `phase1_demo_deep`,
`phase1_demo`) answer the *deep-detail* questions a judge or
reviewer asks after an injection ("show me the per-cause
scoring," "show me the trajectory table"). The frontend is
the surface for the operator's view *during* a live
injection. The two surfaces answer different questions.

The prior 3-phase incremental plan (Phase 1 = live data,
Phase 2 = verdict panel, Phase 3 = trajectory) has been
collapsed into a single coordinated implementation because
the backend extensions needed by Phase 2/3 are small enough
to land in one commit up front, and the frontend is
naturally additive (each commit leaves the app runnable).
The user has broken the delivery into **3 reviewable
milestones** (3 git pushes), with the agent's work gated
between each.

#### What this is

A **3-route React app** (intro / dashboard / runbook) served
by the existing FastAPI server on port 8000, backed by
Zustand for state, with a **3D rotating satellite model** at
the center of the dashboard. Black-space background with a
star field and a planet/Earth limb behind it. Lines from
each subsystem body on the satellite to its corresponding
**subsystem card** — the cards are positioned around the
satellite so each card sits next to its subsystem body
(they are NOT stacked vertically on the right; the
operator sees the spatial relationship between the
satellite and the data per subsystem).

#### Tech stack (fixed)

- **Vite + React + TypeScript** for the build/dev server.
- **React Router** for the 3 routes.
- **Zustand** for state (4 module-level singleton stores).
- **react-three-fiber + drei** for the 3D satellite model.
- **No CSS framework.** Hand-written CSS in
  `src/styles/globals.css` + per-component CSS modules.
  Tokens (colors, spacing) as CSS custom properties.
- **FastAPI serves the built Vite output** from
  `live/static/app/` at `/`. The current
  `live/static/index.html` is replaced by the Vite shell.

#### Build milestones (3 reviewable git pushes)

**Milestone 1 — React router, intro page, 3D satellite, websocket, live data.**

Scope: `frontend/` skeleton, all 3 routes wired, intro page
(static), dashboard with rotating 3D satellite on
black-space background, websocket connection live,
`/stream` tick data flowing into `telemetryStore`,
`ChannelRow` showing `t | value | pred | ewma_error |
threshold` per channel, subsystem cards connected to the
satellite via lines, terminal log streaming tick lines.

Acceptance: `npm run dev` works, `npm run build` outputs
to `live/static/app/`, FastAPI serves the Vite shell at
`/`, navigating to `/dashboard` opens the WebSocket, the
3D satellite rotates, tick lines scroll in the terminal,
each of the 5 subsystem cards shows its channels' live
values and EWMA error color. No runbook page content yet —
it can render a "Runbook page TBD" placeholder.

**Git push:** user reviews, then push. No verdict
plumbing, no inject modal, no runbook sections.

**Milestone 2 — Backend verification, inject modal, runbook (9 sections + trajectory).**

Scope: verify each backend stage (Detect, Diagnose, Propose,
Validate) with unit tests, ensure data and information
flowing through is accurate end-to-end. Wire the inject
modal (`POST /inject_twin_fault`), wire the
`m.twin_verdict` payload into `verdictStore`, write the log
narrative lines on anomaly and verdict arrival, auto-
redirect to `/runbook` 1 second after "RUNBOOK GENERATED".
Render all 9 runbook sections and the 3-field trajectory
canvas.

Acceptance: the full happy path works: inject a shift on
T-1, the terminal logs the anomaly → diagnose → propose →
validate → runbook sequence, the route navigates to
`/runbook` after 1s, all 9 sections render with accurate
data, the trajectory canvas shows predicted vs baseline
for 3 fields over 1h.

**Git push:** user reviews, then push.

**Milestone 3 — Polish, intro content, inconsistencies, final review.**

Scope: final intro page content (project goals, title,
summary — to be written by the user later, agent leaves
the route scaffolded with placeholder text), fix any
inconsistencies discovered during Milestone 2, mobile-
responsive pass, color token stabilization, accessibility
checks, final review.

**Git push:** final review, then push.

#### Endpoints the frontend will hit

| Method | Path | When | Request body | Response |
|---|---|---|---|---|
| `GET` | `/` | On page load | — | Vite shell (HTML) |
| `WS` | `ws://<host>/stream` | On first `/dashboard` mount | — | Stream of JSON messages at 5 Hz |
| `POST` | `/inject_twin_fault` | When operator clicks "Inject" in the inject modal | `{kind, channel, magnitude}` (JSON) | `{status, fault_type, verdict, runbook_id, ...}` — see D-19 below |
| `GET` | `/api/runbooks?limit=N` | When the runbook list page mounts (or after a new runbook is generated) | — | `Array<RunbookSummary>` (most recent first) |
| `GET` | `/api/runbooks/{runbook_id}` | When the runbook detail page mounts (`/runbook?rb=...`) | — | Full `Runbook` payload (all 9 sections + trajectory arrays) |
| `GET` | `/api/runbooks/{runbook_id}/trajectory` | When the trajectory canvas needs to refresh (polled less often than WS) | — | `{t_s[], battery_soc: {baseline[], predicted[]}, battery_temp_c: {...}, payload_temp_c: {...}, horizon_s, dt_s}` |
| `GET` | `/` (Vite SPA fallback) | On any client-side route (`/dashboard`, `/runbook`) | — | Same Vite shell; React Router handles the route |

The runbook API is what the operator's `/runbook` page
calls to render the BIBLE §3.2 / §8 / §9 runbook. The
endpoints were added in D-19; the shapes are documented
under "The runbook page" below.

#### WebSocket message shape (every tick at 5 Hz)

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
  sim_progress: null | SimProgressEvent;  // null on most ticks; populated while parallel twin sims are running (D-17)
  runbook_id: null | string;  // D-19: null until a /inject_twin_fault has run; the id of the most recent runbook. The dashboard links /runbook?rb=<id> off this.
};
```

The `kind` field (operator's injection kind) is **NEVER**
included in the WebSocket message in Phase 1 — see "The
`kind` rule" below.

**The `sim_progress` field** (D-17). While the Propose stage is
running the top-K candidates through the digital twin in parallel,
each completed sim step in each parallel sim produces a
`SimProgressEvent` that is pushed to `AppState.sim_progress_queue`
on the WS server. The producer loop drains the queue once per
tick and attaches the **most recent** event to the next tick
message (so a 5 Hz broadcast rate doesn't drown in 60+ events
from one injection). When the producer loop runs faster than
the sims, only the last event is visible; when the sims run
faster than the producer, intermediate events are dropped. The
field is `null` on ticks that don't have a pending sim event.

```ts
type SimProgressEvent = {
  procedure: string;          // e.g. "eps_shed_non_essential_load"
  step: number;               // 0-indexed step in the current sim run
  total: number;              // total step count for the run (horizon_s / dt_s)
  t_s: number;                // sim time at this step, in seconds
  battery_soc: number;        // state snapshot
  battery_temp_c: number;
  payload_temp_c: number;
};
```

The frontend's parallel-sims panel renders one line per
distinct `procedure` it has seen in the most recent N progress
events, with a step progress bar (e.g., "eps_shed_load: 12/30
[███████───]"). When the corresponding tick carries a
`twin_verdict` (the pipeline finished), the panel collapses to
the candidates table.

#### `Proposal` shape (full payload, exactly what the runbook renders)

The full BIBLE §2 / §8 / §9 contract. The frontend's
TypeScript `Proposal` type matches this shape 1:1:

```ts
type Proposal = {
  cause: Cause;                                 // enum from digital-twin/twin/procedures.py
  cause_score: number;                          // 0..5, threshold 0.5
  procedure: Procedure;                         // enum — the winner under the multi-axis rank (D-19)
  procedure_params: Record<string, any>;        // the chosen procedure's params
  risk_score: number;                           // 0..1, runtime risk from twin simulation (also the gauge reading)
  verdict: Verdict;                             // OK | REJECT | INCONCLUSIVE
  candidates_ranked: Array<RankedCandidate>;     // top-K, ordered by multi-axis _rank_key (D-19)
  winner_rank_key: [number, number, number, number];  // [mission_impact_score, effort_score, reversibility_score, risk_score] of the winner
};

type RankedCandidate = {
  procedure: Procedure;                         // enum
  risk_score: number;                           // 0..1, runtime
  effort_score: number;                         // 0..1, catalog-level editorial (D-16)
  mission_impact: "none" | "minor" | "major" | "mission-ending";  // catalog-level (D-16)
  reversibility: "trivial" | "easy" | "hard";   // catalog-level (D-16)
};
```

**Multi-axis ranking (D-19).** `candidates_ranked` is ordered
by the lexicographic key `(mission_impact_score,
effort_score, reversibility_score, risk_score)` ascending.
The catalog fields are the primary decision axis; runtime
risk_score is the final tie-breaker, only consulted when two
candidates are tied on impact / effort / reversibility. This
makes the catalog fields the operator sees in the runbook
*the same logic that picked the winner*, not just
descriptive labels. `winner_rank_key` is the 4-tuple that
decided the comparison — the runbook builder uses it to
compute the per-axis comparison list and the human-readable
"why this won" reason (the deciding axis is the first
position on which the two candidates differ).

**The frontend's candidates table** (runbook section 6, "Why
this procedure") renders one row per entry in
`candidates_ranked`, sorted by the multi-axis key (winner on
top). The winning row is highlighted and its `factors` block
includes a `rank_key_comparison[]` list (4 entries, one per
axis) with `deciding: true` on the axis that picked the
winner. Non-winning rows show their catalog fields plus a
`factors.rejected_because` text that names the deciding
axis ("lexicographic rank: mission_impact major (0.5) >
winner none (0.0); deciding on mission_impact; risk_score
not consulted"). This is the demo surface the catalog-level
editorial fields (D-16) and the multi-axis ranking (D-19)
are designed to support.

**Note on the shape delta from earlier BIBLE drafts.** The
`Proposal` shape shown here is the *current* landed contract
(D-16, D-17, D-18, **D-19**). The runbook-enrichment fields
that earlier drafts listed at the top level —
`affected_subsystems`, `expected_channels`, `risk_class`,
`approval_required`, `procedure_description`, `symptom_window`,
`twin_state_endpoints`, the `trajectory` block with the
3-field canvas data, the `candidates_considered_detail` block,
the `pipeline` block with per-stage timestamps, the
`approval` block, and the `winner_rank_key` field — have
**all landed in D-19** as part of the runbook builder
output (see "Runbook API (D-19)" in §3.1 and "The runbook
page" below). The frontend's `/runbook` page reads them
from `GET /api/runbooks/{runbook_id}`; the `Proposal` type
above is what the WS `twin_verdict` field carries (a
strict subset, sufficient for the dashboard's verdict
panel).

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

#### Channel → subsystem → state-field map (EXACT — copy this into the frontend)

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

#### The 3D satellite model (what to build)

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
at the bottom, near the battery indicator; Thermal card
on the right, near the radiators; ADCS card centered,
near the wheel cones). The exact card positions are
tuned at build time; the constraint is "no vertical
stack."

**Color logic:** every frame, the SatelliteScene reads
`twinStateEndpoints` from `telemetryStore` and updates
each subsystem mesh's `material.color` via `useFrame`.
The body's overall color is the worst-case (red if any
subsystem is red, else amber if any is amber, else green
if any is green, else neutral gray when no verdict has
arrived).

#### The subsystem cards (the source of truth for per-subsystem state)

Each card is a fixed-position panel (~280px wide, varies
in height) showing:

- **Header** — subsystem name (e.g. "EPS") in human
  form ("Electrical Power"), connection status dot
  (green if any channel's latest EWMA error is below
  threshold, amber if within 50% of threshold, red if
  above).
- **ChannelRows** — one per channel the subsystem owns
  (see the channel → subsystem map). Each `ChannelRow`
  shows:
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
  green/amber/red color from the range check table.
  Before any verdict: "**State: awaiting first verdict**"
  in neutral gray.

#### The `kind` rule (NEVER show the operator's injection kind as a label)

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

#### The runbook page (Milestone 2 — full BIBLE §2 / §8 / §9 contract)

Route: `/runbook?rb=<runbook_id>`. Reads the runbook
from `GET /api/runbooks/{runbook_id}` (full payload) and
optionally `GET /api/runbooks/{runbook_id}/trajectory` for
the canvas (when the lightweight poll path is preferred).
The store keeps the most recent 50 runbooks in memory;
older ones are evicted. Renders 9 sections in order, with
the data paths called out explicitly so the implementing
agent knows which field maps to which endpoint field:

1. **Verdict status badge** — `OK` (green) / `REJECT`
   (red) / `INCONCLUSIVE` (amber), from `runbook.verdict.status`.
2. **Verdict reason** — one-line from `runbook.verdict.reason`.
3. **Diagnosis** — cause (from `runbook.cause.id`), cause
   score bar (0–5 with the 0.5 `MIN_DIAGNOSE_SCORE` marker,
   reading `runbook.cause.score`), affected subsystems
   chips (`runbook.cause.affected_subsystems`), expected
   channels chips (`runbook.cause.expected_channels`),
   pipeline-stage timestamps for "when this happened"
   (`runbook.pipeline.stage_2_diagnose.at_unix`, etc.).
4. **Procedure** — procedure (humanized, from
   `runbook.candidates_ranked[0].procedure`), description
   (from `runbook.candidates_ranked[0].description`),
   parameters table (key-value with units, from
   `runbook.candidates_ranked[0].procedure_params` on the
   corresponding WS `twin_verdict` payload — the runbook
   stores the procedure name + rank, the params travel on
   the verdict block), risk score gauge (0–1 with the 0.3
   `RISK_THRESHOLD_INCONCLUSIVE` marker, from
   `runbook.candidates_ranked[0].risk_score`), risk class
   pill (color-coded by
   `runbook.candidates_ranked[0].risk_class`), approval
   required pill (color-coded by
   `runbook.candidates_ranked[0].approval_required`).
5. **Twin prediction** — linked small multiples
   trajectory canvas: 3 canvases stacked vertically, all
   sharing the x-axis (0–60 min, 31 points). Fields:
   `battery_soc`, `battery_temp_c`, `payload_temp_c`.
   Each canvas shows predicted (solid) and baseline
   (dashed) series, with constraint lines drawn as
   horizontal threshold lines. An injection band drawn
   as a translucent vertical band at t=0. The arrays come
   from `runbook.trajectory[field].baseline[]` and
   `runbook.trajectory[field].predicted[]`, or
   `GET /api/runbooks/{id}/trajectory` if the page prefers
   a separate fetch.
6. **Why this procedure** — candidates ranked table
   (multi-axis lexicographic ranking per D-19). Each row:
   rank | procedure | risk_score | effort_score |
   mission_impact | reversibility | highlighted if it's
   the winner. Above the table, the **"why this won" panel**
   surfaces `runbook.winner.factors.reason` (the human-
   readable explanation of the deciding axis) and
   `runbook.winner.factors.rank_key_comparison[]` (the
   4-axis per-axis comparison with the `deciding: true`
   marker). Below the table, the **"considered vs
   simulated" panel** renders
   `runbook.candidates_considered_detail[]` with the
   `simulated: true|false` flag and `drop_reason` for
   pre-filtered procedures — the operator sees the full
   set the cause mapped to, not just the simulated subset.
7. **Evidence** — symptom window list
   (`runbook.symptom_window` rendered as
   `t | channel | subsystem | kind | score` rows) and
   constraint violations table (only populated for
   REJECT verdicts; renders a red-bordered table with
   the field name and the timestep it broke — comes from
   the verdict's `per_step_outcomes[]`).
8. **Approval** — Approve / Reject buttons (Milestone 2
   stub: the buttons are visible, tappable, colored by
   the required role from
   `runbook.candidates_ranked[0].approval_required`; on
   click they show a toast: "Approval deferred to Phase 2
   (OPA/Rego). Verdict recorded."). The `runbook.approval`
   block carries `status: PENDING` until Phase 2 wires the
   real gate. This is intentional: the UI rehearses the
   Phase 2 surface today so the operator's mental model
   carries forward unmodified.
9. **Footer metadata** — fault_id (e.g. "F-001") from
   `runbook.fault_id`, generated-at timestamp from
   `runbook.footer.generated_at_unix`, runbook_id from
   `runbook.runbook_id` (now a real `RB-YYYYMMDD-NNNN` id
   from the store, not the Phase 3 stub), twin digest
   placeholder. The `twin_simulation_digest` and
   `proposal_id` fields in the verdict are empty strings in
   Phase 1; the footer reserves the slot.

#### The terminal log (the operator's narrative surface)

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
`navigate('/runbook')` lets the operator see the message
before the route swap.

#### State management (4 Zustand stores, all module-level singletons)

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

#### Backend extension (Milestone 1, single commit before frontend work)

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

#### What the frontend is NOT

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

#### Test gate

- **Backend:** the existing 4 `test_injection_bridge.py`
  tests stay green; one new `test_propose.py` test for
  the enriched payload. The 2 pre-existing keras-gated
  failures (`test_load_predict`,
  `test_inject_shift_triggers_anomaly_over_websocket`)
  remain expected and unchanged.
- **Frontend:** vitest unit tests for `lib/humanize.ts`,
  `lib/riskClass.ts`, store reducers, log formatter.
  No canvas tests (rendering verified manually).

#### Working artifact

`/home/rishabh/.claude/plans/snug-forging-tome.md` —
the detailed implementation plan. Source of truth for
the build order; this BIBLE section is the human-
readable summary for the implementing agent.

### 3.3 — What's left for future

This is the "deferred" section: what's in plan but not in
code, in priority order.

#### Phase 2 — Approve / Execute

The policy engine (OPA/Rego with a Python fallback per
§7), the 4-class risk scheme (A/B/C/D) with the mapping
table from the current `low/medium/high/critical` strings,
the 2-person rule for Class D, the 30-second hold-down
timer, the dry-run preview UI, and the `CommandBus` mock
are all Phase 2 work.

**What this means concretely:**

- The `procedures.py` registry already has the
  `approval_required` metadata per procedure
  (`auto` / `operator` / `director` per the spec in
  `twin/procedures.py`). Phase 2 wires it into a real
  gate, not a registry field.
- The `Verdict` dataclass already reserves the
  `proposal_id` and `twin_simulation_digest` fields
  (empty strings in Phase 1). Phase 3 fills them in.
- The `risk_class` mapping (current strings
  `low/medium/high/critical` → future A/B/C/D) is a
  small mapping table. The draft mapping (low→B,
  medium→C, high→C, critical→D) is in
  `live/twin_bridge.py:36-37`; Phase 2 ratifies it
  in the BIBLE §7.

#### Phase 3 — Verify

The Merkle-chained runbook, the content-addressed evidence
store, the replay harness, and the 7-year retention
hook. The runbook schema (per §8) has `runbook_id`
(sha256-derived), `merkle_root`, `chain_head`,
`per_step_outcomes[]` with `prev_step_hash` /
`self_hash` chaining, and the `evidence://<sha256>`
content-addressing for telemetry, twin state, twin
simulation trace, model weights digest, and Rego
bundle.

#### Phase 4 — LLM narrator + RAG

A post-processor on Diagnose, Propose, Validate outputs
that writes human-readable prose around the already-
structured data. No seam in Phase 1 code (per D-6);
added in Phase 4 with the LLM, not as a no-op stub.
RAG over past runbooks (produced by Phase 3) for
novel-cause cases where `get_candidate_procedures(cause)`
returns an empty list.

#### Phase 1 polish items (deferred)

These are improvements to the *current* Phase 1 surface,
not new stages. They're documented here so they don't
get lost:

- **D-1 diagnostic gap.** Diagnose can disambiguate
  the three thermal causes (`stuck_off` vs `stuck_on`
  vs `runaway`) better with a richer symptom shape.
  Today they tie at score 5.0 for a single shift on
  T-1, and stable sort picks the first. The fix is to
  add a `kind` field to the `expected_channels` rubric
  so a `shift` event scores higher for `stuck_on` than
  for `stuck_off`. This is a Diagnose improvement, not
  a new stage; the change is in
  `digital-twin/twin/diagnose.py` and the per-cause
  expected-channel lists in
  `digital-twin/twin/procedures.py`.
- **`alerts_to_symptom_events` legacy hardcode.** The
  bridge function at `live/twin_bridge.py:138-180`
  hardcodes `channel="P-1"` and `subsystem="eps"` for
  the fall-back path (no `channel_hint`). The fix is
  to plumb `channel` into the live `AlertEvent`
  (`live/error_stream.py:41-46` doesn't carry it
  today) and use it. The fast path
  (`channel_hint` provided) already uses the right
  channel; this is just the fall-back cleanup.
- **`risk_class` mapping.** The current `procedures.py`
  uses `low/medium/high/critical` strings; the planned
  §7 scheme is `A/B/C/D`. Phase 2 work.
- **CHESS venv CI.** The 2 keras-gated tests
  (`test_load_predict`,
  `test_inject_shift_triggers_anomaly_over_websocket`)
  are pre-existing failures on system Python. They
  pass when the CHESS venv is active. CI setup is
  out of scope for Phase 1.

### Working plan (not in the repo)

- `/home/rishabh/.claude/plans/yes-go-forward-implement-expressive-lobster.md`
  — the post-merge BIBLE update plan (this section was
  written from it).
- `/home/rishabh/.claude/plans/glowing-painting-possum.md`
  — the pre-merge digital-twin planning document.
  Historical context; the actual architecture that landed
  is in §6 of this BIBLE.
- `/home/rishabh/.claude/plans/glowing-painting-possum-agent-ab34f718c65d76b95.md`
  — research summary (multi-agent patterns,
  satellite-ops references, digital-twin patterns, HITL
  patterns, audit patterns). Historical context.
- `/home/rishabh/.claude/plans/glowing-painting-possum-agent-a7ec1f2bc7082d7f2.md`
  — early detailed design draft (superseded by the
  pre-merge plan and this BIBLE, but kept for context).

---

## 4. Decisions we took (and the alternatives we rejected)

This section is the project's *truth base* for future work. When a future
agent or contributor asks "why did we do it this way?", the answer is
here. **Do not re-litigate these decisions without first reading this
section in full.** If you change one, update the corresponding entry
here in the same commit.

### D-1. Causal knowledge source: hand-authored YAML catalog, not a learned DAG

> **Status (updated 2026-08-30):** this decision is **partially superseded**
> by the incoming digital twin. The "YAML catalog" framing is replaced by
> a single Python module per D-10; the alternatives rejected here (learned
> DAG, LLM-only RCA) remain rejected. The "hand-authored, not learned"
> rationale is unchanged — what changed is the *file format*, not the
> *source-of-truth principle*.

**Decision (original):** the cause catalog is a hand-authored YAML file
(`mission_ops/knowledge/causes.yaml`) loaded at startup. The Diagnose
algorithm is a deterministic pattern-matcher that scores cause entries
against a sliding window of `SymptomEvent`s and returns the top-k
candidates with the evidence chain attached.

**Alternatives considered:**

- **(a) Bayesian posterior over a learned DAG** (e.g., NOTEARS, PC
  algorithm) from historical labeled telemetry. Rejected for Phase 1
  because (i) we don't have enough labeled data to learn a stable DAG,
  (ii) learned DAGs are notoriously unstable on small datasets, (iii) the
  resulting model is harder for ops engineers to audit and correct.
- **(b) Pure LLM RCA** (the LLM sees the symptoms, the LLM names the
  cause). Rejected because LLM-only RCA gives a confident answer with no
  evidence chain. It cannot satisfy the audit requirement.

**Why we chose what we chose:** the cause catalog is exactly the shape of
a real satellite Fault Management (FM) document — the document every
real ops center maintains. Modeling it as **data, not code** means
ops engineers can update it without a deploy, regulators can audit it
without reading Python, and the Diagnose algorithm can evolve (pattern
matcher today, Bayesian reasoner tomorrow) by swapping only the ranking
function. The catalog schema stays the same.

**Future evolution:** Phase 4 (or later) may add a Bayesian posterior
over a hand-built causal DAG as an alternative ranker. The catalog
schema is designed to support this: each entry's `propagation_path`
becomes the DAG edge set. The LLM (Phase 4) becomes a narrator that
writes prose around the already-ranked list, citing the catalog entry
and the symptom events.

---

### D-2. Procedure source: hand-authored procedure catalog, not LLM-drafted

> **Status (updated 2026-08-30):** this decision is **partially superseded**
> by D-10. The "no LLM-drafted procedures" rationale remains valid; what
> changed is the file format (YAML → single Python module). The LLM is
> still barred from authoring procedures in any phase.

**Decision (original):** the procedure catalog is a hand-authored YAML file
(`mission_ops/knowledge/procedures.yaml`). Each procedure is a typed
template with ordered steps, each step having `action` (from the twin's
vocabulary allowlist), `params`, `expected_state`, `abort_on`, and
per-step `risk_class`. The LLM (in Phase 4) can *select* a procedure from
the catalog but cannot invent one.

**Alternatives considered:**

- **(a) LLM-drafted procedures.** Rejected because the LLM can
  hallucinate unsafe or out-of-vocabulary steps. Even with a JSON-schema
  constraint, LLM-drafted procedures are not auditable to the standard
  real ops centers require.
- **(b) RAG over past runbooks only.** Rejected as the *sole* source
  because novel anomalies have no prior runbooks. We need a hand-
  authored baseline.

**Why we chose what we chose:** real ops centers only fly pre-approved
procedures. The procedure catalog is reviewed and signed off before any
procedure runs on a spacecraft (or, in our case, the twin). The LLM
drafting procedure text is a Phase 4 stretch goal for novel-cause
cases, and only with strict template validation + human sign-off before
the procedure enters the catalog.

---

### D-3. Catalog coverage: 13 causes × 9 procedures × ~21 edges

> **Status (updated 2026-09-01):** the post-merge final-form decision.
> The pre-merge BIBLE had D-3 as "4 causes × 2 procedures" with a
> "superseded by the incoming digital twin" note pointing at D-10/D-11.
> With the digital-twin branch landed, the catalog scope is final
> (13 × 9 × ~21, per D-11) and the catalog lives in a single Python
> module (`digital-twin/twin/procedures.py`, per D-10). The original
> 4×2 rationale and tradeoff table are preserved below as historical
> context — they show the path we did *not* take and the reasoning.

**Decision (final, as landed):** the Phase 1 catalog is **13 causes ×
9 procedures × ~21 cause-procedure edges**, defined in
`digital-twin/twin/procedures.py`. 12 diagnosable faults + 1 sentinel
`no_fault_detected` cause. The 9 procedures are reusable across
causes (e.g., `eps_shed_non_essential_load` is a candidate for 4
different EPS causes). The 13 causes, 9 procedures, and the
cause→procedure map are documented in D-11 and reproduced in §6.2
and §6.3.

**Why 13 × 9 × ~21 (and not 4 × 2):** the catalog scope is the
twin's scope. A smaller catalog would mean inventing causes and
procedures the twin can't actually simulate, which is exactly the
drift D-10 prevents. A larger catalog (50+ causes) would mean
the owner is doing FM-doc authorship work that the project
doesn't need. 13 × 9 × ~21 is what the twin supports, which is
what the demo needs.

---

**Decision (original, preserved for historical context):** Phase 1
ships with **4 fully-developed causes**, each with **2 fully-developed
procedures**. Total: 8 procedures. Every cause has a real
`symptom_pattern`, `propagation_path`, `candidate_procedures`, and
`references`. No stubs. No "shape-only" entries.

**The tradeoff we considered:**

| Catalog size | Pro | Con |
|---|---|---|
| 1 cause, 1 procedure (minimum) | Cheapest, tightest regression test | Doesn't exercise the Propose ranking or multi-cause Diagnose disambiguation. |
| **4 causes, 2 procedures each (chosen)** | Exercises cause disambiguation in Diagnose. Exercises procedure selection in Propose. Demo tells a real story. | More catalog content to write and review. |
| 10+ causes, 3+ procedures each | Comprehensive. Looks like a real FM doc. | Significant writing cost. Credibility drops if written without ops-engineer review (we don't have one). |

**Why we chose what we chose:** 4 causes × 2 procedures is the sweet
spot. It's enough to exercise (i) the Diagnose ranking across competing
causes, (ii) the Propose selection between candidate procedures, and
(iii) the twin's per-procedure validation including the
`abort_on`-currently-true → REJECT path. It's not so much that the
catalog content would start to look thin to a real reviewer.

**Future evolution:** add more causes and procedures as the catalog
matures. The schema is designed to scale to 100+ entries without
restructuring. The loader validates every entry; adding entries is a
pure data change (PR to the YAML, no code change).

**The cost of this decision is recorded here so future agents do not
re-derive it.** If you are tempted to "expand the catalog for
comprehensiveness" before shipping Phase 1, ask: does the test coverage
require it? If not, defer until ops review.

---

### D-4. Diagnose window: 1 minute

**Decision:** the default Diagnose window is 60 seconds (1 minute). The
window is configurable per-cause via `min_duration_ticks` in the cause
catalog entry.

**The tradeoff:**

- **Shorter windows (1 min or less)** let Diagnose fire on fast
  causes. But slowly-developing causes (sensor drift over 10 minutes)
  won't accumulate enough evidence.
- **Longer windows (5+ min)** catch slow causes but risk stale-evidence
  pollution when a *new* cause starts mid-window.
- **Per-cause `min_duration_ticks`** is the right knob. The global
  default just sets the upper bound; each cause declares how many
  ticks its evidence pattern needs.

**Why 1 minute:** the live detector emits `AlertEvent`s at ~5 Hz (see
`live/config.TICK_HZ = 5.0`). A 1-minute window contains up to 300 ticks
and up to ~60 symptom events. That's enough to disambiguate the
13 causes (see §6.2) with per-cause `min_duration_ticks` in the
30–90 tick range. Slower-developing causes (e.g., `solar_degradation`,
a gradual drop in P-2 current) declare their own longer
`min_duration_ticks` and are processed on a longer rolling window;
the Diagnose stage keeps a hot 1-minute window and a cold 5-minute
window, and each cause picks which it consumes.

**Future evolution:** tune the windows per-cause as real telemetry
patterns emerge. The current values are educated guesses based on the
synthetic generator.

---

### D-5. Knowledge catalog: repo-only, read-only

**Decision:** the cause and procedure catalogs live in
`mission_ops/knowledge/` and are loaded at startup. **They are not
editable at runtime in Phase 1.** Changes go through a PR and review.

**Alternatives considered:**

- **Runtime catalog editor** (a CLI to add/update/archive causes and
  procedures while the system is running). Rejected for Phase 1 because
  (i) it's more code to test, (ii) the regression test loses hermeticity
  if catalog state is mutable, (iii) real FM changes go through review,
  not ad-hoc edits. The right process for FM changes is a PR.

**Why we chose what we chose:** the catalog is application knowledge,
not user input. It deserves the review process of code. Runtime editing
is a Phase 2 or Phase 3 feature if it ever lands; the `KnowledgeBase`
interface is designed to support it as a future addition.

---

### D-6. LLM narrator: deferred to Phase 4, no seam in Phase 1 code

> **Status (updated 2026-08-30):** the original D-6 added a `Narrator`
> protocol with a no-op implementation in Phase 1, on the theory that
> it would make Phase 4 a strictly additive change. The owner reviewed
> this and decided: no seam. Phase 1 code has **no** narrator protocol,
> **no** no-op implementation, and **no** LLM involvement of any
> kind. The structured outputs (Diagnosis, Proposal, Verdict) carry
> only structured data; prose is added in Phase 4 as a post-processor.
> This entry is kept as a future-implementation note so Phase 4 has a
> clear spec to follow.

**Future-implementation notes for Phase 4 (NOT to be acted on in Phase 1):**

- **What gets added:** a `Narrator` protocol (or equivalent interface)
  with a real LLM implementation (Claude API, local model via
  ollama/llama.cpp, or hosted service — owner's choice). The
  narrator is a post-processor: it takes a structured output and
  returns a `Diagnosis` / `Proposal` / `Verdict` with a new
  `llm_narrative` field populated.

- **Where it plugs in:** three places, all as post-processors
  (not in the critical path of stage decisions):
  1. After Diagnose: wraps the ranked candidates + evidence chain
     into a human-readable explanation citing the catalog entry and
     the symptom events.
  2. After Propose: wraps the chosen procedure + dry-run diff into
     a human-readable explanation of why this procedure over the
     other candidates.
  3. After Validate: wraps the Verdict into a human-readable
     explanation of what the twin predicted and what it means for
     the operator.

- **Structural bars (designed but not enforced in Phase 1):**
  - The LLM has no path to issue an `ApprovalToken` (Phase 2 concern).
  - The LLM cannot change the `Cause` enum value picked by Diagnose;
    it can only write prose around the already-picked value.
  - The LLM cannot change the `Procedure` enum value picked by
    Propose; it can only write prose around it.
  - The LLM's prose is checked for citation grounding: every claim
    in the narrative maps to a catalog entry or an evidence ref.
  - The LLM is a pure function of its inputs in tests (mocked at the
    HTTP layer for hosted models, or a fixed response for local
    models).

- **What this D-6 explicitly forbids in Phase 1:**
  - No LLM SDK in `mission_ops/requirements.txt` or `pyproject.toml`.
  - No `Narrator` protocol or interface in Phase 1 code.
  - No LLM in the test suite (mocks are fine, but the LLM is never
    called in real test runs).
  - No API keys in the repo, even in `.env.example`.
  - No prompt-injection hardening work (deferred to Phase 4 with
    the LLM).

**Why defer the seam entirely:** the no-op seam added a tiny bit of
code and a tiny bit of test surface for a feature that may never
ship (Phase 4 is a real future phase but its scope is not yet
locked). Cleaner to add the seam in Phase 4 with the LLM than to
carry a no-op seam through every Phase 1+ commit.

**Why this is not a "boring" decision:** the temptation to add the
seam is strong because it makes Phase 4 feel cheap. It is not cheap
in the long run: every Phase 1+ commit has to keep the no-op
working, the regression test has to assert the empty string, and the
schema has a `narrative: str = ""` field that no one reads. Defer
until needed; the cost of adding the seam in Phase 4 is one new
field, not a refactor.

---

### D-7. Twin scope: 5 subsystems (EPS, Battery, Thermal, ADCS, Comms) and 8 channels

> **Status (updated 2026-09-01):** the post-merge final-form decision.
> The pre-merge BIBLE had D-7 as "3 subsystems (Comms, Power,
> Thermal)" with a "superseded by the incoming digital twin" note
> pointing at D-11. With the digital-twin branch landed, the twin
> scope is final (5 subsystems, 8 channels, 2,791 lines in
> `digital-twin/twin/`). The original 3-subsystem rationale and
> tradeoff are preserved below as historical context.

**Decision (final, as landed):** the Phase 1 twin models **5
subsystems** — EPS, Battery, Thermal, ADCS, Comms — across **8
channels** (P-1, P-2, B-1, T-1, T-2, A-1, G-1, D-1) and **13
causes × 9 procedures × ~21 edges** (per D-3 and D-11). The twin
is a lumped, deterministic state machine; same state + same
procedure → same result, byte-for-byte. 2,791 lines in
`digital-twin/twin/`; per-file line counts in §3.1.

**Why 5 subsystems (and not 3):** the owner supplied the
twin spec with all 5 subsystems. The 3-subsystem "minimum
viable" framing was a planning-time approximation; the actual
demo needs EPS, Battery, Thermal, ADCS, and Comms to show the
cross-subsystem coupling that Diagnose reasons about. With 5
subsystems, the Diagnose stage can score cross-coupling
(`eps_internal_r_ramp` affects both EPS and Thermal because
battery temperature couples to internal resistance; see
§6.2 row 1). With 3 subsystems, that signal is lost.

---

**Decision (original, preserved for historical context):** the
Phase 1 twin models 3 example subsystems: Comms, Power, Thermal.
This is a closed-world example, not a general satellite
simulator.

**Alternatives considered:**

- **6+ subsystems (Attitude, Propulsion, GNC, Payload, ...).**
  Rejected because (i) the build cost scales linearly with
  subsystem count, (ii) the credibility of a "fake physics" twin
  drops sharply once you start modeling attitude dynamics,
  propulsion, and orbital mechanics, (iii) we'd be writing a
  bad simulator rather than demonstrating the validation
  contract.

**Why we chose what we chose:** 3 subsystems with well-understood
cross-coupling (thermal affects power efficiency; power affects
comms output; comms is the primary observable) is enough to
demonstrate cross-subsystem diagnosis and recovery. The schema
generalizes; adding a 4th subsystem later is an additive change
to `subsystems.yaml` and the twin's vocabulary.

**Owner note:** the twin scope is being **re-specified by the owner
post-plan.** This section will be updated when that spec lands.

---

### D-8. Bus: in-process `asyncio.Queue` for Phase 1

**Decision:** Phase 1 uses an in-process `asyncio.Queue`-backed
`EventBus`. The `EventBus` interface is what gets swapped to NATS or
Redis in a later phase; the in-process implementation is the default
for now.

**Why:** single-process demo. No NATS, no Redis, no Kubernetes. The
topology is portable but the demo runs as one process. The swap point
is explicit (`mission_ops/bus.py` defines the interface; the
implementation is one file).

---

### D-9. Repo conventions: see §11

Decisions about file layout, ADRs, testing, commits, and branches are
in [§11 Repo conventions](#11-repo-conventions). These are policy
decisions and the rationale lives there.

---

### D-10. The cause catalog and the twin procedure registry are the same file

> **Status (added 2026-08-30):** this decision is **new**, driven by the
> incoming digital twin. The original D-1 and D-2 framed the catalogs as
> hand-authored YAML; this is replaced by a single Python module
> (`mission_ops/twin/procedures.py`) that defines the causes, the
> procedures, the parameter schemas, the pre/post-conditions, the
> approval requirements, **and** the `apply_procedure(state, procedure,
> params)` function the twin uses to execute them.

**Decision:** the cause enum, the procedure enum, the procedure
parameter schemas (with bounds), the precondition/postcondition
predicates, the approval requirement per procedure, and the twin's
`apply_procedure()` function are **all defined in a single Python
module**. Both the catalog and the twin import from it. There is no
YAML or JSON file separately loaded at runtime.

**Alternatives considered:**

- **(a) YAML catalog + Python twin** (the original D-1/D-2 plan). A
  YAML file holds the catalog; a Python module holds the twin's
  `apply_procedure()` function. The twin validates that the catalog's
  procedure IDs match the ones it can execute.
  - **Rejected** because the validator is run at startup; if the
    catalog and the twin are edited independently (the most likely
    real-world path), they can drift until the next restart, and the
    validator only catches *registered* mismatches, not semantic ones
    (e.g., a procedure whose parameter schema changed but the catalog
    still has the old defaults).
- **(b) Code-generated catalog from a typed schema.** Use a
  code-generator that produces the catalog from a schema definition.
  - **Rejected** for Phase 1 because (i) it adds a build step, (ii)
    the schema generator is itself code that has to be maintained,
    (iii) the drift it prevents is the same drift that (a) prevents,
    less robustly.
- **(c) Single Python module (chosen).** The catalog *is* the code.
  The cost is that editing the catalog is editing code (it goes
  through the same review as code), but the benefit is that the
  catalog cannot lie about what the twin can do, because both come
  from the same file.

**Why we chose what we chose:** the catalog and the twin are coupled
by definition — the catalog says "procedure X is available for cause
Y," the twin says "procedure X does Z to state S." If they live in
separate files, they can drift in ways that produce silently wrong
diagnoses (catalog says a procedure exists and is safe; twin
crashes or behaves differently at runtime). Putting both in one file
makes drift *structurally impossible* — you cannot change the
twin's behavior without changing the catalog entry that references
it, and vice versa.

**Tradeoff:** the catalog is now code, not data. Ops engineers cannot
update it via a config-file change alone; they need a PR. This is
acceptable for telos because (i) the catalog changes infrequently
relative to code changes, (ii) the PR review is the right place for
ops-engineer review anyway, (iii) the data/code distinction was
always slightly artificial — the YAML was always going to need a
schema validator that lived in code.

**API contract:**

```python
from mission_ops.twin.procedures import (
    Cause, Procedure, PROCEDURE_REGISTRY,
    get_candidate_procedures, apply_procedure,
)

# Diagnose stage
diagnosis: Cause = Cause.EPS_INTERNAL_R_RAMP  # exactly one of the enum values

# Propose stage
candidates: list[Procedure] = get_candidate_procedures(diagnosis)
spec = PROCEDURE_REGISTRY[Procedure.EPS_SHED_NON_ESSENTIAL_LOAD]
spec.validate_params({"load_reduction_a": 1.5, "duration_s": 300})

# Validate / twin
new_state = apply_procedure(current_state, Procedure.EPS_SHED_NON_ESSENTIAL_LOAD, params)
```

**Future evolution:** if the catalog grows past ~50 causes, splitting
*back* into a generated catalog (option b) becomes worth the build
step. Until then, single module is the right call.

---

### D-11. Catalog scope: 13 causes, 9 procedures, ~21 edges, 5+ subsystems

> **Status (added 2026-08-30):** this decision is **new**, driven by the
> incoming digital twin. The original D-3 said "4 causes × 2 procedures
> each"; this is the actual scope from the owner's twin spec.

**Decision:** the Phase 1 catalog has **13 causes** (12 diagnosable
faults + 1 `no_fault_detected` sentinel), **9 unique procedures**, and
**~21 cause-procedure edges** (some procedures are valid for multiple
causes; the cause→procedure map is the source of truth in
`get_candidate_procedures()`). The twin models **5+ subsystems**:
EPS (channels P-1, P-2), Battery (B-1), Thermal (B-1, T-1, T-2),
ADCS (A-1, G-1), Comms (D-1), plus a `sensor_noise` meta-cause.

**Why this is the scope, not something smaller or larger:**

- The catalog scope is the twin's scope. A smaller catalog would
  mean inventing causes and procedures the twin can't actually
  simulate, which is exactly the drift D-10 prevents.
- A larger catalog (50+ causes) would mean the owner is doing FM-doc
  authorship work that the project doesn't need; the 13 causes here
  are the ones the twin can demonstrate, which is what the demo
  needs.

**The 13 causes:**

`eps_internal_r_ramp`, `eps_load_step`, `battery_overdischarge`,
`battery_undervoltage`, `thermal_heater_stuck_off`,
`thermal_heater_stuck_on`, `thermal_runaway`, `adcs_star_tracker_lost`,
`wheel_saturation`, `solar_degradation`, `comm_ground_station_lost`,
`sensor_noise`, `no_fault_detected`.

**The 9 procedures:**

`eps_shed_non_essential_load`, `eps_increase_charging_priority`,
`thermal_enable_heater_backup`, `thermal_throttle_payload`,
`adcs_switch_to_safe_hold`, `adcs_reset_star_tracker`,
`comms_postpone_downlink`, `mode_change_to_safe`, `wait`.

**Channel taxonomy (preliminary, finalized when the twin lands):**

P-1 (EPS bus voltage), P-2 (solar panel current), B-1 (battery
state-of-charge / temperature shared with thermal), T-1, T-2
(thermal sensor readings), A-1 (ADCS star-tracker error), G-1
(ADCS reaction-wheel speed), D-1 (comms link margin).

**Approval terminology (preliminary, finalized when the twin lands):**

The owner's spec uses `auto`, `operator`, `director`, with
`low/medium/high` risk qualifiers on the operator path. The BIBLE
will be updated to map these to A/B/C/D (or to retain the owner's
3-tier scheme if that's preferred) once the twin's full approval
metadata is in. See §7 for the proposed mapping table.

**Future evolution:** as the owner adds causes (e.g., propulsion,
payload-specific faults), each new cause goes in as a new
`Cause` enum value, each new procedure as a new `Procedure` enum
value, and the cross-references update in one file. The structural
review (does the cause's symptom pattern match a real failure
mode? is the procedure's effect physically correct?) is what
matters, not the file count.

---

### D-12. Propose uses validation-based ranking, not catalog lookup

> **Status (added 2026-09-01):** this decision is **new**, driven
> by the digital-twin integration. The original BIBLE §2 framed
> Propose as "looks up procedure catalog by cause" — that framing
> is replaced by the validation-based ranking that actually landed
> in `digital-twin/twin/propose.py`.

**Decision:** Propose does **not** pick a procedure by
`default_procedure_id`, by catalog rank, or by any
`risk_class`-based heuristic. It calls `validate_procedure()`
for every candidate procedure that survives the
[pre-filter](#d-16-catalog-level-editorial-fields-effort-score-mission_impact-reversibility)
(see [D-16](#d-16-catalog-level-editorial-fields-effort-score-mission_impact-reversibility)),
ranks them by the `risk_score` Validate returns, and picks
the lowest. Ties are broken by stable sort (catalog order from
`get_candidate_procedures`).

The selection loop is in `digital-twin/twin/propose.py:propose()`:

```python
candidates = get_candidate_procedures(cause)

# 1. Pre-filter to top K by catalog-level signals (cheap, no twin).
scored = [(_coarse_rank_key(p), p) for p in candidates]
scored.sort(key=lambda x: x[0])
top_k = [p for _, p in scored[:PROPOSE_TOP_K]]

# 2. Validate all top-K in parallel in a ThreadPoolExecutor.
results: List[RankedCandidate] = []
with ThreadPoolExecutor(max_workers=min(PROPOSE_TOP_K, len(top_k))) as pool:
    for rc in pool.map(
        lambda p: _run_one(p, starting_state, horizon_s, dt_s, progress_cb),
        top_k,
    ):
        results.append(rc)

# 3. Sort by risk_score ascending; lowest wins.
results.sort(key=lambda x: x.risk_score)
best = results[0]
```

The pre-filter and the parallel sims are the new pieces;
see [D-16](#d-16-catalog-level-editorial-fields-effort-score-mission_impact-reversibility)
and [D-17](#d-17-propose-runs-top-k-simulations-in-parallel-and-streams-progress-to-the-frontend)
for the rationale. The original sequential `for` loop is
preserved in this BIBLE entry for historical context only.

Measured wall time: 30-120ms for the demo's 2-3 candidates at
the default 1h/120s horizon. With the parallel sims (D-17),
this is now ~1.5-2x the single-candidate time (GIL-limited)
instead of 3x. Either way, this is well under the operator's
patience budget and invisible to the live broadcast rate.

**Alternatives considered:**

- **(a) Lookup by `default_procedure_id`** (the original BIBLE §2
  framing). Rejected because (i) it requires the catalog to
  know which procedure is "best" for which cause, which
  duplicates knowledge that the twin can compute from the
  current state, (ii) it makes the procedure ranking a static
  property of the catalog rather than a dynamic property of
  the starting state, (iii) it defeats the purpose of having
  a twin at all — the twin is there to tell us what will
  happen, not to confirm a pre-chosen answer.
- **(b) Lookup by `risk_class`** (low-risk first, then medium,
  then high). Rejected for the same reason: `risk_class` is
  a static label; the actual risk depends on the starting
  state and the parameters, and the only honest way to know
  the risk is to run the twin.
- **(c) Validation-based ranking (chosen).** The twin computes
  the risk for each candidate. Propose becomes a thin ranking
  layer over Validate.

**Why we chose what we chose:** the twin is the source of truth
for "what will happen if I apply procedure X to state S."
Letting it do the ranking means Propose is honest — it picks
what the twin says is best, not what the catalog says should
be best. The cost is the time to validate every candidate;
the benefit is that the ranking is *always* correct for the
current state. The same cause with different starting states
can pick different procedures, which is the right behavior —
the operator wants the procedure that works *now*, not the
procedure that worked last Tuesday.

**Tradeoff:** the ranking is no longer deterministic from
the catalog alone. Two runs of Propose with different starting
states can pick different procedures for the same cause. This
is the right behavior. The cost is that the regression test
can't just check "this cause picks this procedure" — it has
to either seed the state with the same defaults
(`make_default_state()`) or check the *structure* of the
ranking rather than the specific winner. The current
`test_propose.py` does the former.

**The ranking is now parallel (updated 2026-09-04).** The
sequential `for proc in candidates` loop was replaced by a
`ThreadPoolExecutor` parallelization in the 2026-09-04 commit
(see [D-17](#d-17-propose-runs-top-k-simulations-in-parallel-and-streams-progress-to-the-frontend)).
The original rationale for sequential execution — that
parallelism would require locking on shared twin state and
the latency budget was already met — is now superseded: the
parallel runbook emits per-step progress events to the
WebSocket so the frontend can render the parallel sims
visually (a key demo surface for the 3-phase frontend build
in §3.2). Measured wall time on a 3-candidate run at the
default 1h/120s horizon is ~1.5-2x the single-candidate
time (not the theoretical 1x, because the Python GIL
limits the per-step bookkeeping in `_run_forward` even
though numpy releases the GIL for the math). This is
acceptable for Phase 1 and improves if the per-step math is
moved to a GIL-releasing primitive in the future.

**Future evolution:** if the candidate set grows past ~10
procedures, the per-candidate validation cost will dominate
the Propose stage latency even with parallelism. The fix
landed in 2026-09-04 as a coarse pre-filter using the
procedure's catalog-level editorial fields
(`effort_score`, `mission_impact`, `reversibility` — see
[D-16](#d-16-catalog-level-editorial-fields-effort-score-mission_impact-reversibility))
— still using Validate as the final arbiter, just with a
catalog-driven warm-up that keeps the top-K bounded.
Future growth past the current `PROPOSE_TOP_K = 5` is
addressed by raising the constant, not by code change.

---

### D-13. The twin runs in-process, same Python interpreter as live/

> **Status (added 2026-09-01):** this decision is **new**, driven
> by the digital-twin integration. The pre-merge BIBLE §6
> framed the twin as "sidecar" with a Phase 1
> "in-process, Phase 2+ separate process" evolution. The
> landed architecture is in-process for both phases; the
> "sidecar" language was aspirational, not descriptive.

**Decision:** the digital twin is loaded into the same Python
process as the live FastAPI server. `live/ws_server.py` imports
`twin` *lazily* (inside the function, not at module load) so
the CHESS astropy chain is not pulled in at boot. The
`FaultScheduler` (the part of the twin that holds active
faults) is a singleton on `app.state.live.twin_scheduler`,
initialized in `AppState.init_twin()` at server boot.

The lazy-import pattern lives in
`live/twin_bridge.py:_ensure_twin_on_path()`. The function
adds the `digital-twin/` directory to `sys.path` if it isn't
there, and is called at the top of every twin-using function
(`inject_fault`, `run_phase1_pipeline`). The bridge module
itself imports only stdlib + `os` + `logging` + `sys`; the
`twin.*` imports happen inside `run_phase1_pipeline()` after
the path is set. This is what lets the live server boot on
system Python without the CHESS venv.

**Alternatives considered:**

- **(a) Twin as a separate process (subprocess or gRPC).**
  Rejected for Phase 1 because (i) the IPC overhead is
  non-trivial for a per-tick validation, (ii) it requires
  shipping the starting state across the boundary, (iii) it
  adds a deployment story we don't have time for, (iv) the
  in-process twin is *fast* (~1ms per step) and the latency
  is invisible to the operator.
- **(b) Twin as a separate thread (threading, not asyncio).**
  Considered; the validation runs in the calling thread
  (which is the FastAPI handler thread). The
  `FaultScheduler` state is not currently thread-safe, but
  this is fine for Phase 1 because `/inject_twin_fault` is
  one-call-at-a-time — FastAPI's default sync handler means
  concurrent calls serialize on the GIL anyway.
- **(c) In-process, lazy imports (chosen).** Fast, simple,
  deterministic. The CHESS venv problem is solved by lazy
  imports. The live server stays single-threaded for
  fault-related work; the producer loop runs in its own
  asyncio task and shares no twin state.

**Why we chose what we chose:** in-process is the simplest
deployment story and lets us treat the twin like a library
call. The lazy-import pattern keeps the live server
bootable on system Python. This combination lets the
hackathon demo run on a laptop without any infra — `uvicorn
live.ws_server:app` is the only command.

**Tradeoff:** the live server can no longer boot on a system
where the `digital-twin/` directory is missing or where the
CHESS venv is not active — the `/inject_twin_fault` endpoint
returns 503 in that case (per
`live/ws_server.py:167-173`). This is the right behavior:
tell the operator the feature is unavailable, don't crash.
The `run_phase1_pipeline()` function returns `None` (not
raises) if the twin can't be imported, so the live
producer loop keeps running; the operator just doesn't see
a verdict on that tick.

**Future evolution:** Phase 2 should consider moving the
twin to a separate process so a crash in the twin doesn't
take down the live server. The seam is already in place —
the `FaultScheduler` and `validate_procedure()` are
self-contained; moving them across a gRPC boundary is a
refactor, not a redesign. The motivation grows if the
twin's per-step simulation cost goes up (e.g., if we add
orbital mechanics with NRLMSISE-00). Out of scope for
Phase 1.

---

### D-14. The bridge is the only place `live/` imports from `twin/`

> **Status (added 2026-09-01):** this decision is **new**, driven
> by the digital-twin integration. The pre-merge BIBLE §14
> named "the bridge" as the seam but did not codify the rule
> that it is the *only* importer; this entry makes the rule
> explicit.

**Decision:** `live/twin_bridge.py` is the *single* module in
`live/` that imports from `twin/`. All other `live/` modules
stay pure-Python and can be loaded without the CHESS venv.
The bridge exposes two public functions — `inject_fault()` and
`run_phase1_pipeline()` — and two public tables —
`INJECTION_TO_FAULT` (13 entries) and `CHANNEL_TO_SUBSYSTEM`
(8 entries).

**The rule in code:** `live/ws_server.py` imports
`live.twin_bridge` and uses `bridge.inject_fault()` and
`bridge.run_phase1_pipeline()`. The FastAPI handler never
imports from `twin.*` directly. The `live/static/`,
`live/tests/`, and `live/config.py` modules never import
from `twin.*` either. A grep for `from twin` or
`import twin` across `live/` should return exactly one
match: `live/twin_bridge.py`.

**Alternatives considered:**

- **(a) Import `twin` directly from `live/ws_server.py`**
  (the obvious place for the call). Rejected because (i)
  it makes the live server's boot path dependent on the
  CHESS venv, (ii) it puts twin-specific knowledge
  (fault types, channel mappings) in the FastAPI handler,
  which is the wrong layer — the handler is HTTP plumbing,
  not a domain expert, (iii) it makes the live test suite
  unable to run without the venv, (iv) it makes Phase 2's
  "twin as a separate process" refactor touch the HTTP
  layer instead of just the bridge.
- **(b) Bridge module (chosen).** A 348-line file that owns
  the seam. The FastAPI handler calls
  `bridge.inject_fault()` and `bridge.run_phase1_pipeline()`
  and never knows what's inside. The bridge's surface is
  the cross-boundary contract.

**Why we chose what we chose:** the boundary between the
streaming detector (`live/`) and the sidecar simulator
(`twin/`) is the most important boundary in the system.
Putting all the cross-boundary knowledge in one file
makes it auditable, testable in isolation, and replaceable
(e.g., to a gRPC twin in Phase 2) without touching the rest
of `live/`. The bridge's `run_phase1_pipeline` is the
single function the integration test exercises; if it
passes, the seam works.

**Tradeoff:** the bridge is a small "god module" that knows
about both sides. This is acceptable because the seam is
the whole point — the bridge's job is to be the seam. If
the bridge grows past ~500 lines, it should be split into
`live/twin_bridge/inject.py` and
`live/twin_bridge/pipeline.py`. Currently 348 lines, well
under the threshold.

**Future evolution:** the bridge is the natural place to
add a fault-injection rate limiter (Phase 2 — prevent the
operator from injecting 100 faults in 1 second), a
fault-history log (Phase 3 — for the runbook), and a
fault-cancel endpoint (Phase 2 — for the panic-abort path).
Out of scope for Phase 1.

---

### D-15. Terminal demos stay alongside the frontend; they answer different questions

> **Status (added 2026-09-01):** this decision is **new**, driven
> by the 3-phase frontend build (§3.2). The pre-merge BIBLE had
> no frontend at all; the terminal demos were the only surface.
> The 3-phase frontend changes the framing from "terminal is
> everything" to "terminal and frontend answer different
> questions."

**Decision:** Phase 1 ships with **four** runnable terminal
demos in `digital-twin/examples/`:

- `smoke_test.py` (198 lines) — pre-existing regression
  test. Exercises the `FaultScheduler` end-to-end with the
  CHESS simulation. Not run on system Python (it needs the
  venv); kept for the regression suite.
- `phase1_demo.py` (360 lines) — full live-loop demo. Boots
  the FastAPI server in-process, POSTs 6 scenarios over HTTP,
  reads from the WebSocket, prints a roll-up table.
- `phase1_demo_deep.py` (196 lines) — Diagnose + Propose +
  Validate trajectory detail for 2 scenarios (P-1 and T-1).
  Prints the per-cause Diagnose scoring, the Propose ranking
  with risk scores, and the Validate trajectory with 7
  columns (`pred/base` SoC, V, T_bat).
- `slight_shift_demo.py` (189 lines) — the single-scenario
  story. 0.2-unit shift on T-1, all 4 stages traced in one
  screen. Adds `T_pay` to the trajectory table.

The frontend (the 3-phase build in §3.2) is the surface for
the operator's view during a live injection. The terminal
demos are the surface for the *deep-detail* questions a
judge or reviewer asks after the injection ("show me the
per-cause scoring," "show me the trajectory table"). The
two surfaces are not redundant — they answer different
questions.

**Alternatives considered:**

- **(a) Terminal-only** (what we had before the frontend
  plan). The four scripts cover every angle: roll-up,
  deep, single, regression. Cheap to maintain, easy to
  demo, no web stack. Rejected as the final answer because
  the operator needs a live view *during* an injection,
  not a script that runs after.
- **(b) Frontend-only** (no terminal demos). Rejected
  because the terminal is the only surface that shows the
  full trajectory table and the per-cause Diagnose scoring
  breakdown. A canvas plot is lossy by comparison, and
  reviewing the per-cause scoring in a hover-tooltip is
  worse than reading a 7-column terminal table.
- **(c) Terminal + frontend (chosen).** The terminal is the
  deep-detail and audit-trail surface; the frontend is the
  live operator's view. Both surfaces consume the same
  data; the terminal reads directly from `twin.*`, the
  frontend reads from the WebSocket broadcast.

**Why we chose what we chose:** the terminal demos are
what the judges will see if they ask "show me the
diagnose scoring" or "show me the predicted vs baseline
trajectory." The frontend is what the judges will see if
they ask "show me the live loop." Both are valid and
answer different questions. Building both means we never
have to say "let me run a separate script for that."

**Tradeoff:** the demos duplicate the path setup
(`sys.path.insert(0, ...)` for both `live/` and `twin/`).
This is acceptable because (i) the duplication is ~3 lines
per demo, (ii) extracting it to a shared helper would
require the demos to import a `live`-or-`twin` module,
which defeats the "self-contained demo" goal — the
hacker running the demo on their laptop shouldn't need
to figure out our module layout.

**Future evolution:** the frontend (3 phases per §3.2) will
*not* subsume the terminal demos. The deep-dive scenarios
need the per-cause scoring table and the full trajectory
printout, both of which are awkward in a canvas. The
`phase1_demo.py` HTTP path is subsumed by the frontend's
Phase 1 (live data) layer; the other three terminal demos
stay as the audit trail.

---

### D-16. Catalog-level editorial fields: effort_score, mission_impact, reversibility

> **Status (added 2026-09-04):** this decision is **new**, driven
> by the 3-phase frontend build's need to render a sortable
> candidates table (and the parallel-sims panel) without
> re-reading the registry. The pre-2026-09-04 `Proposal` shape
> only carried the runtime `risk_score` per candidate; the new
> fields are the catalog-level editorial signal that lets the
> frontend answer "why this procedure, not that one" when the
> runtime `risk_score` alone doesn't disambiguate.

**Decision:** every `ProcedureSpec` carries three new
hand-authored, PR-reviewed fields in addition to the existing
`risk_class` and `approval_required`:

- **`effort_score: float`** in `[0.0, 1.0]`. Lower is less
  operator/spacecraft work. Drives the Propose pre-filter
  (lexicographic first key) and the frontend's candidates
  table sort.
- **`mission_impact: str`** — one of `"none"`, `"minor"`,
  `"major"`, `"mission-ending"`. How much mission capability is
  lost while the procedure is in effect. Distinct from
  `risk_class` ("how bad if it fails"): a `critical` procedure
  can be `none` (e.g., `mode_change_to_safe` is `mission-ending`
  for impact but `critical` for risk) and a `low`-risk
  procedure can be `major` (e.g., `thermal_throttle_payload`).
- **`reversibility: str`** — one of `"trivial"`, `"easy"`,
  `"hard"`. How easy it is to undo the procedure's effect once
  started. `"trivial"` = instant rollback, `"easy"` = stop the
  procedure and the spacecraft returns to nominal within a
  step or two, `"hard"` = the procedure's effect persists and
  recovery requires another procedure.

The import-time validation in `procedures.py` rejects any
registry entry with a `mission_impact` or `reversibility`
value not in the allowed set, or an `effort_score` outside
`[0.0, 1.0]`. The constant `VALID_MISSION_IMPACTS` and
`VALID_REVERSIBILITY` are exported so the test suite can
import the allowed sets without re-declaring them. The
default values for these fields on a bare `ProcedureSpec()`
are `effort_score=0.5`, `mission_impact="minor"`,
`reversibility="easy"` — conservative middle-of-the-road
defaults that surface in the test suite if a future procedure
is added without explicitly populating them.

**Alternatives considered:**

- **(a) Reuse `risk_class` for the pre-filter.** Rejected
  because `risk_class` and `effort_score` are correlated but
  not the same: `mode_change_to_safe` is `critical` for
  `risk_class` (the mission may not recover) but `0.85` for
  `effort_score` (one command, instant). Using `risk_class`
  for the pre-filter would skip `mode_change_to_safe` in
  cases where it was actually the right call; using
  `effort_score` lets the catalog express both axes.
- **(b) Reuse `approval_required` ("auto" < "operator" <
  "director") as the friction signal.** Rejected because
  `approval_required` is org-policy routing that drifts over
  time as policies change. The catalog-level editorial fields
  are *expected behavior*, not *org policy*; a procedure's
  `effort_score` should be stable across policy revisions.
- **(c) Add the three new fields as chosen.** They are
  orthogonal to the existing `risk_class` and
  `approval_required`, hand-authored in the same PR as the
  procedure's `apply_fn`, and reviewed by the same people who
  review the catalog. The cost is a small per-procedure
  editorial burden; the benefit is that the demo can answer
  "why this procedure" with structured data.

**Why we chose what we chose:** the three new fields are the
minimum editorial signal needed to render the candidates
table. With them, the runbook's "Why this procedure" section
(D-18) can show the operator: "we picked `eps_shed_load` over
`mode_change_to_safe` because even though `mode_change_to_safe`
has a lower `risk_score` (0.05 vs 0.18), its `effort_score` is
0.85 (vs 0.30) and its `mission_impact` is `mission-ending`
(vs `minor`). The system did the right thing; the table
explains why." Without the new fields, the same explanation
would require the operator to read the `apply_fn` body of
both procedures and reason about cross-subsystem coupling —
which is exactly the work the system is supposed to be doing.

**Tradeoff:** the three new fields are static catalog values,
not measured outcomes. The runtime `risk_score` from Validate
can disagree with the editorial signal — a procedure that the
catalog says is "easy" can turn out to have a high runtime
`risk_score` when applied to a degraded state. This is a
feature, not a bug: the disagreement is the system's signal
that the catalog is stale, and it surfaces in the runbook.
The alternative — making the catalog fields a function of
runtime state — would re-introduce the catalog-vs-twin drift
that D-10 was designed to prevent.

**Future evolution:** the allowed sets for `mission_impact`
and `reversibility` are deliberately small (4 and 3 values
respectively) to keep the editorial burden low. As the
catalog grows past ~50 procedures, two extensions become
worth considering: (a) splitting `mission_impact` into
`mission_impact_electrical`, `mission_impact_thermal`, etc.
when cross-subsystem procedures make a single
`mission_impact` value ambiguous; (b) adding a fourth
reversibility value `"irreversible"` for procedures that
require manual recovery outside the catalog. Neither is
needed for the current 9 procedures.

---

### D-17. Propose runs top-K simulations in parallel and streams progress to the frontend

> **Status (added 2026-09-04):** this decision is **new**, driven
> by the 3-phase frontend build (§3.2) which needs a live view
> of the parallel twin sims as they run. The pre-2026-09-04
> Propose was a sequential `for` loop that called
> `validate_procedure()` once per candidate; the new Propose
> runs the top-K (D-16) candidates in parallel in a
> `ThreadPoolExecutor` and emits per-step progress events that
> the bridge forwards to the WebSocket.

**Decision:** `twin.propose.propose()` runs the
pre-filtered top-K candidates in parallel using
`concurrent.futures.ThreadPoolExecutor(max_workers=min(PROPOSE_TOP_K,
len(top_k)))`. Each worker calls
`validate_procedure(starting_state, proc, params, ...,
on_step=on_step)` and the `on_step` callback forwards a
`(procedure_value, step, total, snapshot)` event to the
caller's `progress_cb`. The `live/twin_bridge.py` function
`run_phase1_pipeline` accepts the `progress_cb` as an
optional parameter and forwards it to `propose()`. The
`live/ws_server.py` inject handler builds a progress
callback that pushes each event into an
`AppState.sim_progress_queue` (`queue.Queue`, maxsize=2000);
the producer loop drains the queue once per tick and
attaches the most recent event to the next WebSocket
broadcast as the `sim_progress` field (D-17 contract, §3.2).

**Alternatives considered:**

- **(a) asyncio.gather with run_in_executor.** Rejected
  because the `propose()` function is currently sync
  (called from the sync `inject_twin_fault` FastAPI
  handler). Switching to `asyncio` would force the handler
  to be async, and the CPU-bound twin sims would still
  release the GIL via the executor — no net win.
- **(b) Sequential `for` loop, no per-step progress
  events.** This is the pre-2026-09-04 state. Rejected
  because the frontend demo surface is "watch 5 sims run
  in parallel"; without per-step events the frontend has
  no way to render that surface (it would only see the
  final verdict). The wall-time cost of sequential is
  acceptable for 2-3 candidates but not for the
  future-catalog 10+ candidates.
- **(c) ThreadPoolExecutor with per-step progress events
  (chosen).** Matches the 3-phase frontend milestone
  requirements (§3.2) and bounds the wall time as the
  catalog grows. The Python GIL limits the actual
  speedup (numpy releases the GIL for math but the
  per-step bookkeeping in `_run_forward` does not) — see
  "Measured wall time" in D-12.

**Why we chose what we chose:** the `sim_progress` event
stream is the demo surface for the parallel-sims panel in
the frontend. Without it, the only "evidence of parallelism"
the operator sees is the final verdict — which looks
identical to the pre-2026-09-04 sequential verdict. The
event stream is what makes the parallelism visible. The
bridge's choice to *not* wrap the callback in thread-safety
primitives (and to delegate that to the WS server) keeps
the bridge's surface area small and aligns with D-14 (the
bridge is the seam, not a thread-safety policy).

**`on_step` callback contract.** The callback signature
is `(step_index, total_steps, t_s, current_state)`. It
fires from whichever thread called `validate_procedure()` —
i.e., the worker thread inside Propose's executor, not the
caller's thread. When Validate is called directly (offline
demos, unit tests), the callback fires from the caller's
thread and no thread-safety wrapping is required. The
callback does NOT fire on the final `+1` step (the
post-horizon state is conceptually outside the sim); total
fires per `validate_procedure` call is
`2 * n_steps` (once per sim step in the baseline run, once
per sim step in the predicted run).

**Queue overflow protection.** `AppState.sim_progress_queue`
is a `queue.Queue(maxsize=2000)`. When the queue is full,
the producer callback drops the oldest event and inserts
the new one. This is defensive — the queue only fills if
the sim runs at sub-second `dt_s` with 5+ parallel sims,
which is well outside the current demo's parameters. The
drain loop in `_step_once()` always reads the queue to
completion, so a transient overflow self-heals on the next
tick.

**At most one `sim_progress` event per tick.** The
producer loop drains the queue in a `while True` loop
(keeping the most recent event) and attaches only that one
to the tick message. The 5 Hz broadcast rate and the
sub-second sim rate mean 30+ events can pile up between
ticks; we take the most recent. If the user wants to see
every event, they raise the producer tick rate (or shorten
`dt_s`); the server doesn't enforce per-event delivery.

**Tradeoff:** the WS message size grows by ~200 bytes per
`sim_progress` event. With 5 sims × 30 steps = 150 events
between ticks, the queue can hold ~30KB; the broadcast
overhead is one event per tick. Acceptable. If the
catalog grows past 10 procedures or `dt_s` drops below 1
second, the queue bounds and the at-most-one-per-tick
drain both protect the broadcast from overwhelming the
WS clients.

**Future evolution:** the at-most-one-per-tick drain
deliberately drops intermediate events. A finer throttle
("send every Nth step" or "send at most K events per
sim per tick") would let the frontend render smoother
animations of long sims. Out of scope for Phase 1. A
second future extension is per-sim progress ack: the
frontend could acknowledge receipt of progress events so
the WS server can garbage-collect the in-flight sims
early if the client disconnects. Out of scope for Phase
1; the current behavior is "fire and forget."

---

### D-18. RankedCandidate replaces the candidates_ranked tuple

> **Status (added 2026-09-04):** this decision is **new**, driven
> by the same frontend-candidates-table requirement as D-16
> and D-17. The pre-2026-09-04 `Proposal.candidates_ranked`
> was a `list[tuple[Procedure, float, ValidationResult]]` —
> a 3-tuple that lost the catalog-level editorial signal
> (D-16). The new `RankedCandidate` dataclass carries the
> signal through.

**Decision:** `Proposal.candidates_ranked: list[RankedCandidate]`
where `RankedCandidate` is a frozen-shaped dataclass with
six fields:

```python
@dataclass
class RankedCandidate:
    procedure: Procedure
    risk_score: float           # runtime, from validate_procedure
    validation: ValidationResult
    effort_score: float         # catalog-level (D-16)
    mission_impact: str         # catalog-level (D-16)
    reversibility: str          # catalog-level (D-16)
```

`Proposal.to_dict()` serializes each `RankedCandidate` to
`{procedure, risk_score, effort_score, mission_impact,
reversibility}` — a strict superset of the old
`{procedure, risk_score}` shape. The `validation` field
stays on the winning candidate's `Proposal.validation` and
is NOT included in every `candidates_ranked` entry (it's
hundreds of arrays; the runbook only needs the winner's
trajectory for the canvas plot). If the frontend ever
needs to plot every candidate's trajectory, the
`RankedCandidate.validation` field is the place to put it.

**Alternatives considered:**

- **(a) Keep the 3-tuple and add a sidecar
  `List[Dict[str, Any]]` for catalog metadata.** Hacky —
  two parallel lists that have to stay in sync. The
  pre-2026-09-04 codebase had this implicit dualism
  (the 3-tuple and the registry lookup the bridge did
  to enrich the WS payload) and it produced the
  `alerts_to_symptom_events` legacy hardcode that
  §3.3 defers.
- **(b) Promote to a dict instead of a dataclass.** A
  dict would work but loses the type hints and the
  `ranked.procedure` ergonomics. The dataclass is a
  dict-with-types; no real downside.
- **(c) Dataclass with all six fields (chosen).** Type-safe,
  ergonomic (`rc.procedure` not `rc["procedure"]`), carries
  enough metadata for the frontend to render the candidates
  table without re-reading the registry, and small enough
  (~6 fields, all primitives) that the WS payload doesn't
  bloat.

**Why we chose what we chose:** the tuple was a leaky
abstraction. Every consumer that needed catalog metadata
(the bridge when serializing to WS, the demo when printing
the candidates table, the test suite when asserting
catalog fields) had to re-look-up the procedure in the
registry. That re-lookup is what made D-16 impossible to
implement without a shape change — the candidates table
needed the catalog fields on every row, and the tuple
didn't have them. The dataclass is the smallest shape
change that supports D-16 and the frontend.

**Tradeoff:** `RankedCandidate` is a public dataclass
exported from `twin.propose`. Any future change to its
fields is a BIBLE-significant contract change. The
`validation` field in particular is the heaviest field
by far (hundreds of arrays per entry); the to_dict()
output deliberately drops it from the broadcast. If a
future Phase needs to broadcast every candidate's
trajectory, the `validation` field is the carrier and
the WS payload size will grow accordingly.

**Migration path for any external consumer.** The
pre-2026-09-04 `candidates_ranked` was a list of 3-tuples;
the new shape is a list of dataclasses. The change is
*not* API-compatible at the Python level. The integration
test (`live/tests/test_injection_bridge.py`) and the
demo scripts were updated in the same commit. The WS
contract change is *additive* at the JSON level (new
keys per entry) so any WS consumer that ignores unknown
keys keeps working; consumers that destructure the entry
need to update.

**Future evolution:** if the runbook needs every
candidate's full trajectory (Phase 3?), the
`RankedCandidate.validation` field is the carrier and
the WS payload size budget becomes the constraint.
Likely resolution: add a `RankedCandidate.summary` field
with a coarse one-line outcome (e.g., "SoC ends at 0.42,
no violations") and have the WS broadcast that instead
of the full `validation` block.

### D-19. Propose ranks by multi-axis lexicographic key, not by risk_score

> **Status (added 2026-09-05):** this decision is **new**,
> driven by the BIBLE §3.2 / §8 / §9 runbook design: the
> catalog-level editorial fields (`effort_score`,
> `mission_impact`, `reversibility`) that the operator sees
> in the runbook are *the same logic* that picked the
> winner, not just descriptive labels. The pre-2026-09-05
> `propose()` ranked by `risk_score` alone; the catalog
> fields were surfaced but not consulted.

**Decision:** `propose()` ranks `candidates_ranked` by the
**multi-axis lexicographic key**
`(mission_impact_score, effort_score, reversibility_score,
risk_score)` ascending. Lower is better. The catalog
fields are the primary decision axis; runtime
`risk_score` is the final tie-breaker, only consulted when
two procedures are tied on impact / effort / reversibility.

The numeric score tables are the same as the pre-D-19
`_coarse_rank_key` (the pre-filter that kept the top-K
before the expensive twin sims ran); the runtime sort
adds `risk_score` as the 4th element so the winner is
determined by the catalog fields *and* the runtime risk
in a single comparison. The numeric mappings are:

```python
_MISSION_IMPACT_SCORE = {"none": 0.0, "minor": 0.2,
                         "major": 0.5, "mission-ending": 1.0}
_REVERSIBILITY_SCORE  = {"trivial": 0.0, "easy": 0.1,
                         "hard": 0.4}
# effort_score is already in [0, 1] from the registry.
```

**Why multi-axis and not weighted-sum.** Three reasons:

1. **Operator auditability.** The runbook shows the
   per-axis comparison (`winner.factors.rank_key_comparison`)
   with a `deciding: true` marker on the axis that
   picked the winner. A weighted sum is opaque — the
   operator would have to compute it from a published
   weights table to reproduce the choice. Lexicographic
   ordering has a single tie-break rule ("the first axis
   that differs decides") and the deciding axis is
   visible in the runbook.

2. **Defensive against silent regressions.** If a future
   code change accidentally inverts a catalog field's
   numeric score (e.g. swaps `trivial` ↔ `hard` in the
   mapping), the lexicographic structure means the
   regression is visible — the winner's deciding axis
   flips immediately, and the test
   `test_propose_ranks_by_multi_axis_key` (which patches
   `validate_procedure` to force specific risk_scores)
   catches it. A weighted sum with a 0.5 risk / 0.5
   catalog split could mask the regression for several
   test cases.

3. **Matches D-12's "future evolution" line.** D-12 said:
   *"the validation-based ranking is the final arbiter,
   but the catalog fields are surfaced so a future
   policy engine can weight them."* D-19 is the moment
   we let the catalog fields *be* the policy — but with
   the lexicographic structure (not a sum) so the
   weighting is auditable, not buried in a coefficient
   table.

**Why not a learned ranker / LLM ranker.** D-12 already
argued this for the "validation vs. catalog" question:
the runtime `risk_score` is a deterministic twin-computed
value, and any ranker that doesn't use it would be making
worse decisions. Multi-axis lexicographic is a
deterministic policy on top of the existing
risk_score; it doesn't replace D-12, it extends it.

**Alternatives considered and rejected:**

- **Weighted sum** (e.g. `0.5*risk + 0.2*effort +
  0.2*mission_impact + 0.1*reversibility`). Rejected for
  reason 1 above (opaque) and reason 2 (silently masks
  catalog regressions).
- **Pareto-front** (find the set of non-dominated
  candidates, ask the operator to pick). Rejected: the
  operator already approved D-19's lex-key via the BIBLE;
  adding a manual-pick step to the Phase 1 demo loop
  breaks the "show me everything in one screen" promise
  of `slight_shift_demo.py`.
- **Reverse order: risk first, catalog as tie-breaker**
  (the pre-D-19 behavior extended with a tie-breaker).
  Rejected: makes `risk_score` the primary axis and
  catalog fields the secondary, which inverts the
  operator's auditability story (the runbook would have
  to explain "we ignored the catalog because risk was
  close enough", which is much harder to defend than
  "the catalog decided").

**Migration path for any external consumer.** The
ordering of `candidates_ranked` and the choice of
`procedure` may differ for any cause where the catalog
fields and `risk_score` disagree. The change is *not*
order-preserving on the previous `risk_score`-only sort.
For Phase 1 there are no external consumers; the change
is the same-shape extension as D-18 (additive fields on
`Proposal`, plus a new `winner_rank_key` field).

**Future evolution:** the `_rank_key` tuple is a
4-tuple today. If the BIBLE §7 policy engine adds a 5th
axis (e.g. operator's pre-set "preferred class" for
recurring faults), the tuple grows and the lexicographic
ordering naturally extends. The numeric score tables
would move from `twin/propose.py` constants to a
`PolicyConfig` dataclass (Phase 2) so the values are
auditable from the runbook.

---

---

## 5. The hand-authored knowledge catalogs

The cause and procedure catalogs are the *application knowledge* of
telos — the "what failure modes exist and what to do about them"
that an ops center would normally keep in a Fault Management (FM)
document. In telos they live in a single Python module:
`digital-twin/twin/procedures.py` (712 lines). See [D-10](#d-10-the-cause-catalog-and-the-twin-procedure-registry-are-the-same-file)
for why they're in one file rather than a separate YAML or JSON
catalog.

### Why one file, not three

The catalog has three parts:

1. The `Cause` enum — what the diagnostic stage is allowed to
   diagnose.
2. The `Procedure` enum — what the diagnostic stage is allowed to
   recommend.
3. The `ProcedureSpec` registry — for each procedure, the parameter
   schema, the preconditions, the postconditions, the `apply_fn`
   that mutates twin state, the `risk_class`, the
   `approval_required` field, and the `default_params` Propose
   uses for validation.

All three live in `procedures.py`. There is no separate YAML or
JSON file; the catalog *is* the code. The drift-prevention
rationale is in D-10: putting the catalog and the twin in one
file means they cannot lie about each other, because they
come from the same import. The cost is that editing the
catalog is editing code (it goes through the same review
as code). The benefit is that the catalog cannot drift from
what the twin can actually do.

### Why data-shaped, even though it's Python

Even though the catalog is a Python module, it's *shaped* like
data: enums with `affected_subsystems()` and
`expected_channels()` methods, a flat `dict[Procedure,
ProcedureSpec]` registry, no inheritance, no abstract base
classes. The Diagnose and Propose stages reason about the
catalog as if it were a table; the only difference from the
YAML version is that the schema validation happens at import
time (a `ProcedureSpec.validate_params(params)` call) instead
of at load time (a YAML loader). This is the
"data-shaped-but-still-Python" pattern that lets the catalog
evolve without the rest of the system needing to know.

### `Cause` enum shape

```python
class Cause(str, Enum):
    """Diagnoses the diagnostic agent can emit. Each name matches a fault_type."""
    EPS_INTERNAL_R_DEGRADATION = "eps_internal_r_ramp"
    EPS_LOAD_EXCESS = "eps_load_step"
    BATTERY_OVERDISCHARGE = "battery_overdischarge"
    BATTERY_UNDERVOLTAGE = "battery_undervoltage"
    THERMAL_HEATER_STUCK_OFF = "thermal_heater_stuck_off"
    THERMAL_HEATER_STUCK_ON = "thermal_heater_stuck_on"
    THERMAL_RUNAWAY = "thermal_runaway"
    ADCS_STAR_TRACKER_LOST = "adcs_star_tracker_lost"
    WHEEL_SATURATION = "wheel_saturation"
    SOLAR_DEGRADATION = "solar_degradation"
    COMM_GROUND_STATION_LOST = "comm_ground_station_lost"
    SENSOR_NOISE = "sensor_noise"
    NO_FAULT_DETECTED = "no_fault_detected"

    def affected_subsystems(self) -> List[str]:
        """Which subsystems the diagnostic agent should expect to see in
        the symptom stream when this cause is active. Used by the
        diagnostic layer to cross-check the channel-level evidence."""
        # Full mapping in digital-twin/twin/procedures.py:97-112
        ...

    def expected_channels(self) -> List[str]:
        """Channel IDs (Telemanom convention) likely to show anomalies
        when this cause is active. Diagnostic agent should look for these
        in the Telemanom E_seq output to confirm or rule out the cause."""
        # Full mapping in digital-twin/twin/procedures.py:114-133
        ...
```

13 values total — 12 diagnosable faults + 1 sentinel
`no_fault_detected`. The `affected_subsystems()` and
`expected_channels()` methods are the per-cause metadata the
Diagnose stage scores against. The full mapping tables are in
`digital-twin/twin/procedures.py:97-133` and reproduced in §6.2.

### `Procedure` enum shape

```python
class Procedure(str, Enum):
    """Actions the diagnostic agent can recommend."""
    EPS_SHED_LOAD = "eps_shed_non_essential_load"
    EPS_PRIORITIZE_CHARGING = "eps_increase_charging_priority"
    THERMAL_ENABLE_BACKUP_HEATER = "thermal_enable_heater_backup"
    THERMAL_THROTTLE_PAYLOAD = "thermal_throttle_payload"
    ADCS_SAFE_HOLD = "adcs_switch_to_safe_hold"
    ADCS_RESET_STAR_TRACKER = "adcs_reset_star_tracker"
    COMMS_POSTPONE_DOWNLINK = "comms_postpone_downlink"
    MODE_CHANGE_TO_SAFE = "mode_change_to_safe"
    WAIT = "wait"
```

9 values. Names are independent of cause names — a procedure
can be a candidate for multiple causes (e.g.,
`EPS_SHED_LOAD` is a candidate for 4 different EPS causes).
The cause→procedure map is the source of truth in
`get_candidate_procedures(cause)` (§6.4).

### `ProcedureSpec` shape

Each entry in the `PROCEDURE_REGISTRY` is a `ProcedureSpec`:

```python
@dataclass
class ProcedureSpec:
    name: Procedure
    description: str
    params_schema: Dict[str, ParamSpec]   # type, min, max, allowed_values, unit
    preconditions: List[Callable[[dict], bool]]   # state -> bool
    postconditions: List[Callable[[dict, dict], bool]]  # (pred, actual) -> bool
    apply_fn: Optional[Callable[[dict, dict], None]]   # mutates state in place
    risk_class: str = "low"               # "low" | "medium" | "high" | "critical"
    approval_required: str = "operator"   # "auto" | "operator" | "director"
    default_params: Optional[Dict[str, Any]] = None
    # -- D-16 catalog-level editorial fields (hand-authored, PR-reviewed) --
    effort_score: float = 0.5             # 0..1, lower = less operator/spacecraft work
    mission_impact: str = "minor"         # "none" | "minor" | "major" | "mission-ending"
    reversibility: str = "easy"           # "trivial" | "easy" | "hard"
```

The `ParamSpec` for each parameter is a frozen dataclass:

```python
@dataclass(frozen=True)
class ParamSpec:
    type: type                # int | float | str | bool
    min: Optional[float] = None
    max: Optional[float] = None
    allowed_values: Optional[tuple] = None
    unit: Optional[str] = None
    required: bool = True
    description: str = ""
```

`validate_params(params)` raises `ValueError` on first invalid
parameter (wrong type, out of bounds, missing required, or
unknown name). The 9 `apply_*` functions in `procedures.py`
are the *one* place twin state is mutated for a procedure
(per D-10); `apply_procedure(state, procedure, params)` is
the public entry point that calls them.

A worked example — the `EPS_SHED_LOAD` entry:

```python
Procedure.EPS_SHED_LOAD: ProcedureSpec(
    name=Procedure.EPS_SHED_LOAD,
    description=(
        "Reduce total spacecraft load by shedding non-essential consumers. "
        "Frees power for thermal survival and battery recovery. "
        "Use for: battery undervoltage, internal_r degradation, load excess."
    ),
    params_schema={
        "load_reduction_a": ParamSpec(
            float, min=0.1, max=5.0, unit="A", required=True,
            description="Amps to shed from the load bus. 0.1-5.0A typical for smallsat.",
        ),
        "duration_s": ParamSpec(
            float, min=60, max=14400, unit="s", required=True,
            description="How long the load reduction stays in effect.",
        ),
    },
    preconditions=[
        lambda s: s.get("operating_mode") != 1,  # don't override safe mode
    ],
    postconditions=[
        # Actual battery_soc should be ≥ predicted_soc - 1% (recovery happened
        # or stayed flat; we don't expect it to get worse)
        lambda s_pred, s_actual: s_actual["battery_soc"] >= s_pred["battery_soc"] - 0.01,
    ],
    apply_fn=_apply_eps_shed_load,
    risk_class="low",
    approval_required="operator",
    default_params={"load_reduction_a": 1.0, "duration_s": 3600.0},
    # D-16 catalog-level editorial fields
    effort_score=0.30,
    mission_impact="minor",
    reversibility="easy",
),
```

The other 8 entries follow the same shape. The full
registry starts at `digital-twin/twin/procedures.py:389`.

### Module invariants (import-time validation)

The catalog validates itself when `procedures.py` is imported:

- **`Cause.affected_subsystems()` and `expected_channels()` are
  complete.** Every Cause has both methods; `NO_FAULT_DETECTED`
  has empty lists, all others have non-empty lists.
- **`PROCEDURE_REGISTRY` covers every `Procedure` value.** The
  `get_default_params(procedure)` accessor raises `KeyError`
  if a procedure is missing from the registry; this is caught
  at import time by the registry's `Dict[Procedure,
  ProcedureSpec]` type hint.
- **`default_params` validates against `params_schema`.** Every
  entry's `default_params` is checked at import time to
  satisfy the spec (the `apply_procedure` function calls
  `validate_params` before calling `apply_fn`).
- **`apply_fn` is non-null for every entry except
  `Procedure.WAIT` and `Procedure.ADCS_SAFE_HOLD`** (which
  have explicit no-op apply functions for the lifecycle
  symmetry). The `apply_procedure` function handles
  `apply_fn=None` defensively.
- **D-16 catalog-level editorial fields are valid.** Every
  entry's `mission_impact` is in the allowed set
  `("none", "minor", "major", "mission-ending")`, every
  `reversibility` is in `("trivial", "easy", "hard")`, and
  every `effort_score` is in `[0.0, 1.0]`. Violations raise
  `ValueError` at import time. The constant
  `VALID_MISSION_IMPACTS` and `VALID_REVERSIBILITY` are
  exported so the test suite can import the allowed sets
  without re-declaring them.

### Versioning

The catalog's "version" is the git commit hash of
`procedures.py`. A runbook is replayable only against the
catalog versions it was generated with, unless the catalog
explicitly declares backward-compatibility. The Phase 3
runbook schema (§8) will log
`twin_simulation_digest = sha256(twin.procedures.__file__)` and
`procedures_sha256 = sha256(canonical_json(registry))` in
`input_evidence`.

### Why not a separate YAML / JSON catalog

The pre-merge BIBLE had the catalog as a YAML file with a typed
loader. That was replaced by the single-Python-module design
(D-10) when the twin landed. The reasons are in D-10; the
short version is: the catalog and the twin are coupled by
definition (the catalog says "procedure X is available for
cause Y," the twin says "procedure X does Z to state S"),
and putting them in separate files lets them drift in
ways that produce silently wrong diagnoses. One file
makes drift *structurally impossible*.

If the catalog grows past ~50 causes, splitting *back* into
a generated catalog (code-generated from a typed schema)
becomes worth the build step. Until then, single module is
the right call.

---

## 6. The digital twin

The twin is the deterministic sidecar simulator that the **Validate**
stage runs proposed procedures through before they touch anything real.
It is also the source of truth for the cause catalog and the procedure
catalog — both live in `mission_ops/twin/procedures.py` per
[D-10](#d-10-the-cause-catalog-and-the-twin-procedure-registry-are-the-same-file).

### 6.1 Scope (as landed)

The twin is a lumped, deterministic state machine implemented in
`digital-twin/twin/` — **~3,012 lines** across 12 Python modules
(was 2,791 lines before the 2026-09-04 D-16/D-17/D-18 additions;
see §3.1's file inventory table for the per-file deltas).
The catalog (cause enum, procedure enum, `ProcedureSpec`
registry, and the 9 `apply_*` functions) lives in the same
file per D-10: `digital-twin/twin/procedures.py` (712 lines).

**Subsystems (5):** EPS, Battery, Thermal, ADCS, Comms. B-1
is shared between Battery and Thermal (the battery temperature
is part of both subsystems' state).

**Channels (8):** P-1, P-2, B-1, T-1, T-2, A-1, G-1, D-1.
See §13 Glossary for the per-channel definitions (units,
nominal ranges).

**Causes (13):** see §6.2.

**Procedures (9):** see §6.3.

**Physics (lumped, deterministic):**

- **EPS** (`twin/eps.py`, 196 lines): terminal voltage + SoC
  bookkeeping + temperature-coupled internal resistance
  (PASEOS-derived equations; the math is used, not the
  vendored source, to avoid the GPL license). The
  `step_eps(state, dt_s, in_eclipse, sun_angle_deg, params)`
  function mutates the EPS fields in place.
- **Battery:** shares B-1 with Thermal. The SoC and
  terminal voltage are part of EPS; the temperature is
  part of Thermal.
- **Thermal** (`twin/thermal.py`, 204 lines): 4-node RC
  network (battery, payload, electronics, radiator) with
  PASEOS solar + albedo + Earth-IR + Stefan-Boltzmann
  emission per node. `step_thermal(state, dt_s, ...)`
  mutates the 4 temperature fields and the 3 heater-duty
  fields.
- **ADCS** (`twin/adcs.py`, 134 lines): 2nd-order damped
  pointing + reaction wheel dynamics with saturation.
  `step_adcs(state, dt_s, params)` mutates
  `pointing_error_deg`, `wheel_speed_rpm`, and
  `attitude_mode`.
- **Comms:** derived link margin — no separate simulation
  step. The `link_margin_db` is a function of
  `pointing_error_deg` (computed by `step_adcs`) and
  `comms_enabled` (a procedure flag).
- **Orbit:** simplified. A 35-minute orbit with 40%
  eclipse fraction is hard-coded in `validate.py:_run_forward`
  for the validation loop. The real CHESS-based orbit
  with NRLMSISE-00 atmosphere is in the vendored
  `digital_twin_CubeSat` reference but is not used by
  the Phase 1 pipeline.

**File inventory** (per-file line counts from
`wc -l digital-twin/twin/*.py`):

| File | Lines | Role |
|---|---|---|
| `state.py` | 199 | `make_default_state()` factory + the state-dict shape |
| `eps.py` | 196 | `step_eps()` — terminal voltage + SoC bookkeeping |
| `thermal.py` | 204 | `step_thermal()` — 4-node RC network |
| `adcs.py` | 134 | `step_adcs()` — pointing + wheel dynamics |
| `fault_injection.py` | 265 | `FaultScheduler` — 9 fault types + per-step `apply()` |
| `channel_shaper.py` | 102 | Telemanom-shaped `.npy` per channel |
| `procedures.py` | 712 | **Catalog + twin execution** (D-10) |
| `validate.py` | 306 | `validate_procedure()` + two-trajectory forward loop |
| `propose.py` | 154 | `propose()` + validation-based ranking |
| `diagnose.py` | 152 | `diagnose()` + scoring rubric |
| `verdict.py` | 136 | `to_verdict()` + BIBLE-shaped `Verdict` |
| `run_sim.py` | 231 | CLI entry: `python -m twin.run_sim` |
| **Total** | **2,791** | |

**License:** telos's own code is MIT (per the repo root
`LICENSE`). The vendored references
(`digital-twin/digital_twin_CubeSat/` is CHESS, with its
own license; `digital-twin/paseos/` is PASEOS, with its
own license; `digital-twin/telemanom/` is the Hundman
et al. reference) are kept with their original licenses
intact. The PASEOS math is used (not the source) where
it's referenced in `twin/eps.py` and `twin/thermal.py`,
to avoid the GPL contamination that vendoring the source
would introduce.

### 6.2 The 13 causes (as landed)

The `Cause` enum in
`digital-twin/twin/procedures.py:67-133`. Each cause name
matches a `fault_type` string in
`twin/fault_injection.py:FaultScheduler` so the runbook
can verify "we suspected cause X, we injected it for
real, the recovery worked or didn't." The
`affected_subsystems()` and `expected_channels()` methods
on the enum are the per-cause metadata the Diagnose stage
scores against.

| # | Cause | Subsystem(s) | Channels (per `expected_channels()`) | What the Diagnose stage actually scores |
|---|---|---|---|---|
| 1 | `eps_internal_r_ramp` | EPS, Thermal | P-1, B-1 | P-1 voltage sag **plus** B-1 temperature rise (the temperature coupling is the discriminator from `eps_load_step` and `battery_undervoltage`) |
| 2 | `eps_load_step` | EPS | P-1 | P-1 SoC falling faster than eclipse predicts, but voltage stable — pure load, not source problem |
| 3 | `battery_overdischarge` | EPS | P-1 | P-1 voltage at floor, SoC near 0 |
| 4 | `battery_undervoltage` | EPS | P-1 | P-1 voltage below safe threshold, SoC nonzero |
| 5 | `thermal_heater_stuck_off` | Thermal | B-1, T-1, T-2 | All three thermal channels drift cold (below setpoint) |
| 6 | `thermal_heater_stuck_on` | Thermal | B-1, T-1, T-2 | All three thermal channels drift hot (above setpoint) |
| 7 | `thermal_runaway` | Thermal, EPS | B-1, T-1, T-2, P-1 | Temperatures rising **faster** than setpoint can track; P-1 affected because battery temp raises internal_r |
| 8 | `adcs_star_tracker_lost` | ADCS, Comms | A-1, G-1, D-1 | Pointing error drifts AND wheel speeds rise AND link margin falls (the cascade) |
| 9 | `wheel_saturation` | ADCS | G-1, A-1 | Wheel speeds pinned at saturation, pointing error oscillating around bound |
| 10 | `solar_degradation` | EPS | P-2 | P-2 current drops in sunlit periods (eclipse-aware) |
| 11 | `comm_ground_station_lost` | Comms | D-1 | D-1 link margin drops to 0 with **no pointing change** (the discriminator from `adcs_star_tracker_lost`) |
| 12 | `sensor_noise` | any | any | High-frequency oscillation around the mean; no drift; no cross-channel coupling |
| 13 | `no_fault_detected` | — | — | All channels within normal bounds. Sentinel cause. |

The "What the Diagnose stage actually scores" column is
the operational discriminator — when three thermal causes
all show a `shift` on T-1, they tie at the same score
and stable sort picks the first (currently
`thermal_heater_stuck_off`); when the symptom window
includes both T-1 and B-1 with cross-subsystem coupling
signals, `thermal_runaway` wins. The Diagnose stage does
not know how the events were generated; the scores are
purely a function of the symptom window and the cause's
declared `expected_channels()` / `affected_subsystems()`.

### 6.3 The 9 procedures (as landed)

The `Procedure` enum in
`digital-twin/twin/procedures.py:159-181`. Each entry in
the `PROCEDURE_REGISTRY: Dict[Procedure, ProcedureSpec]`
(registry starts at `procedures.py:389`) has a typed
parameter schema, preconditions, postconditions, an
`apply_fn`, a `risk_class`, and an `approval_required`
field.

| # | Procedure | Risk | Approval | Required params (with bounds) | Default params (Propose uses) | Effort | Mission impact | Reversibility |
|---|---|---|---|---|---|---|---|---|
| 1 | `eps_shed_non_essential_load` | low | operator | `load_reduction_a: float [0.1, 5.0] A`, `duration_s: float [60, 14400] s` | `{1.0, 3600.0}` | 0.30 | minor | easy |
| 2 | `eps_increase_charging_priority` | low | operator | `load_reduction_a: float [0.1, 5.0] A`, `duration_s: float [60, 14400] s`, `solar_input_multiplier: float [0.5, 1.0]` (optional) | `{1.0, 3600.0, 1.0}` | 0.35 | minor | easy |
| 3 | `thermal_enable_heater_backup` | low | operator | `node: str ∈ {battery, payload, electronics}` | `{node: battery}` | 0.15 | none | trivial |
| 4 | `thermal_throttle_payload` | medium | operator | `payload_power_w: float [0.0, 15.0] W`, `duration_s: float [60, 14400] s` | `{0.0, 3600.0}` | 0.40 | major | easy |
| 5 | `adcs_switch_to_safe_hold` | medium | operator | (no params) | `{}` | 0.60 | major | hard |
| 6 | `adcs_reset_star_tracker` | medium | operator | `hold_off_s: float [5, 300] s` | `{30.0}` | 0.45 | minor | easy |
| 7 | `comms_postpone_downlink` | low | **auto** | `postpone_s: float [300, 86400] s` | `{3600.0}` | 0.10 | minor | trivial |
| 8 | `mode_change_to_safe` | **critical** | **director** | (no params) | `{}` | 0.85 | mission-ending | hard |
| 9 | `wait` | low | **auto** | `duration_s: float [60, 3600] s` | `{600.0}` | 0.05 | none | trivial |

**Important — the `risk_class` strings here are not the
A/B/C/D scheme in §7.** The Phase 1 catalog uses
`low / medium / high / critical` strings. The A/B/C/D
mapping is Phase 2 work (see §3.3 deferred polish items
and the `risk_class` mapping note). The
`approval_required` field is the owner-supplied 3-tier
scheme (`auto / operator / director`); the A/B/C/D
scheme is a 4-tier refinement of the `operator` tier
(see §7 for the planned mapping).

**The three new columns — Effort, Mission impact, Reversibility
— are the catalog-level editorial fields added in D-16.**
`effort_score` is a `float` in [0.0, 1.0] (lower is less
operator/spacecraft work). `mission_impact` is a categorical
from the allowed set `("none", "minor", "major",
"mission-ending")`. `reversibility` is a categorical from
`("trivial", "easy", "hard")`. All three are STATIC catalog
values, hand-authored per procedure and reviewed in the same
PR as the procedure's `apply_fn`. They are distinct from
`risk_class` (which is "how bad if this procedure fails") and
`approval_required` (which is org-policy routing). The runtime
`risk_score` from Validate is the *measured* outcome; the
catalog fields are the *expected* characterization. When the
two disagree, that is interesting and worth surfacing in the
runbook.

These three fields serve three purposes:

1. **Propose pre-filter** (D-16, see §2 Stage 3) — `propose()`
   trims the candidate set to `PROPOSE_TOP_K = 5` by sorting
   on `(effort_score, mission_impact_score, reversibility_score)`
   before paying the twin simulation cost.
2. **Frontend candidates table** (D-18) — the
   `candidates_ranked` list in the `Proposal` carries these
   fields per candidate, so the runbook's "Why this procedure"
   section can render a sortable table showing "we picked the
   low-effort, easy-to-reverse procedure over the
   mission-ending one even though both have a low `risk_score`."
3. **Audit trail** — when the BIBLE §3 runbook schema lands
   (Phase 3), these fields are part of the runbook JSON
   alongside the runtime `risk_score`, so an auditor can
   reconstruct the system's reasoning.

The import-time validation in `procedures.py` rejects any
registry entry with a `mission_impact` or `reversibility` value
not in the allowed set, or an `effort_score` outside `[0.0,
1.0]`. The `test_every_procedure_has_effort_score_in_unit_range`,
`test_every_procedure_has_valid_mission_impact`, and
`test_every_procedure_has_valid_reversibility` tests in
`digital-twin/twin/tests/test_procedures_defaults.py` guard
against future relaxations.

### 6.4 The cause → candidate procedure mapping (as landed)

The `get_candidate_procedures(cause)` function in
`digital-twin/twin/procedures.py` (the function is defined
near the end of the file) returns the procedures Propose
should consider for each cause. Propose ranks them by
validation outcome — the twin is the ranking function (D-12).

| Cause | Candidate procedures |
|---|---|
| `eps_internal_r_ramp` | `eps_shed_non_essential_load`, `eps_increase_charging_priority`, `mode_change_to_safe` |
| `eps_load_step` | `eps_shed_non_essential_load`, `mode_change_to_safe` |
| `battery_overdischarge` | `eps_shed_non_essential_load`, `mode_change_to_safe` |
| `battery_undervoltage` | `eps_shed_non_essential_load`, `wait` |
| `thermal_heater_stuck_off` | `thermal_enable_heater_backup`, `thermal_throttle_payload` |
| `thermal_heater_stuck_on` | `thermal_throttle_payload` |
| `thermal_runaway` | `thermal_throttle_payload`, `mode_change_to_safe` |
| `adcs_star_tracker_lost` | `adcs_switch_to_safe_hold`, `adcs_reset_star_tracker` |
| `wheel_saturation` | `adcs_switch_to_safe_hold` |
| `solar_degradation` | `eps_increase_charging_priority`, `mode_change_to_safe` |
| `comm_ground_station_lost` | `comms_postpone_downlink` |
| `sensor_noise` | `wait` |
| `no_fault_detected` | `wait` |

The map is the *expert-defined priority* — when two
procedures have identical `risk_score` from Validate, the
one earlier in this list wins (stable sort).

### 6.5 The Validate contract — the two-trajectory model

Validate is the bridge between "what the catalog says a
procedure does" and "what the twin says a procedure does
to *this* state." The contract is a **two-trajectory
forward projection**: every `validate_procedure()` call
runs the twin forward twice from the same starting state
— once with the procedure applied, once without — and
returns both trajectories for direct comparison.

The function lives in
`digital-twin/twin/validate.py:195-306` and returns a
`ValidationResult` with:

- `predicted_trajectory: dict[str, np.ndarray]` — the
  procedure applied for its full `duration_s`. Each array
  has `horizon_s / dt_s + 1` entries (default 14400s / 60s
  = 241 entries for a 4-hour validation).
- `baseline_trajectory: dict[str, np.ndarray]` — the
  no-action baseline over the same horizon. Same array
  shape; used as the comparison reference.
- `feasible: bool` — `True` if no constraint was violated
  at any timestep.
- `violations: list[dict]` — first violation per
  constraint field (6 fields checked: `battery_soc`,
  `battery_voltage_v`, `battery_temp_c`,
  `electronics_temp_c`, `payload_temp_c`,
  `pointing_error_deg`).
- `risk_score: float` in [0, 1] — `0.5 * violation_rate +
  0.5 * min(1.0, soc_diff * 2.0)` where `soc_diff` is the
  procedure's SoC impact relative to baseline.
- `summary: str` — one-line human-readable roll-up
  (e.g., `"Procedure eps_shed_non_essential_load: SoC
  baseline=0.385 predicted=0.745 delta=+0.360,
  violations=0, risk=0.50"`).
- `postcondition_check: Optional[bool]` — the
  `ProcedureSpec.check_postconditions(predicted, actual)`
  call's result, where `predicted` is the baseline's
  terminal state and `actual` is the predicted's terminal
  state. Used by Phase 3 to assert "did the procedure
  achieve its postcondition?"

The trajectories share keys (`battery_soc`,
`battery_voltage_v`, `battery_temp_c`, `payload_temp_c`,
`electronics_temp_c`, `radiator_temp_c`,
`pointing_error_deg`, `wheel_speed_rpm`, etc.) and the
same array shape, which is what the
`phase1_demo_deep.py` and `slight_shift_demo.py` scripts
exploit to print the side-by-side `pred <field>` vs
`base <field>` columns. The integration test asserts
that both trajectories have the same shape so consumers
can plot them together.

**The Verdict wrapper** (`twin/verdict.py`) maps
`ValidationResult` to the BIBLE-shaped `Verdict` with
status `OK | REJECT | INCONCLUSIVE` and the decision
order: `REJECT` if `!feasible` or there are constraint
violations; `INCONCLUSIVE` if `feasible` but
`risk_score >= 0.3`; `OK` otherwise. The `0.3` threshold
is `RISK_THRESHOLD_INCONCLUSIVE`, a tunable constant at
the top of `twin/verdict.py`. See §2 Stage 4 for the
full mapping.

**Why two trajectories, not one.** The two-trajectory
model is what makes the trajectory table in
`phase1_demo_deep.py` and `slight_shift_demo.py` show a
*delta*. A single predicted trajectory tells the operator
"here is where the spacecraft will be" but not "compared
to what would have happened without the procedure."
Comparing the two is what makes the verdict honest — a
procedure that just keeps the spacecraft at the same
state as the baseline is `INCONCLUSIVE` (it didn't help,
but it didn't hurt); a procedure that makes things worse
than the baseline is `REJECT` even if it doesn't violate
a hard constraint.

### Twin invariants (as landed)

- **Deterministic.** Same `starting_state` + same
  `procedure` + same `params` → byte-identical
  `ValidationResult`. There is no wall-clock, no random
  input, no I/O in the step functions. The
  `_run_forward` helper in `validate.py` is the only
  function that calls `step_eps`, `step_thermal`, and
  `step_adcs`; it's a deterministic loop over the
  horizon. This invariant is what makes the Phase 3
  Merkle chain work.
- **In-process.** The twin runs in the same Python
  interpreter as `live/ws_server.py` (D-13). The lazy
  imports in `live/twin_bridge.py` keep the CHESS
  astropy chain out of the live server's boot path.
- **One-way dependency on `live/`.** The twin never
  imports from `live/`. The bridge is the only seam
  (D-14); twin code is self-contained and can be
  unit-tested in isolation.
- **Pure step vocabulary.** Every procedure's `apply_fn`
  is a pure function of the state dict. The Propose
  stage cannot invent `action` values outside this
  vocabulary — the `PROCEDURE_REGISTRY` is the only
  way to apply a procedure to a state.
- **Content-addressed state.** Every twin state is
  serializable (it's a flat dict of floats / bools /
  strings / tuples; the `to_arrays` helper in
  `validate.py:248-271` converts to numpy arrays for the
  trajectory output). The Phase 3 runbook will
  content-address the trajectory by SHA-256 and attach
  it to the `Verdict.twin_simulation_digest` (currently
  `""` in Phase 1).

---

## 7. Trust and approval

### The non-negotiable: humans approve, LLMs propose

The LLM (when one is added in Phase 4) is **structurally barred from
issuing an `ApprovalToken`.** The approval gate is not in the LLM's
tool surface. This is enforced by code, not by prompt.

### The 4 risk classes

| Class | Description | Examples | Approval route | Dry-run | Hold-down | 2-person |
|---|---|---|---|---|---|---|
| **A** | Read-only / informational | dump telemetry, run diagnostics | Auto, log only | No | No | No |
| **B** | Reversible within minutes | power-cycle non-critical payload, rerun cal | Single approval (async queue OK) | No | No | No |
| **C** | Reversible but expensive | safing a payload, mode change | Single approval + 60s mandatory review of dry-run | Yes | No | No |
| **D** | Irreversible or safety-affecting | thruster firings, antenna deploy, propellant | Twin verdict + dry-run + 30s hold-down + 2-person | Yes | Yes (30s) | Yes |

### The policy engine (Phase 2)

The `ApprovalGate` is an OPA/Rego gate. The Rego rules
(`mission_ops/policy/risk.rego`) produce `{allow, required_approvals,
hold_down_seconds, dry_run_required}` from the `Proposal` + `Verdict`
+ `OperatorContext`.

If OPA is unavailable, a hand-coded Python fallback in
`mission_ops/policy/gate.py` is used; a test asserts the fallback
matches the Rego rules on a fixed fixture.

**The LLM has no Python path to the gate.** The gate is not in the
LLM's tool surface. This is enforced structurally, not by prompt.

### Class D: 2-person rule and hold-down timer

Class D requires two distinct operator signatures (separation of
duties: executor of record ≠ approver of record). After the second
signature, a 30-second hold-down timer starts. During the hold-down,
any operator can press "Panic Abort" to void the token. After the
timer expires without abort, the token is `READY_FOR_EXECUTE`.

The Executor (Phase 2) refuses tokens whose:
- `hold_down_expires_at` is in the future
- `signatures` count is below `required_approvals`
- two signatures share the same `operator_id`
- `procedure.id` does not match the procedure being executed

### The dry-run preview

For Class C and Class D, the Approver UI renders a dry-run preview:
- current twin state
- predicted state after each step
- predicted subsystem impacts
- the abort path (which commands can be issued mid-procedure to roll back)

The "Approve" button is disabled until the operator has scrolled past
every step. This is enforced in the UI, not the agent.

---

## 8. Audit and runbooks

### Runbook schema (Phase 3)

The full runbook JSON schema is defined in Phase 3. The key fields
are:

- `runbook_id` (sha256-derived)
- `risk_class`
- `mission_id`
- `operator`, `approvers[]`
- `input_evidence` (telemetry_sha256, twin_state_sha256,
  dag_version, model_version, procedure_catalog_version)
- `diagnosis` (symptoms, candidate_causes with scores and propagation
  paths, selected_cause_id, llm_narrative, llm_citations,
  model_version)
- `procedure` (id, risk_class, steps, twin_verdict,
  twin_simulation_sha256)
- `approval` (gate, rego_bundle_sha256, required_approvals,
  hold_down_seconds, dry_run_required, approval_token_sha256)
- `steps_executed[]` (step_id, timestamp, input_hash, output_hash,
  output, operator_signature, prev_step_hash, self_hash)
- `merkle_root`, `chain_head`

### Merkle chain (Phase 3)

The chain is over `steps_executed`. The first step's `prev_step_hash`
is `null` (or all-zeros). Each subsequent step's `prev_step_hash` is
the previous step's `self_hash` = `sha256(canonical_json(step))`. The
`merkle_root` is the standard Merkle root over the `self_hash` list.

Any tamper with a single step invalidates the chain from that step
forward. Detected by `test_auditor.py::test_chain_tamper_detection`.

### Content addressing (Phase 3)

All evidence blobs (telemetry snapshot, twin state, twin simulation
trace, Rego bundle, model weights digest) are stored by SHA-256. The
URI is `evidence://<sha256>`. The runbook stores only the digests, not
the evidence itself. To verify, fetch by digest and re-hash.

### Retention (Phase 3)

- Class C/D: 7 years
- Class A/B: 2 years

The hook is `audit.retention.purge_older_than(ts)`. In production
this is a cron + WORM bucket. In Phase 3, the hook is implemented
and unit-tested; the cron and WORM bucket are deployment concerns.

### Replay harness (Phase 3)

The replay harness loads a recorded runbook, locates the
content-addressed evidence by digest, re-executes the recorded
procedure against the same twin + model version, and asserts
byte-identical output. If a replay diverges:

- Same model version, same procedure → runbook is non-deterministic
  (a bug; fail the test).
- Different model version, same procedure → drift detected; the
  harness attributes the divergence to the model version delta
  (informational, not a failure).

This is the drift detector in CI.

---

## 9. The human-in-the-loop model

### Who is the operator?

The operator is a human at a ground station. The operator's interface
to telos is the Approver UI (Phase 2). In Phase 1, the operator
interaction is implicit (the catalog's `risk_class` tags are set
anticipating what the UI will eventually need).

### What does the operator see?

- The `Proposal` (which cause, which procedure, which risk class)
- The `Verdict` (what the twin predicted)
- The dry-run preview (current state vs. predicted state per step)
- The full evidence chain (which symptoms triggered the diagnosis,
  which catalog conditions matched)
- Citations (FM doc sections, prior runbooks)

### What can the operator do?

- Approve (with their identity attached)
- Reject (with a reason attached)
- Panic Abort (during a Class D hold-down)
- Edit a procedure's parameters (proposed change is sent back through
  Propose → Validate; no direct execution path)

### What the operator cannot do

- Bypass the policy engine
- Approve a Class D command with a single signature
- Modify the cause or procedure catalogs at runtime (see D-5)
- Cause the LLM to be the approval authority (structurally impossible)

---

## 10. Future branches (Phase 2/3/4)

### Phase 2 — Approve → Execute

**What unlocks it:** the Phase 1 `Proposal` and `Verdict` seams are
stable. The OPA/Rego policy engine is added (`mission_ops/policy/`).
The `CommandBus` mock is added (test-only). The 4-class risk scheme,
2-person rule, hold-down timer, and dry-run preview are implemented.

**What changes:** the supervisor learns to call the Approver and the
Executor after the Validator. The CLI adds an `approve` subcommand for
operator testing. The UI (if any) is the Approver UI; it is a
separate repo or sub-app in Phase 2+.

**Test gate:** every risk-class path has a test. The LLM has no path
to an `ApprovalToken` (AST/imports check). Two signatures from the
same operator are rejected. Hold-down timer is checked at the gate,
the Executor, and every step boundary (defense in depth).

### Phase 3 — Verify

**What unlocks it:** Phase 2 produces per-step receipts. The
content-addressed evidence store, Merkle chain, and runbook emitter
land. The replay harness is the test gate.

**What changes:** every Phase 1+ run produces a runbook on disk. The
replay harness is runnable as a CLI (`python -m mission_ops.demos.replay_demo <runbook>`)
for ad-hoc operator use.

**Test gate:** replay a recorded runbook against the same model
version → byte-identical output. Replay against a different model
version → divergence attributed to the model change. Tampered step →
chain breaks from the tampered step forward.

### Phase 4 — LLM narrator + RAG

**What unlocks it:** the structured outputs (Diagnosis, Proposal,
Verdict) are stable from Phase 1. The LLM is added as a
post-processor on those outputs; no schema change is required to
the existing dataclasses. RAG over past runbooks (Phase 3 produces
them) is added to Propose for novel-cause cases where
`get_candidate_procedures(cause)` returns an empty list (i.e., a
cause the catalog doesn't cover).

**What changes:** a new `Narrator` protocol (or equivalent) is
added in `mission_ops/narrator/` with the structural bars from
[D-6](#d-6-llm-narrator-deferred-to-phase-4-no-seam-in-phase-1-code).
The Diagnose, Propose, and Validate stages each get an optional
post-processor hook that calls the narrator; if no narrator is
configured, the hook is a no-op. RAG is added in `mission_ops/rag/`
with a fixed corpus (the runbook store from Phase 3) and a
retrieval function that returns top-k similar past runbooks.

**What does NOT change:** the LLM has no path to authority. It
cannot change the `Cause` enum value, the `Procedure` enum value,
or any field of the `ApprovalToken` (Phase 2). Same audit chain,
same Merkle-chained runbook format, same retention policy.

**Test gate:** the LLM is mocked at the HTTP layer (hosted models)
or with a fixed-response stub (local models). The narrator is
tested against fixtures: known inputs → known narratives, with
citation-grounding assertions. The RAG retrieval is tested for
coverage (no-cause-left-behind on a fixture set). **In CI, no
network calls to LLM providers.** LLM tests run with mocks only;
manual smoke tests can use real LLMs.

---

## 11. Repo conventions

### File layout (as landed)

The pre-merge BIBLE had a planned layout under `mission_ops/`
that never landed. The actual Phase 1 layout is:

- `live/` — the LSTM streaming detector (Detect stage). Edit
  in place. Five modules (`config.py`, `error_stream.py`,
  `generator.py`, `model_runner.py`, `ws_server.py`, plus
  the bridge `twin_bridge.py` and the static dashboard
  HTML in `live/static/index.html`).
  - `live/tests/` — pytest suite for the live pipeline
    (5 test files, 13 tests). Includes the keras stub
    pattern that lets the suite run on system Python
    without the CHESS venv.
- `digital-twin/` — Phase 1+ code (Diagnose, Propose,
  Validate, plus the twin simulator).
  - `digital-twin/twin/` — the digital twin. 12 modules,
    2,791 lines total. The catalog (cause enum, procedure
    enum, `ProcedureSpec` registry, 9 `apply_*` functions)
    lives in `digital-twin/twin/procedures.py` per D-10.
  - `digital-twin/twin/tests/` — pytest suite for the twin
    (4 test files, 32 tests). All 32 pass on system
    Python.
  - `digital-twin/examples/` — runnable demos. 4 files
    (`smoke_test.py`, `phase1_demo.py`,
    `phase1_demo_deep.py`, `slight_shift_demo.py`). All
    but `smoke_test.py` run on system Python; the
    `smoke_test.py` needs the CHESS venv.
  - `digital-twin/data/` — small data fixtures (initial
    state, fault log).
  - `digital-twin/digital_twin_CubeSat/` — vendored CHESS
    reference, **not edited**. License preserved at
    `digital_twin_CubeSat/LICENSE`.
  - `digital-twin/paseos/` — vendored PASEOS reference,
    **not edited**. PASEOS math is used (not the source)
    in `digital-twin/twin/eps.py` and
    `digital-twin/twin/thermal.py` to avoid GPL
    contamination.
  - `digital-twin/telemanom/` — vendored Hundman et al.
    reference, **not edited**. The math is ported into
    `live/error_stream.py`; this directory is kept for
    comparison and for the Telemanom `.npy` channel
    shape.
  - `digital-twin/TWIN_REFERENCE.md` — the owner's
    design document for the twin. Kept for context.
- `BIBLE.md` — this file. Updated in the same commit as any
  architectural change.
- `CLAUDE.md` — the project-level instructions for AI
  agents (multi-agent worktree isolation, "Keep changes
  modular and write tests for all new functions",
  "Never edit files outside your assigned directory or
  branch"). Read this first.
- `README.md` — the user-facing quick start. Updated when
  the runnable surface changes.

**`docs/decisions/` (ADRs) — deferred.** The pre-merge
BIBLE had a planned `docs/decisions/` directory for
Nygard-style ADRs; that directory does not exist. The
BIBLE §4 is the *summary* of decisions, not the full
record. When the second architectural decision lands,
ADRs become worth the directory.

**`mission_ops/` — does not exist.** The pre-merge BIBLE
had a planned `mission_ops/` package with `stages/`,
`twin/`, `knowledge/`, `interfaces/`, `tests/`, `demos/`
subdirectories. None of this was built; the actual
landing sites are `live/` and `digital-twin/twin/`. The
planned-but-not-built status is a useful record for
future agents — the layout was designed, the team chose
not to build it because the digital-twin code came in
through a different path (the owner's `feature/digital-twin`
branch) and the layout was reshaped to match where the
code actually lives.

### Naming

- Files: `snake_case.py`.
- Classes: `PascalCase`.
- Functions and variables: `snake_case`.
- Constants: `UPPER_SNAKE_CASE`.
- Enum values: `UPPER_SNAKE_CASE` for the Python name;
  the string value is `kebab-case` or `snake_case` to
  match the twin's fault-type vocabulary (e.g.,
  `Cause.EPS_INTERNAL_R_RAMP = "eps_internal_r_ramp"`).
- Test files: `test_<thing>.py`.
- Demo files: `<name>_demo.py` for end-to-end demos;
  `smoke_test.py` for the pre-existing regression test.

### Testing

- Every new function gets a test. The "Keep changes
  modular and write tests for all new functions" rule
  in `CLAUDE.md` is the binding version of this.
- Tests live next to the code they test, in a `tests/`
  subdir. `live/tests/` for the live pipeline;
  `digital-twin/twin/tests/` for the twin.
- Use pytest. Use `pytest-asyncio` for async tests.
- Use fixtures from `conftest.py` for shared setup.
- Determinism: every test that touches randomness must
  seed its RNG. The twin is fully deterministic (no
  random input in the step functions); the live
  pipeline seeds the generator's RNG where it matters
  for the regression test.
- The Phase 1 regression gate is 49/51 tests pass on
  system Python (the 2 failures are pre-existing
  keras-gated tests; they pass when the CHESS venv is
  active).

### Commits

- One commit per logical change. Don't bundle unrelated
  changes.
- Commit message: imperative subject line, blank line,
  body explaining *why* (not what — the diff shows what).
- Update the BIBLE in the same commit as any
  architectural change. The BIBLE update is
  *non-optional* — the BIBLE is the project's source
  of truth for "what is telos and why."

### Branches

- `main` is always green. No direct commits to `main`;
  use a feature branch.
- Multi-agent work uses git worktrees (per `CLAUDE.md`).
- The `feature/digital-twin` branch was the integration
  branch for the owner's twin code; it is the
  historical record for D-10 through D-15.

---

## 12. Tooling and dependencies

### Python

- The **CHESS venv** (Python 3.10, with `astropy` and
  `keras`/`tensorflow`) is the production environment for
  the live pipeline. The digital-twin code is pure-Python
  and runs on both the CHESS venv and the system Python.
- The **system Python** (3.12+) is what the test suite and
  the demos run on by default. The keras stub pattern
  (`live/tests/test_injection_bridge.py` installs a
  minimal keras stand-in in `sys.modules` at import time)
  is what makes the system Python sufficient.
- `pip` for dependency management. No `pyproject.toml` is
  committed; the dependency list is in this section.

### Key packages

| Package | Why |
|---|---|
| `fastapi`, `uvicorn`, `websockets` | The `live/` pipeline's HTTP/WS surface (`ws_server.py`, the dashboard) |
| `httpx` | For test client calls in `live/tests/test_injection_bridge.py` and the integration tests |
| `numpy`, `pandas` | The LSTM pipeline (`live/error_stream.py`'s EWMA); also used in the twin (`validate.py:_run_forward`) |
| `more-itertools` | Used by the `ErrorStream` anomaly-grouping math (`mit.consecutive_groups`) |
| `keras`, `tensorflow` | The LSTM model. Loaded at boot by `live/model_runner.py`; stubbed in `sys.modules` for system-Python tests |
| `pytest`, `pytest-asyncio` | Test framework. 49/53 tests pass on system Python |

### What we deliberately do not depend on (Phase 1)

- **No multi-agent framework** (LangGraph, AutoGen, CrewAI).
  The live server is the orchestrator (D-13); the
  bridge is the seam (D-14). We are not married to any
  framework's abstractions.
- **No LLM SDK in Phase 1.** The LLM is a Phase 4 future
  narrator. Phase 1 has no narrator protocol, no no-op
  implementation, no LLM in the test suite, and no API
  keys in the repo. See D-6.
- **No OPA binary in Phase 1.** The policy engine is
  Phase 2.
- **No NATS/Redis in Phase 1.** The bus is the
  in-process WebSocket + the `FaultScheduler` singleton
  on `app.state.live.twin_scheduler`.
- **No ORM / database.** State is in-memory. Phase 3 may
  add SQLite for the runbook index.
- **No `pydantic`.** The pre-merge BIBLE had a planned
  `MissionState` pydantic model; it was not built. The
  twin uses `@dataclass` and `str, Enum` for the
  `Cause` and `Procedure` enums; the live server uses
  plain dicts. The Phase 3 runbook schema is the first
  place pydantic is likely to land.
- **No `networkx`.** The pre-merge BIBLE planned a
  causal DAG; it was not built. Diagnose is a flat
  pattern-matcher against `Cause.expected_channels()`,
  not a graph reasoner.

### Vendored references (not edited)

- `digital-twin/digital_twin_CubeSat/` — CHESS reference
  (Cubesat simulation). License preserved at
  `LICENSE`. Used as the historical / comparison
  reference for the orbit and atmosphere simulation; the
  Phase 1 pipeline uses a simplified orbit
  (35-min period, 40% eclipse fraction, hard-coded in
  `validate.py:_run_forward`).
- `digital-twin/paseos/` — PASEOS reference. License
  preserved. The PASEOS math is used (not the source)
  in `twin/eps.py` and `twin/thermal.py` to avoid GPL
  contamination. The vendored source is kept for
  reference; do not import from it.
- `digital-twin/telemanom/` — Hundman et al. 2018
  reference. The math is ported into
  `live/error_stream.py`; this directory is kept for
  the Telemanom `.npy` channel shape and for the
  pre-existing `smoke_test.py` regression test.

---

## 13. Glossary

**AlertEvent** — the output of Stage 1 (Detect). A
`@dataclass` in `live/error_stream.py:41-46` with
`{t, score, seq, kind}`. `t` is the live-stream
index; `score` is the severity from
`score_anomalies`; `seq` is `(start_t, end_t)` in
live-stream indices; `kind` is `"anomaly"` (with
forward-looking values for `shift`, `spike`, `dropout`).
The dataclass does **not** carry `channel` or
`subsystem` — those are added by the bridge from the
`channel_hint` query param on `/inject_twin_fault`. A
Phase 2 polish item is to plumb `channel` into
`AlertEvent` itself (see §3.3 and the
`alerts_to_symptom_events` legacy hardcode in
`live/twin_bridge.py:138-180`).

**Anomaly** — a per-channel deviation signal from the
streaming detector. In `live/error_stream.py` this is an
`AlertEvent`. In `twin/diagnose.py` it's re-typed as a
`SymptomEvent`.

**Approval token** — a signed authorization to execute a
procedure. The only path to which is the policy engine
(Phase 2). The LLM has no path to it.

**Bridge** — `live/twin_bridge.py` (348 lines). The
single seam between `live/` and `twin/` (D-14). Exposes
two public functions (`inject_fault`,
`run_phase1_pipeline`) and two public tables
(`INJECTION_TO_FAULT`, `CHANNEL_TO_SUBSYSTEM`). Uses
lazy imports to keep the CHESS astropy chain out of
the live server's boot path.

**CandidateCause** — the dataclass returned per ranked
cause by `twin.diagnose.diagnose()`. Has `{cause: Cause,
score: float, matched_events: list[SymptomEvent],
evidence_subsystems: list[str]}`.

**Cause** — a named, cataloged hypothesis for *why* an
anomaly pattern is occurring. 13 values in
`digital-twin/twin/procedures.py:Cause`. Each has
`affected_subsystems()` and `expected_channels()` methods
that the Diagnose stage scores against. Each cause's
string value matches a `fault_type` in
`twin/fault_injection.py:FaultScheduler` so the runbook
can verify "we suspected X and we were right."

**Channel** — a named telemetry stream on a single
physical quantity. Phase 1 has 8 channels (the
Telemanom SMAP/MSL prefix convention):

| Channel | Subsystem | Quantity | Unit |
|---|---|---|---|
| P-1 | EPS | Bus voltage | V (28V nominal) |
| P-2 | EPS | Solar panel current | A |
| B-1 | Battery / Thermal | Battery SoC + temperature | dimensionless / °C (shared) |
| T-1 | Thermal | Payload temperature | °C |
| T-2 | Thermal | Electronics temperature | °C |
| A-1 | ADCS | Star-tracker pointing error | arcsec |
| G-1 | ADCS | Reaction-wheel speed | rpm |
| D-1 | Comms | Link margin | dB |

The full set is defined by the twin; the BIBLE catalogs
it for cross-reference.

**CHANNEL_TO_SUBSYSTEM** — the 8-channel → 4-subsystem
map in `live/twin_bridge.py:66-72`. Used to build
`SymptomEvent`s for Diagnose when the bridge knows
which channel the injection targeted.

**Content addressing** — storing data by the hash of
its content (SHA-256). The hash *is* the address. The
Phase 3 runbook stores only the digests, not the
evidence itself; to verify, fetch by digest and
re-hash.

**Diagnosis** — a ranked list of candidate causes for a
sliding window of symptoms, with the evidence chain
attached. The output of Stage 2 (Diagnose).

**Dry-run** — a state-diff preview of what a procedure
will do, computed by the twin. Shown to the operator
before they approve a Class C or Class D action (Phase
2). The two-trajectory model in §6.5 is the Phase 1
precursor — the predicted vs baseline trajectories are
the dry-run.

**Evidence chain** — the sequence of content-addressed
blobs (telemetry, twin state, twin simulation trace,
model weights, policy bundle) that a runbook
references. Together with the Merkle chain over the
steps, this is what makes a runbook auditable.

**Hold-down** — a mandatory waiting period (30s for
Class D) after the last approval signature, during
which any operator can panic-abort the action.

**INJECTION_TO_FAULT** — the 13-entry mapping table in
`live/twin_bridge.py:40-61`. Keys are `(kind, channel)`
pairs; values are `(twin fault_type, default params)`.
3 entries with `channel=None` for the legacy
3-kind interface; 10 channel-specific entries for the
demo helpers.

**Merkle chain** — a sequence of hashes where each
step's hash includes the previous step's hash. Detects
tampering.

**Narrator** — the post-processor (Phase 4) that wraps
an LLM around already-structured outputs (Diagnosis,
Proposal, Verdict) to write human-readable prose.
Defined as a protocol only in Phase 4; the seam is
deferred (see D-6). The narrator is structurally
barred from authority — it can write prose but cannot
change the structured fields.

**Procedure** — a typed, ordered list of steps that
address a cause. 9 values in
`digital-twin/twin/procedures.py:Procedure`. Each
procedure has a `ProcedureSpec` in
`PROCEDURE_REGISTRY` with parameter bounds,
preconditions, postconditions, an `apply_fn`, a
`risk_class` (Phase 1 strings; Phase 2 A/B/C/D), and
an `approval_required` field (`auto / operator /
director`).

**Proposal** — the chosen cause + chosen procedure +
risk score + Verdict + the full ranked candidate list.
The dataclass returned by `twin.propose.propose()`. Has
`{cause, cause_score, procedure, procedure_params,
risk_score, validation, verdict, candidates_ranked}`.
The `to_dict()` method produces the JSON-serializable
form for the WebSocket broadcast.

**Replay** — re-executing a recorded runbook against
the same inputs (catalog versions, model version, twin
state) to verify the output is byte-identical. Detects
non-determinism and drift.

**Risk class** — the Phase 1 catalog uses
`low / medium / high / critical` strings on each
`ProcedureSpec.risk_class` field. The Phase 2 / §7
scheme is `A / B / C / D` (A=read-only, B=reversible,
C=expensive, D=irreversible). The mapping (low→B,
medium→C, high→C, critical→D is the draft) is Phase 2
work. The `approval_required` field is the
owner-supplied 3-tier scheme (`auto / operator /
director`); the A/B/C/D scheme is a 4-tier refinement
of the `operator` tier.

**Runbook** — the tamper-evident, replayable record of
an anomaly-to-resolution episode. Produced by Stage 7
(Verify). Phase 3.

**Sentinel cause** — a `Cause` enum value that
represents "no fault present," used to terminate the
Diagnose stage when no symptom pattern matches. In
telos this is `Cause.NO_FAULT_DETECTED`. Sentinel
causes do not require a procedure; the system is at
rest.

**Subsystem** — a logical grouping of channels that
share a function on the spacecraft. telos has 5
subsystems: EPS (P-1, P-2), Battery (B-1, shared with
Thermal), Thermal (B-1, T-1, T-2), ADCS (A-1, G-1),
Comms (D-1). The set of subsystems is determined by
the twin; the BIBLE catalogs it for cross-reference.

**Symptom / SymptomEvent** — the output of Stage 1
(Detect), re-typed for Stage 2 (Diagnose). A frozen
`@dataclass` in `twin/diagnose.py:37-44` with
`{channel, subsystem, kind, score, seq, ts}`. The
kind field is `"anomaly" | "shift" | "spike" |
"dropout" | "noise"`.

**Trajectory** — a `dict[str, np.ndarray]` returned by
`validate_procedure()`. Keys: `battery_soc`,
`battery_voltage_v`, `battery_temp_c`,
`payload_temp_c`, `electronics_temp_c`,
`radiator_temp_c`, `pointing_error_deg`, etc. Each
array has `horizon_s / dt_s + 1` entries. The
`ValidationResult` carries two trajectories
(`predicted_trajectory` and `baseline_trajectory`)
with the same keys and same shape, used for direct
side-by-side comparison.

**Twin** — the deterministic state-machine simulator
in `digital-twin/twin/`. Receives a starting state +
a procedure + params, returns the predicted state at
each timestep + the no-action baseline. 2,791 lines,
12 modules. Never touches the `CommandBus` (there is
no `CommandBus` in Phase 1).

**ValidationResult** — the dataclass returned by
`twin.validate.validate_procedure()`. Has
`{feasible, violations, risk_score,
predicted_trajectory, baseline_trajectory, summary,
postcondition_check}`. The `to_verdict()` wrapper
maps it to the BIBLE-shaped `Verdict` (next entry).

**Verdict** — the result of running a procedure
through the twin. `OK | REJECT(reason) |
INCONCLUSIVE(what_we_need_to_know)`. The output of
Stage 4 (Validate). The dataclass in
`twin/verdict.py:37-49` has
`{proposal_id, status, reason, per_step_outcomes,
twin_simulation_digest, notes}`. The
`proposal_id` and `twin_simulation_digest` fields are
reserved for Phase 3 (empty strings in Phase 1) but
are structurally present so the Phase 3 schema doesn't
break the Phase 1 contract.

**Merkle chain** — a sequence of hashes where each step's hash
includes the previous step's hash. Detects tampering.

**Content addressing** — storing data by the hash of its content
(SHA-256). The hash *is* the address. Lets the runbook prove what
inputs it saw by referencing the digests.

**Replay** — re-executing a recorded runbook against the same
inputs (catalog versions, model version, twin state) to verify the
output is byte-identical. Detects non-determinism and drift.

**Narrator** — the post-processor (Phase 4) that wraps an LLM around
already-structured outputs (Diagnosis, Proposal, Verdict) to write
human-readable prose. Defined as a protocol only in Phase 4; the
seam is deferred (see D-6). The narrator is structurally barred
from authority — it can write prose but cannot change the structured
fields.

**Approval token** — a signed authorization to execute a
procedure. The only path to which is the policy engine (Phase 2).
The LLM has no path to it.

**Evidence chain** — the sequence of content-addressed blobs
(telemetry, twin state, twin simulation trace, model weights,
policy bundle) that a runbook references. Together with the Merkle
chain over the steps, this is what makes a runbook auditable.

**Channel** — a named telemetry stream on a single physical quantity
(e.g., P-1 is the EPS bus voltage, D-1 is the comms link margin).
The full set of channels in telos is documented in
`mission_ops/twin/procedures.py`'s channel definitions, with the
owner-supplied mapping to subsystems.

**Subsystem** — a logical grouping of channels that share a function
on the spacecraft. telos's subsystems: EPS (channels P-1, P-2),
Battery (B-1, shared with Thermal for temperature), Thermal
(B-1, T-1, T-2), ADCS (A-1, G-1), Comms (D-1). The set of subsystems
is determined by the twin; the BIBLE catalogs it for cross-reference.

**Sentinel cause** — a `Cause` enum value that represents "no fault
present," used to terminate the Diagnose stage when no symptom
pattern matches. In telos this is `no_fault_detected`. Sentinel
causes do not require a procedure; the system is at rest.

---

## 14. The digital-twin integration — landed history

> **Status (updated 2026-09-01):** this section was originally
> titled "Incoming: the digital twin branch" and described the
> pre-merge plan. With the integration complete, it is rewritten
> as a landed-history changelog. The integration landed
> cleanly, with the architecture close to the pre-merge plan
> (5 subsystems, 8 channels, 13 causes, 9 procedures, single
> Python module per D-10) and four new decisions that emerged
> from the integration work (D-12 through D-15).

### What was incoming (recap of the 2026-08-30 BIBLE update)

The pre-integration BIBLE §14 said:

- **Branch name:** `feature/digital-twin`, base `main` at
  the 2026-08-30 commit.
- **Owner of the branch:** the project owner. The twin was
  being supplied as a separate repo to be merged in.
- **Integration responsibility:** the assistant merges
  the owner's twin code, resolving any conflicts, with
  no new architectural decisions made during the merge
  without being added to the BIBLE first.
- **Expected scope:** 5+ subsystems, 8 channels, 13
  causes, 9 procedures, `apply_procedure()` as the one
  state-mutation point.
- **D-10 and D-11** were the pre-merge decisions about
  the catalog and the twin being one file, with
  13 × 9 × ~21 scope.

### What landed

- **Twin core:** 12 modules, 2,791 lines in
  `digital-twin/twin/`. Subsystems, channels, causes, and
  procedures all as planned. The catalog lives in
  `digital-twin/twin/procedures.py` per D-10.
- **Bridge:** `live/twin_bridge.py` (348 lines) as the
  single seam between `live/` and `twin/` per D-14.
  The lazy-import pattern keeps the CHESS astropy
  chain out of the live server's boot path.
- **Phase 1 pipeline:** all four stages (Detect, Diagnose,
  Propose, Validate) runnable end-to-end. The
  `/inject_twin_fault` endpoint on the live server
  runs the full pipeline and returns a BIBLE-shaped
  verdict.
- **Test gate:** 49/53 tests pass on system Python
  (32 twin + 13 live pre-existing + 4 new integration).
  The 2 keras-gated failures are pre-existing and pass
  when the CHESS venv is active.
- **Demos:** 4 runnable demos in
  `digital-twin/examples/`. `phase1_demo.py`,
  `phase1_demo_deep.py`, and `slight_shift_demo.py`
  run on system Python; `smoke_test.py` needs the
  CHESS venv.

### What was deferred

- **Frontend (3-phase build per §3.2):** not started.
  The first frontend commit (Phase 1 — live data)
  is the next chunk of work.
- **Phase 2/3/4:** the policy engine, the runbook, the
  LLM narrator. Documented in §3.3 and §10.
- **Phase 1 polish items (4):** see §3.3.
  - Diagnose can disambiguate the 3 thermal causes
    better with a richer symptom shape.
  - The `alerts_to_symptom_events` legacy hardcode
    in `live/twin_bridge.py:138-180`.
  - The `risk_class` string-to-A/B/C/D mapping
    (Phase 2 work).
  - The CHESS venv CI setup for the 2 keras-gated
    tests.

### Decisions that emerged from the integration

Four new decisions were added to §4 as a direct result
of the integration work:

- **[D-12](#d-12-propose-uses-validation-based-ranking-not-catalog-lookup):** Propose uses
  validation-based ranking, not catalog lookup. The
  twin is the ranking function, not a lookup table.
  This is the biggest architectural surprise of the
  integration — the pre-merge BIBLE §2 framed Propose
  as "look up procedure catalog by cause," which was
  replaced by the actual landing.
- **[D-13](#d-13-the-twin-runs-in-process-same-python-interpreter-as-live):** The twin runs
  in-process, same Python interpreter as `live/`.
  Pre-merge BIBLE §6 framed this as a Phase 2 evolution;
  it landed in Phase 1 because the lazy-import
  pattern kept the deployment story simple.
- **[D-14](#d-14-the-bridge-is-the-only-place-live-imports-from-twin):** The bridge is
  the only place `live/` imports from `twin/`. Codified
  from the integration work; previously just a
  design goal in the pre-merge BIBLE §14.
- **[D-15](#d-15-terminal-demos-stay-alongside-the-frontend-they-answer-different-questions):**
  Terminal demos stay alongside the frontend (once
  it lands); they answer different questions. The
  pre-merge BIBLE had no frontend; the integration
  work landed 3 demos that answer the deep-detail
  questions, and the 3-phase frontend (§3.2) will
  answer the live-view questions.

### Where to look

| What you want | Where |
|---|---|
| The 7-stage pipeline contract | §2 |
| What's runnable today | §3.1 |
| What's being built next (frontend) | §3.2 |
| What's deferred (Phase 2/3/4 + polish) | §3.3 |
| Why Propose uses twin ranking | D-12 |
| Why twin is in-process | D-13 |
| Why the bridge is the seam | D-14 |
| Why demos + frontend | D-15 |
| The catalog and its invariants | §5 |
| The twin's actual scope and the two-trajectory Validate | §6 |
| The 13 causes | §6.2 |
| The 9 procedures | §6.3 |
| The cause→procedure map | §6.4 |
| How Validate works | §6.5 |
| The repo's actual file layout | §11 |
| Vendored references and what we don't depend on | §12 |
| New terms (ValidationResult, Proposal, Trajectory, Bridge, etc.) | §13 |

### What the integration must NOT do (still binding)

The "What the merge must NOT do" list from the pre-merge
BIBLE §14 is still binding — it describes invariants
that the integration has not violated, and that future
work must continue to honor:

- **Do not** change the names or types of the existing
  public APIs in `live/` or `telemanom/`. The Detect
  stage contract is locked.
- **Do not** introduce LLM dependencies. The LLM is a
  Phase 4 narrator; the twin branch and all subsequent
  work is LLM-free.
- **Do not** introduce OPA, NATS, Redis, or any Phase
  2/3 infrastructure without an explicit Phase 2/3
  work item. The twin is the in-process deterministic
  simulator.
- **Do not** introduce a database. State is in-memory;
  the only on-disk artifacts are the trained LSTM
  weights and the runbook JSON (Phase 3).
- **Do not** rewrite the existing `live/` or
  `telemanom/` code. Add new code under
  `digital-twin/`; do not touch old code.
- **Do not** add a CI configuration without an
  explicit owner request. CI is not in the current
  scope.
- **Do not** add an LLM SDK or narrator protocol
  without a Phase 4 work item. D-6 is binding.

### The BIBLE update rule (still binding)

**No commit that contains code from the twin branch
or that changes the architecture is acceptable without
the corresponding BIBLE update in the same commit.**
This is a hard rule. The pre-merge BIBLE §14
established it; the post-merge BIBLE update
(this rewrite) honors it. Future agents must
continue to honor it: if you change the
architecture, update the BIBLE in the same commit.

---

*Last updated: 2026-09-04 (D-16/D-17/D-18 — catalog-level
editorial fields, parallel sims + sim_progress streaming,
RankedCandidate). The BIBLE is the project's source of
truth. Code may drift; the BIBLE must not.*
