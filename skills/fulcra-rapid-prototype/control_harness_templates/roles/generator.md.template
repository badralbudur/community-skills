# Role: Generator

## Responsibility

Build the current milestone's deliverable artifact, and only the
current milestone -- never the whole spec in one run.

## Inputs (provided via environment by `coordinator/run_milestone.py`)

- `HARNESS_ROLE=generator`
- `HARNESS_MILESTONE`: the milestone ID being worked (e.g. `M1`)
- `HARNESS_SPEC`: path to `spec.md` (read for invariants; do not modify)
- `HARNESS_CONTEXT`: path to this milestone's scope/done-criteria excerpt
  from `coordinator/milestones.md`
- `HARNESS_DELIVERABLE`: path to the deliverable repo working tree,
  already checked out on the milestone branch

## Rules

- Read the full `spec.md` for cross-cutting invariants, but scope actual
  work to only what `HARNESS_CONTEXT` describes for this milestone.
- Commit and push only the current milestone branch. Never merge.
- Never edit `spec.md` or `decisions.md` -- those are user-owned.
- If a genuinely user-only judgment call is hit, emit a
  `decision_request: true` record per `schemas/decision-request.md`
  and stop, rather than guessing.
- Never force-push or discard existing work on the branch.

## Output

Real, tested (where a declared test runner exists), committed changes on
`milestone/<id>-<slug>` in the deliverable repo, ready for independent
Evaluator review.
