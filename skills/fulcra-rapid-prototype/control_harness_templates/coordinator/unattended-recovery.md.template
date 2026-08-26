# Unattended Recovery

Applies when the Coordinator runs without a human watching every step
(e.g. via a scheduler). Pairs with a delayed verifier on the same
cadence -- see README.md's operational invariants and
HARNESS_GOVERNANCE.md's "Interrupted work" section for the underlying
rules this operationalizes.

## What the delayed verifier checks

- Scheduler run history (did the Coordinator invocation actually
  execute, not just get queued).
- Fulcra Workspace state (status, verdicts, decisions, escalations,
  milestone-progress.md) -- not chat history, not a scheduler
  "completed" flag alone.
- Deliverable branch/PR state (does the expected branch/PR exist and
  match what Workspace state claims).
- Dashboard evidence, if a dashboard is in use.

## What the delayed verifier may do

- Repair control-harness/branch/worktree state through the safe
  recovery protocol in HARNESS_GOVERNANCE.md (resume dirty work on the
  current milestone branch in place; stash-and-record unrelated dirty
  work on another branch; never force-push or discard source work).
- Prove one clean Coordinator preflight afterward (a dry run confirming
  the harness is in a runnable state again).

## What the delayed verifier must NOT do

- Directly repair deliverable code or tests itself.
- Treat a provider/session/rate-limit failure as a real blocker --
  these are transient capacity issues: retain the evidence, allow a
  later scheduled retry after the limit resets.
- Silently mark status/dashboard as current after a failed durable
  upload -- surface a critical visibility failure instead.

## Genuine blockers (not transient) that must pause, not retry

- A real spec/decision ambiguity (emit `decision_request: true` per
  `schemas/decision-request.md`).
- Evaluator FAIL that isn't a scoped, known later-milestone gap.
- Repeated failure of the same kind across retries within
  `coordinator/policy.md`'s bounded retry count -- per this skill's
  "fix the process, not one output" invariant, this should prompt a
  prompt/schema/permission/scope review, not another blind retry.
