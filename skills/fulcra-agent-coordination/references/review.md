# Review

A lightweight **review handshake** over a teams space: an author requests review of an artifact, one or
more reviewers leave verdicts, and the overall state is folded deterministically. Requesting a review and
reading the tally are engine commands; leaving a verdict is a single-file write. Folding multiple reviewers
is derived state — code, not eyeballing.

## Layout (under `team/<team>/review/<slug>/`)

**`review/<slug>.md`** — the active review round, written by `review request`. OKF `type: Review`. `<slug>` is a
short id for the artifact (e.g. `pr-42`). The `required` list is what the tally gates on — name **roles**
where you can (`reviewer`, `security`), so the obligation follows whoever holds the role rather than a
named session:

```yaml
---
type: Review
schema: review-request/v2
requested_by: alice
of: https://github.com/org/repo/pull/42
required: [reviewer, security]   # all must approve for APPROVED (a "a, b" string also parses)
head: aaaaa...                   # exact 40- or 64-hex artifact head
round: 1
---
Review requested: <artifact>
```

**`review/<slug>/verdicts/<head>--<required-token>--<timestamp>-<digest>.md`** — append-only verdict
evidence for one exact head. The required token in the filename is the tally key (the role, or the direct
agent name), not necessarily the holder's identity. OKF `type: Verdict`:

```yaml
---
type: Verdict
reviewer: bob               # who signed off (informational — the filename drives the tally)
requirement: reviewer       # role/token represented by this signer
head: aaaaa...              # must match the active request head
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
    --head <exact-commit-sha> \
    --reviewer reviewer --reviewer security     # roles preferred; repeat --reviewer for many
```

This writes `review/<slug>.md` at the exact path the tally reads and echoes the verdict path each required
reviewer must fill. The request doc is the durable obligation: it surfaces in every required reviewer's
`needs-me` as a pending marker and stays there until that reviewer's verdict file exists — no inbox message
to remember, and no way for a dropped review to gate on nothing.

Re-running the same request and head is idempotent recovery. Re-running the same slug, artifact, requester,
and reviewer set with a **new exact head** advances the request to the next round. Prior verdict shards are
never deleted; status reads only the active head. A changed artifact or reviewer set still requires a new
slug.

### 2. Verdict (reviewer)

Use the verdict command. It validates the active register head, then writes a uniquely named append-only
record, so concurrent reviewers and corrections cannot overwrite evidence:

```bash
<skill-dir>/scripts/coord-engine review verdict <team> pr-42 \
    --head <exact-commit-sha> --verdict approve \
    --as reviewer --from bob --note "Checked the retry behavior."
```

To change your mind, file another verdict. The newest timestamped record for that requirement wins and the
older record remains auditable. Pushing a fix alone never clears `changes`; the requirement must be
re-affirmed against the active exact head.

### Historical compatibility

Headless `review-request/v1` documents and mutable `verdicts/<required-token>.md` files remain readable.
They are compatibility inputs, not the recommended authority for new reviews. A keyed shard under a
headless review, or a shard whose frontmatter claims a different active head, is reported as uncounted and
causes a degraded (exit 3) status instead of silently appearing as “reviewer has not voted.”

### 3. Check state (anyone)

Deterministic fold — do not tally by hand:

```bash
<skill-dir>/scripts/coord-engine review status <team> pr-42 --json
# -> {state, approvals, changes, required, pending_required, head, round}
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
