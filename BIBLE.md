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
5. [The hand-authored knowledge catalogs](#5-the-hand-authored-knowledge-catalogs)
6. [The digital twin](#6-the-digital-twin)
7. [Trust and approval](#7-trust-and-approval)
8. [Audit and runbooks](#8-audit-and-runbooks)
9. [The human-in-the-loop model](#9-the-human-in-the-loop-model)
10. [Future branches (Phase 2/3/4)](#10-future-branches-phase-234)
11. [Repo conventions](#11-repo-conventions)
12. [Tooling and dependencies](#12-tooling-and-dependencies)
13. [Glossary](#13-glossary)

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
[Telemetry source: live/sim.py producer or replay]
                  | tick(actual, predicted)
                  v
1. DETECT      wraps live/error_stream.ErrorStream
               emits SymptomEvent (per-channel deviation signal)
                  |
                  v
2. DIAGNOSE    pattern-matches SymptomEvents against cause catalog
               returns ranked CandidateCause[] with evidence chain
                  |
                  v
3. PROPOSE     looks up procedure catalog by cause
               returns concrete Procedure with typed steps + dry-run state diff
                  |
                  v
4. VALIDATE    runs procedure through sidecar digital twin
               returns Verdict = OK | REJECT(reason) | INCONCLUSIVE(...)
                  |
                  v
─────────────────────────────────────── Phase 1 boundary ───────────────────────────────────────
                  |
                  v
5. APPROVE   (deferred) policy-gated operator approval; 4-class risk; LLM cannot reach this
6. EXECUTE   (deferred) walks approved procedure through mock CommandBus; per-step receipts
7. VERIFY    (deferred) Merkle-chained runbook + content-addressed evidence + replay harness
```

### Stage 1 — Detect

**What it does:** continuously scores telemetry against an LSTM-predicted
baseline, emits a per-channel deviation signal when the smoothed error
exceeds a learned threshold.

**Where it lives:** `live/error_stream.py` (existing) wrapped by
`mission_ops/stages/detect.py` (Phase 1).

**What it consumes:** `(actual_value, predicted_value)` per tick.

**What it produces:** `SymptomEvent {channel, kind, score, seq, ts}` where
`channel` and `subsystem` are derived from `subsystems.yaml`.

**LLM involvement:** none.

### Stage 2 — Diagnose

**What it does:** given a stream of `SymptomEvent`s over a sliding time
window, match them against a hand-authored cause catalog and return ranked
candidate causes with the evidence chain attached.

**Where it lives:** `mission_ops/stages/diagnose.py` (Phase 1).

**What it consumes:** a sliding window of `SymptomEvent`s (default: 1
minute, configurable per cause).

**What it produces:** `Diagnosis {candidates: list[CandidateCause],
window: tuple[int, int], evidence_chain: list[EvidenceRef]}` where each
`CandidateCause` is `{cause_id, score, matched_conditions,
unmatched_conditions, evidence_refs, references}`.

**LLM involvement:** in Phase 4 only — narrator role. The LLM writes prose
*around* the already-ranked list, citing the matched conditions. The LLM
does **not** pick the top cause.

### Stage 3 — Propose

**What it does:** given a `Diagnosis`, select a concrete `Procedure` from
the procedure catalog and compute the dry-run state diff (predicted state
after each step) using the twin in sandbox mode.

**Where it lives:** `mission_ops/stages/propose.py` (Phase 1).

**What it consumes:** `Diagnosis` (top-1 cause preferred, top-k fallback).

**What it produces:** `Proposal {proposal_id, cause_id, cause_score,
evidence_chain, procedure, dry_run_state_diff, risk_class, provenance,
citations}`.

**LLM involvement:** in Phase 4 only — proposer role for novel-cause
cases. The LLM's output is constrained to a JSON schema; the LLM cannot
invent `action` values outside the twin's step vocabulary. Every
LLM-drafted procedure requires explicit human sign-off before being
added to the catalog.

### Stage 4 — Validate

**What it does:** run the `Proposal`'s procedure through the sidecar
digital twin step by step. Compare predicted state to each step's
`expected_state` and `abort_on` conditions. Return a verdict.

**Where it lives:** `mission_ops/stages/validate.py` (Phase 1).

**What it consumes:** `Proposal` (from Stage 3).

**What it produces:** `Verdict {proposal_id, status, reason,
per_step_outcomes, twin_simulation_digest, notes}` where `status` is one
of `OK | REJECT(reason) | INCONCLUSIVE(what_we_need_to_know)`.

**LLM involvement:** none.

### Stage 5 — Approve *(deferred to Phase 2)*

**What it does:** given a `Proposal` and a `Verdict`, route the action
through the operator approval flow that matches its `risk_class`. Issue
an `ApprovalToken` if and only if the policy says so.

**Where it will live:** `mission_ops/stages/approve.py` (Phase 2).

**LLM involvement:** none. **The LLM has no path to the approval gate.**
This is enforced structurally (the LLM's tool surface does not include
the gate), not by prompt. See [§7 Trust and approval](#7-trust-and-approval).

### Stage 6 — Execute *(deferred to Phase 2)*

**What it does:** given an `ApprovalToken` and a `Procedure`, walk the
procedure's steps through a `CommandBus`, recording per-step input/output
hashes and operator signatures.

**Where it will live:** `mission_ops/stages/execute.py` (Phase 2).

**LLM involvement:** none.

### Stage 7 — Verify *(deferred to Phase 3)*

**What it does:** build a Merkle-chained runbook from the per-step
receipts, content-address every evidence blob (telemetry, twin state,
twin simulation trace, model weights digest, Rego policy bundle), and
register the runbook with the replay harness.

**Where it will live:** `mission_ops/stages/verify.py` (Phase 3).

**LLM involvement:** none.

### Orchestration

The supervisor (`mission_ops/supervisor.py`, ~150 lines, Phase 1) holds
the typed `MissionState` (a pydantic model) and routes each step. It is
**not an LLM**. All stages are pure functions
`(state_in) -> (state_out, side_effects)`. State cannot be mutated outside
return values. This makes replay trivial and audit natural.

---

## 3. Current implementation state

### What's runnable today

- **Stage 1 (Detect) — fully runnable.** `live/error_stream.py` and
  `live/model_runner.py` together implement the streaming LSTM detector.
  The `python -m live.sim` entrypoint boots a websocket server that emits
  `AlertEvent`s and exposes a `/inject` endpoint for synthetic anomaly
  injection. The `live/tests/` suite is green.

- **`telemanom/` reference — vendored, not modified.** Thresholding math
  already ported into `live/`. No re-port needed.

### What's designed (in plan) but not yet in code

- **Stages 2–4 (Diagnose, Propose, Validate).** Full design in
  `glowing-painting-possum.md`. Awaiting the cause and procedure
  catalogs.
- **Cause catalog (`mission_ops/knowledge/causes.yaml`).** 4 causes with
  2 procedures each, scoped and structured. Catalog content is pending
  owner review.
- **Procedure catalog (`mission_ops/knowledge/procedures.yaml`).** Same
  status as the cause catalog.
- **Twin (`mission_ops/twin/`).** Scope to be specified by the owner.
  File structure designed (`sim.py`, `state.py`, `vocabulary.py`,
  `sandbox.py`); content awaiting the owner's twin spec.
- **State, bus, supervisor.** Designed; not coded.

### What's deferred (designed, not started)

- **Stages 5–7 (Approve, Execute, Verify).** Interfaces designed; not
  built. Phase 2 / Phase 3 work.
- **LLM narrator + RAG.** Phase 4. The seam is designed (`Narrator`
  protocol in Phase 1) but the implementation is a no-op in Phase 1.

### Working plan (not in the repo)

- `/home/rishabh/.claude/plans/glowing-painting-possum.md` — the active
  planning document. Updated as the design evolves.
- `/home/rishabh/.claude/plans/glowing-painting-possum-agent-ab34f718c65d76b95.md`
  — research summary (multi-agent patterns, satellite-ops references,
  digital-twin patterns, HITL patterns, audit patterns).
- `/home/rishabh/.claude/plans/glowing-painting-possum-agent-a7ec1f2bc7082d7f2.md`
  — early detailed design draft (superseded by the active plan and this
  Bible, but kept for context).

---

## 4. Decisions we took (and the alternatives we rejected)

This section is the project's *truth base* for future work. When a future
agent or contributor asks "why did we do it this way?", the answer is
here. **Do not re-litigate these decisions without first reading this
section in full.** If you change one, update the corresponding entry
here in the same commit.

### D-1. Causal knowledge source: hand-authored YAML catalog, not a learned DAG

**Decision:** the cause catalog is a hand-authored YAML file
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

**Decision:** the procedure catalog is a hand-authored YAML file
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

### D-3. Catalog coverage: 4 causes × 2 procedures each

**Decision:** Phase 1 ships with **4 fully-developed causes**, each with
**2 fully-developed procedures**. Total: 8 procedures. Every cause has
a real `symptom_pattern`, `propagation_path`, `candidate_procedures`,
and `references`. No stubs. No "shape-only" entries.

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
example 4 causes with `min_duration_ticks` in the 30–90 range. The
slower causes (e.g., a sensor drifting over 5 minutes) have their
own `min_duration_ticks` set higher, but they're processed on a longer
rolling window (the loader keeps two windows: a 1-minute hot window and
a 5-minute cold window; causes pick which they consume).

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

### D-6. LLM-narrator seam: visible in Phase 1, no-op implementation

**Decision:** Phase 1 defines a `Narrator` protocol with a no-op
implementation (`NoOpNarrator` returns empty strings). Diagnose calls
the narrator and the empty string is part of the structured `Diagnosis`
output. Phase 4 plugs in an LLM implementation; Diagnose does not
change.

**Why we chose what we chose:** the seam is cheap (~30 lines), makes
Phase 4 a strictly additive change, and the no-op is genuinely
testable (it produces a deterministic empty string, which is part of
the byte-identical regression test). The cost is trivial; the value is
real.

**Why we did not wire up an LLM in Phase 1:** no LLM agency, no
non-determinism, no prompt-injection surface, no API key dependency.
The regression test is byte-identical. The LLM is a Phase 4 future
narrator, and that's where it stays.

---

### D-7. Twin scope: 3 subsystems (Comms, Power, Thermal) as the example

**Decision:** the Phase 1 twin models 3 example subsystems: Comms,
Power, Thermal. This is a closed-world example, not a general
satellite simulator.

**Alternatives considered:**

- **6+ subsystems (Attitude, Propulsion, GNC, Payload, ...).** Rejected
  because (i) the build cost scales linearly with subsystem count, (ii)
  the credibility of a "fake physics" twin drops sharply once you
  start modeling attitude dynamics, propulsion, and orbital mechanics,
  (iii) we'd be writing a bad simulator rather than demonstrating the
  validation contract.

**Why we chose what we chose:** 3 subsystems with well-understood
cross-coupling (thermal affects power efficiency; power affects comms
output; comms is the primary observable) is enough to demonstrate
cross-subsystem diagnosis and recovery. The schema generalizes;
adding a 4th subsystem later is an additive change to
`subsystems.yaml` and the twin's vocabulary.

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

## 5. The hand-authored knowledge catalogs

### Why data, not code

The cause catalog and procedure catalog are YAML files in
`mission_ops/knowledge/`. They are loaded by a typed loader
(`loader.py`) that validates the schema, rejects unknown fields, and
fails loud on missing required fields.

**Why data, not code:** ops engineers and regulators can review the
catalogs without reading Python. The Diagnose and Propose algorithms
are general; the catalogs are the variable input. When the FM team
learns about a new anomaly class, they update the catalog, not the
algorithm.

### Cause catalog entry shape

```yaml
- id: cause.comms.modulator.degraded
  subsystem: comms
  description: "Modulator output amplitude drift due to component aging"
  symptom_pattern:
    - channel: P-2
      kind: shift
      min_duration_ticks: 30
      min_score: 4.0
  propagation_path: [P-2, R-1, comms.modulator]
  evidence_subsystems: [power, comms]
  candidate_procedures: [proc.comms.modulator.reset, proc.comms.modulator.recal]
  default_procedure_id: proc.comms.modulator.reset
  default_risk_class: C
  references:
    - "FM-doc §4.2.1"
    - "RB-2026-03-14-7a1c"   # prior runbook that exercised this cause
  version: "0.1.0"
```

### Procedure catalog entry shape

```yaml
- id: proc.comms.modulator.reset
  risk_class: C                       # max of step risk_classes
  description: "Soft reset of the comms modulator, with diagnostic dump first"
  steps:
    - id: 1
      action: modulator.diag_dump
      params: {}
      expected_state: {comms.modulator.state: DIAG_OK}
      abort_on: {comms.modulator.state: FAULT}
      risk_class: A
    - id: 2
      action: modulator.soft_reset
      params: {hold_seconds: 5}
      expected_state:
        comms.modulator.state: NOMINAL
        comms.modulator.amplitude: "in_tolerance"
      abort_on:
        power.bus.voltage: "<24V"
      risk_class: C
  version: "0.1.0"
```

### Loader invariants

- Unknown fields → reject (fail loud). This catches typos and schema
  drift.
- Missing required fields → reject.
- `version` field is mandatory and bumped on every change.
- `id` fields are unique within a catalog.
- `candidate_procedures` references in a cause must exist in the
  procedure catalog (cross-reference check at load time).
- `default_procedure_id` must be in `candidate_procedures`.
- Step `action` values must be in the twin's step vocabulary
  (cross-reference check at load time).

### Versioning

Both catalogs carry a `version` field. The version is logged in every
runbook's `input_evidence.dag_version` (cause catalog version) and
`procedure_version` (procedure catalog version) fields. A runbook is
replayable only against the catalog versions it was generated with,
unless the catalog explicitly declares backward-compatibility.

---

## 6. The digital twin

> **Owner note: the twin scope is being re-specified by the owner. This
> section will be updated when that spec lands.** The current planning
> assumption is a deterministic state-machine twin over 3 subsystems
> (Comms, Power, Thermal), per [D-7](#d-7-twin-scope-3-subsystems-comms-power-thermal-as-the-example).
> The owner has indicated the eventual twin will be a more complex
> subsystem; the architecture supports this by treating the twin as a
> pluggable sidecar.

### Twin invariants (regardless of scope)

- **Deterministic.** Same state + same procedure → same result,
  byte-for-byte. Tested by `test_twin.py::test_twin_deterministic`.
- **Sidecar.** Runs in the same process as the agent pipeline (Phase 1)
  or as a separate process (Phase 2+). Never touches the
  `CommandBus`. Tested by
  `test_twin.py::test_twin_has_no_command_bus_reference` (AST/imports
  check).
- **Pure step vocabulary.** Every `action` in the procedure catalog
  corresponds to a pure function in the twin's vocabulary. The
  Propose stage cannot invent `action` values outside this vocabulary.
- **Content-addressed state.** Every twin state is serializable and
  hashable. The simulation trace (the sequence of states visited
  during a procedure run) is content-addressed by SHA-256 and
  attached to every `Verdict`.
- **Sandbox mode for Propose.** Propose uses a read-only twin to
  compute the `dry_run_state_diff` without side effects. The same
  step functions are used in both modes; only the state container
  differs.

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

**What unlocks it:** Phase 1 ships the `Narrator` protocol with a
no-op. Phase 4 plugs in a real LLM implementation (Claude API,
local model via ollama/llama.cpp, or hosted service — owner's
choice). RAG over past runbooks (Phase 3 produces them) is added
to Propose for novel-cause cases.

**What changes:** the Diagnose output gets a `llm_narrative` field
filled with the LLM's prose. The Propose output for novel-cause cases
uses retrieved runbooks as the proposal draft. **The LLM never gains
authority.** Same `Narrator` protocol, same `Proposal` shape, same
audit chain.

**Test gate:** the LLM is a pure function of its inputs (tested
against a fixed fixture). The narrative is checked for citation
grounding (every claim in the narrative maps to a catalog entry or
evidence ref). The RAG retrieval is checked for coverage
(no-cause-left-behind on a fixture set).

---

## 11. Repo conventions

### File layout

- `live/` — the existing LSTM streaming detector (Detect stage). Edit
  in place. No new subpackages.
- `telemanom/` — vendored reference. **Not edited.** If you need to
  update telemanom, do it upstream and re-vendor.
- `mission_ops/` — Phase 1+ code.
  - `stages/` — one file per stage (`detect.py`, `diagnose.py`,
    `propose.py`, `validate.py`, plus deferred `approve.py`,
    `execute.py`, `verify.py`).
  - `twin/` — the digital twin (`sim.py`, `state.py`, `vocabulary.py`,
    `sandbox.py`).
  - `knowledge/` — the cause and procedure catalogs
    (`causes.yaml`, `procedures.yaml`, `subsystems.yaml`) and the
    typed loader (`loader.py`).
  - `interfaces/` — interfaces for deferred stages (designed in
    Phase 1, built in Phase 2/3).
  - `tests/` — pytest suite. One test file per stage
    (`test_<stage>.py`).
  - `demos/` — runnable end-to-end demos.
- `docs/decisions/` — ADRs. One Markdown file per ADR, named
  `NNNN-short-slug.md` (zero-padded number, e.g.
  `0001-supervisor-architecture.md`).
- `BIBLE.md` — this file. Updated in the same commit as any
  architectural change.
- `README.md` — the user-facing quick start. Updated when the
  runnable surface changes.

### Naming

- Files: `snake_case.py`.
- Classes: `PascalCase`.
- Functions and variables: `snake_case`.
- Constants: `UPPER_SNAKE_CASE`.
- YAML catalog entries: `kebab-case` ids (e.g.,
  `cause.comms.modulator.degraded`).
- ADR filenames: `NNNN-short-slug.md` (4-digit zero-padded).

### Testing

- Every new function gets a test.
- Tests live next to the code they test, in a `tests/` subdir.
- Use pytest. Use `pytest-asyncio` for async tests.
- Use fixtures from `conftest.py` for shared setup.
- Determinism: every test that touches randomness must seed its RNG.
  The regression test is the release gate; it must be byte-identical.

### ADRs

- Every architectural decision lands as an ADR in
  `docs/decisions/` before the code that implements it.
- The ADR format follows Michael Nygard's template (Context, Decision,
  Consequences).
- The Bible's [§4 Decisions we took](#4-decisions-we-took-and-the-alternatives-we-rejected)
  is the *summary*; the ADRs are the *full record*. Update both.

### Commits

- One commit per logical change. Don't bundle unrelated changes.
- Commit message: imperative subject line, blank line, body explaining
  *why* (not what — the diff shows what).
- Update the Bible in the same commit as any architectural change.
- Update the README in the same commit as any change to the runnable
  surface.

### Branches

- `main` is always green. No direct commits to `main`; use a feature
  branch.
- Multi-agent work uses git worktrees (per `CLAUDE.md`).

---

## 12. Tooling and dependencies

### Python

- Python 3.11+ (use the `pyproject.toml`'s `requires-python`).
- `pip` for dependency management.

### Key packages

| Package | Why |
|---|---|
| `fastapi`, `uvicorn`, `websockets` | The existing `live/` pipeline's HTTP/WS surface. |
| `httpx` | For test client calls in the integration tests. |
| `numpy`, `pandas` | The existing LSTM pipeline; also used in the twin. |
| `more-itertools` | Used by the `ErrorStream` anomaly-grouping math. |
| `keras`, `tensorflow` | The LSTM model. |
| `pytest`, `pytest-asyncio` | Test framework. |
| `pydantic` | The `MissionState` and all inter-stage dataclasses. |
| `networkx` (Phase 1+) | The causal DAG (if/when we add Bayesian reasoning). |
| `cryptography` (Phase 2+) | Ed25519 signatures for the runbook. |
| `httpx` (Phase 4) | LLM API client (if using a hosted model). |

### What we deliberately do not depend on

- **No multi-agent framework** (LangGraph, AutoGen, CrewAI). The
  supervisor is a ~150-line Python module. We are not married to any
  framework's abstractions.
- **No LLM SDK in Phase 1.** The LLM is a Phase 4 future narrator.
  The Phase 1 `Narrator` protocol has a no-op implementation.
- **No OPA binary in Phase 1.** The policy engine is Phase 2.
- **No NATS/Redis in Phase 1.** The bus is an in-process
  `asyncio.Queue`.
- **No ORM / database.** State is in-memory or on disk. Phase 3 may
  add SQLite for the runbook index.

---

## 13. Glossary

**Anomaly** — a per-channel deviation signal from the streaming
detector. In `live/error_stream.py` this is an `AlertEvent`. In
Phase 1 it's re-typed as a `SymptomEvent`.

**Cause** — a named, cataloged hypothesis for *why* an anomaly
pattern is occurring. Each cause has a `symptom_pattern` (what
symptoms must be present) and a `propagation_path` (which other
channels/subsystems are affected).

**Procedure** — a typed, ordered list of steps that address a cause.
Each step has an `action` (from the twin's vocabulary), `params`,
`expected_state`, `abort_on`, and per-step `risk_class`.

**Symptom** — a `SymptomEvent`. The output of Stage 1 (Detect). The
input to Stage 2 (Diagnose).

**Diagnosis** — a ranked list of candidate causes for a sliding
window of symptoms, with the evidence chain attached.

**Proposal** — the chosen cause + chosen procedure + dry-run state
diff + risk class + provenance. The output of Stage 3 (Propose).
The input to Stage 4 (Validate) and (eventually) Stage 5 (Approve).

**Verdict** — the result of running a `Proposal`'s procedure through
the twin. `OK | REJECT(reason) | INCONCLUSIVE(what_we_need_to_know)`.
The output of Stage 4 (Validate).

**Runbook** — the tamper-evident, replayable record of an
anomaly-to-resolution episode. Produced by Stage 7 (Verify).

**Twin** — the deterministic sidecar simulator. Receives a
procedure, returns the predicted state after each step. Never
touches the `CommandBus`.

**Risk class** — one of `A` (read-only), `B` (reversible), `C`
(expensive), `D` (irreversible). Determines the approval route.

**Dry-run** — a state-diff preview of what a procedure will do,
computed by the twin in sandbox mode. Shown to the operator before
they approve a Class C or Class D action.

**Hold-down** — a mandatory waiting period (30s for Class D) after
the last approval signature, during which any operator can panic-
abort the action.

**Merkle chain** — a sequence of hashes where each step's hash
includes the previous step's hash. Detects tampering.

**Content addressing** — storing data by the hash of its content
(SHA-256). The hash *is* the address. Lets the runbook prove what
inputs it saw by referencing the digests.

**Replay** — re-executing a recorded runbook against the same
inputs (catalog versions, model version, twin state) to verify the
output is byte-identical. Detects non-determinism and drift.

**Narrator** — the protocol that an LLM (in Phase 4) plugs into to
write prose around already-structured outputs. The narrator is
structurally barred from authority.

**Approval token** — a signed authorization to execute a
procedure. The only path to which is the policy engine (Phase 2).
The LLM has no path to it.

**Evidence chain** — the sequence of content-addressed blobs
(telemetry, twin state, twin simulation trace, model weights,
policy bundle) that a runbook references. Together with the Merkle
chain over the steps, this is what makes a runbook auditable.

---

*Last updated: when the architecture changes. The Bible is the project's
source of truth. Code may drift; the Bible must not.*
