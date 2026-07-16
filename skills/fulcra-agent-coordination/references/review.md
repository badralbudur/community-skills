# Review

A lightweight **review handshake** over a teams space: an author requests review of an artifact, one or
more reviewers leave verdicts, and the overall state is folded deterministically. Requesting a review and
reading the tally are engine commands; leaving a verdict is a single-file write. Folding multiple reviewers
is derived state — code, not eyeballing.

## Layout (under `team/<team>/review/<slug>/`)

**`review/<slug>.md`** — the review request, written by `review request`. OKF `type: Review`. `<slug>` is a
short id for the artifact (e.g. `pr-42`). The `required` list is what the tally gates on — name **roles**
where you can (`reviewer`, `security`), so the obligation follows whoever holds the role rather than a
named session:

```yaml
---
type: Review
schema: review-request/v1
requested_by: alice
of: https://github.com/org/repo/pull/42
required: [reviewer, security]   # all must approve for APPROVED (a "a, b" string also parses)
---
Review requested: <artifact>
```

**`review/<slug>/verdicts/<required-token>.md`** — one verdict per requirement. The **filename stem is the
tally key** and must equal a `required` token (the role, or the direct agent name), not the holder's own
name. OKF `type: Verdict`:

```yaml
---
type: Verdict
reviewer: bob               # who signed off (informational — the filename drives the tally)
verdict: approve            # approve | changes
---
Notes / requested changes.
```

Verdict synonyms accepted: `approve|approved|lgtm` and `changes|request-changes|reject|rejected`.

## Lifecycle

### 1. Request (author)

One command, not a hand-written doc:

```bash
<skill-dir>/scripts/coord-engine review request <team> pr-42 \
    --of https://github.com/org/repo/pull/42 \
    --reviewer reviewer --reviewer security     # roles preferred; repeat --reviewer for many
```

This writes `review/<slug>.md` at the exact path the tally reads and echoes the verdict path each required
reviewer must fill. The request doc IS the durable obligation: it surfaces in every required reviewer's
`needs-me` as a pending marker and stays there until that reviewer's verdict file exists — no inbox message
to remember, and no way for a dropped review to gate on nothing.

Re-running the same request is safe: an identical `<slug>`/`--of`/reviewer set is **idempotent recovery**
(it re-delivers any reviewer notice a prior partial failure dropped, and converges). A request that changes
`--of` or the reviewer set is refused (exit 1) — a changed review is a **new slug**, never an overwrite of
the old one.

### 2. Verdict (reviewer)

Write the verdict file at the **exact path `review request` echoed** for you, with `verdict:
approve|changes` and notes, then drop a message into the author's inbox. The **filename stem is what the
tally matches against the `required` token** — not the frontmatter `reviewer:` field — so name the file
after the requirement, not after yourself:

- **role requirement** (`required: reviewer`) → `review/<slug>/verdicts/reviewer.md`, whoever you are.
  Writing `verdicts/bob.md` records an approval the tally can't credit: `reviewer` stays in
  `pending_required` and the review can never reach APPROVED.
- **direct requirement** (`required: bob`) → `review/<slug>/verdicts/bob.md`.

```bash
# type: Verdict, verdict: approve|changes  (filename = the required token, e.g. reviewer.md)
uv tool run fulcra-api file upload /tmp/verdict.md \
  "team/<team>/review/pr-42/verdicts/reviewer.md"
# then notify the author's inbox (same teams lifecycle).
```

To change your mind, re-upload the same file (overwrites; the File Store keeps the history).
**Fail-closed:** a `changes` verdict keeps blocking until that same file is re-uploaded as `approve` —
pushing a fix does **not** clear it; the requirement must be re-affirmed.

### 3. Check state (anyone)

Deterministic fold — do not tally by hand:

```bash
<skill-dir>/scripts/coord-engine review status <team> pr-42 --json
# -> {state, approvals, changes, required, pending_required}
```

- **CHANGES** — any reviewer requested changes (a single blocker dominates).
- **APPROVED** — ≥1 approval, no outstanding changes, and every `required` reviewer approved.
- **PENDING** — otherwise (no verdicts yet, or required reviewers haven't voted).
- **exit 1** — the review doc is unreadable (transport failure or nonexistent slug). The tally is
  *unknown, retry*, not a state.

**Fail loud on an unreadable tally.** Without the `required` list a lone approval would fold to a clean
APPROVED and durably hide a pending review, so a watcher must treat exit 1 as *transport down, retry* —
never as a settled state, and never fold a missing doc into APPROVED.

## When to use

- Gating a merge/land on review in a multi-agent team.
- Any "N reviewers must sign off" flow where you need an unambiguous, non-drifting verdict state.

**User's call:** who the required reviewers are, whether they're roles or named agents, and what a review
gates in your process. The engine computes the tally; it does not decide your merge policy.
