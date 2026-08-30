# Architecture Decision Records (ADRs)

This directory holds the project's ADRs. Each ADR is a single Markdown
file capturing one design decision, the alternatives considered, and
the constraints that forced the choice.

## Convention

- Filename: `NNNN-short-slug.md` (4-digit zero-padded number, e.g.
  `0001-supervisor-architecture.md`).
- Format: [Michael Nygard's template](https://github.com/joelparkerhenderson/architecture_decision_records/blob/main/locales/en/templates/decision-record/index.md)
  (Context, Decision, Consequences).
- Status: `Proposed` → `Accepted` → `Superseded` (or `Deprecated`).
- One ADR per decision. Don't bundle unrelated decisions.

## Process

1. Open a PR with the new ADR in `docs/decisions/`. Status: `Proposed`.
2. Discuss in the PR. Iterate.
3. Once accepted, mark status `Accepted` and merge.
4. Update `BIBLE.md` §4 in the same commit to reflect the decision.
5. The code that implements the decision lands in a follow-up PR.

## Index

| # | Title | Status | Summary |
|---|---|---|---|
| — | _(no ADRs yet)_ | — | The first ADRs land with Phase 1 implementation. |

## Summaries (also live in BIBLE.md §4)

The Bible's [§4 Decisions we took](../BIBLE.md#4-decisions-we-took-and-the-alternatives-we-rejected)
is the *summary*; the ADRs here are the *full record*. Keep both in sync.
