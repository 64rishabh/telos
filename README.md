# telos

**Autonomous satellite mission ops & anomaly response — a multi-agent pipeline
for detecting, diagnosing, proposing, validating, approving, executing, and
verifying telemetry anomalies with auditable runbooks and a human-in-the-loop
safety model.**

> **Status: pre-implementation planning.** Phase 1 (Detect → Diagnose → Propose
> → Validate) is fully designed in `BIBLE.md` and the working plan at
> `/home/rishabh/.claude/plans/glowing-painting-possum.md`. No Phase 1 code
> has been written yet. The pieces that are *runnable today* are the
> pre-existing `live/` LSTM streaming detector (the Detect stage) and the
> vendored `telemanom/` reference (NASA's original implementation we ported
> from).

---

## What telos is

Satellite operations are telemetry-heavy and unforgiving. Today, anomaly
detection is automated (LSTM-based streaming detectors exist and work) but
everything *after* detection — cross-subsystem diagnosis, recovery proposal,
pre-execution validation, operator approval, command execution, and an
auditable record — is still manual. As constellations scale, this becomes
the bottleneck.

**telos** is a multi-agent system that closes that gap. It treats the
problem as a 7-stage pipeline:

```
Detect → Diagnose → Propose → Validate → Approve → Execute → Verify
```

with the following non-negotiable properties:

- **Human-in-the-loop by default.** Every recovery action requires operator
  approval. The LLM (when one is added in a later phase) is a narrator
  and proposer, never the approval authority.
- **Auditable end-to-end.** Every decision is reproducible from a
  content-addressed evidence chain. Runbooks are replayable.
- **Digital twin as the pre-execution oracle.** No proposed recovery
  procedure runs against real (or simulated) hardware without first being
  validated against a deterministic sidecar twin.
- **Hand-authored knowledge as the source of truth.** Causes and procedures
  are data (YAML), not code. They can be reviewed by ops engineers and
  regulators without touching application logic.

The full design, the current implementation state, and every decision we
took (with the alternatives we rejected) is in **[`BIBLE.md`](BIBLE.md)**.
That file is the project's living reference — read it first if you're new.

---

## Repo layout (today)

```
telos/
├── README.md            ← you are here
├── BIBLE.md             ← the project's living reference (read next)
├── CLAUDE.md            ← project rules for AI agents
├── .gitignore
├── live/                ← the working LSTM streaming anomaly detector
│                          (Phase 1 "Detect" stage — already runnable)
├── telemanom/           ← vendored NASA reference (not edited)
├── models/              ← trained model artifacts (gitignored, regenerated)
└── docs/
    └── decisions/       ← ADRs (architecture decision records)
```

### `live/`

A streaming port of NASA's telemanom. It runs an LSTM over a synthetic
sinusoid telemetry source, applies EWMA-smoothed error detection on the
predictions, and emits `AlertEvent`s when error bursts cross a learned
threshold. This is the **Detect** stage of the pipeline. It is fully
runnable today; Phase 1 wraps it with a `SymptomEvent` re-typing layer and
hands its output to the Diagnose stage.

### `telemanom/`

The original NASA/JPL LSTM-based anomaly detection library
([nasa/telemanom](https://github.com/khundman/telemanom)), vendored for
reference. The thresholding math in `live/error_stream.py` is a direct port
of `telemanom/telemanom/errors.py`. **Not edited** — this is a read-only
attribution copy. Caltech/JPL copyright is preserved in
`telemanom/LICENSE.txt`.

### `models/`

Trained model artifacts. Currently holds `baseline.h5` (gitignored; ~1 MB).
Regenerate with `python -m live.train_baseline`.

### `docs/decisions/`

Architecture decision records (ADRs). Each ADR is a single Markdown file
capturing one design decision, the alternatives considered, and the
constraints that forced the choice. The first ADRs land with Phase 1
implementation; the format follows Michael Nygard's template.

---

## Quick start (today's runnable pipeline)

```bash
# 1. Set up Python environment
python -m venv .venv
source .venv/bin/activate
pip install -r live/requirements.txt

# 2. Train the baseline LSTM (one-time, ~minutes)
python -m live.train_baseline
# → writes models/baseline.h5

# 3. Boot the live demo (dashboard + websocket + anomaly inject)
python -m live.sim
# → http://localhost:8000/  (dashboard)
# → ws://localhost:8000/stream  (live alerts)
# → POST /inject?type=shift&magnitude=2.0  (inject an anomaly)

# 4. Run the existing tests
pytest live/tests/
```

That's what works today. Everything else described in `BIBLE.md` is in
plan, not yet in code.

---

## Roadmap at a glance

| Phase | Scope | Status |
|---|---|---|
| **1. Detect → Diagnose → Propose → Validate** | Build the first 4 stages. Hand-authored cause + procedure catalogs. Deterministic twin. | **Designed, not yet implemented** |
| **2. Approve → Execute** | OPA/Rego policy gate, 4-class risk scheme, 2-person rule, hold-down timer, mock command bus, per-step receipts. | Designed, deferred |
| **3. Verify** | Merkle-chained runbook emitter, content-addressed evidence store, replay harness, retention hooks, drift detection. | Designed, deferred |
| **4. LLM narrator + RAG** | LLM as Diagnose/Propose narrator (no authority). RAG over past runbooks for novel-cause proposals. | Designed, deferred |

The detailed design of every phase, the current implementation status of
every stage, and the rationale for every decision is in `BIBLE.md`.

---

## Project conventions

- **Modular changes.** Every new function gets tests. See `CLAUDE.md`.
- **Worktree isolation.** Multi-agent development uses git worktrees.
- **ADRs in `docs/decisions/`.** Every architectural decision lands as an
  ADR before the code that implements it.
- **No silent LLM authority.** LLM features (when they land in Phase 4) are
  narrators and proposers, not decision-makers.
- **`BIBLE.md` stays current.** When you change architecture, update the
  Bible in the same commit. The Bible is the project's source of truth for
  "what is telos and why."

---

## License

Project code: [MIT](LICENSE) (Copyright © 2026 Rishabh).

`telemanom/` retains its original Caltech/JPL copyright. See
`telemanom/LICENSE.txt`.

---

## Contributing

Read `BIBLE.md` first. Then read `CLAUDE.md`. Then open an issue or
draft an ADR in `docs/decisions/` describing what you want to change and
why.
