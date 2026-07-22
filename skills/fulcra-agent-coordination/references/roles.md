# Roles

A team's `member/<agent>/role.md` says what a *member* does, but teams has no notion of a **durable role**
that outlives any one session — "who is the reviewer right now?", "is anyone on-call?", "this role has
been unattended too long." Roles adds that, as a pure OKF-markdown convention over the team namespace:
lease mechanics via `roles` verbs, everything else plain `fulcra-api file` plus the OKF standard.

## Concepts

- **Role** — a named, durable function in the team (e.g. `reviewer`, `maintainer`, `on-call`). Defined
  once; sessions come and go.
- **Lease** — an agent's claim on a role, refreshed to prove liveness. A role is *held* while a fresh lease
  exists.
- **Policy** — `shared` (many holders allowed) or `exclusive` (one holder; a second fresh lease is a
  contention signal).
- **SLA / escalation** — if a role sits vacant longer than `sla_hours`, its `maintainer` is notified.

**User's call:** which roles a team defines, each role's `policy`, its `sla_hours`, and who maintains it.
There is no built-in role vocabulary and no default org chart.

## Layout (under `team/<team>/roles/`)

**`roles/<name>.md`** — the role registry doc, OKF `type: Role`, created once when the role is
established. Frontmatter carries policy and SLA:

```yaml
---
type: Role
title: Reviewer
description: Adversarial code/plan review for the team's PRs.
policy: shared            # shared | exclusive
sla_hours: 24             # vacancy longer than this escalates
maintainer: alice         # who gets the escalation (an agent or member name)
---
# Duties
- Pick up review requests from the team inbox…
```

**`roles/<name>/leases/<slug>-<hash6>.md`** — one lease per holder, named by the engine from the holder's
id (`agent_key`); never hand-name lease files. OKF `type: Lease`. The `timestamp` is the liveness signal —
refresh it (re-claim) each time you act in the role:

```yaml
---
type: Lease
title: reviewer lease — bob
agent: bob
timestamp: 2026-07-01T18:00:00Z
---
Holding the reviewer role. Next: drain the review inbox.
```

**`roles/<name>/escalations/<YYYY-MM-DD>.md`** — a first-writer-wins daily marker so a vacant role
escalates at most once per day.

## Lifecycle

### Establish a role (once)

Write `roles/<name>.md` with `type: Role` plus policy/SLA/maintainer. The engine folds role status from the
`roles/` directory listing, so a `roles/index.md` is optional human courtesy, not a requirement.

```bash
uv tool run fulcra-api file upload /tmp/reviewer.md "team/<team>/roles/reviewer.md"
```

### Claim / hold

```bash
<skill-dir>/scripts/coord-engine roles claim <team> reviewer [--agent <your-id>]
```

Writes your lease shard (engine-named `<slug>-<hash6>.md`; the command echoes the filename). **Re-run it**
whenever you do work in the role — the refreshed `timestamp` is what keeps the role "held". Never
hand-upload a lease file: a hand-named shard makes a second lease for your id, which reads as a spurious
`CONTESTED` on exclusive roles. The Fulcra File Store versions every write, so the lease's history is an
audit trail of your tenure.

### Release

```bash
<skill-dir>/scripts/coord-engine roles release <team> reviewer [--agent <your-id>]
```

Deletes your engine-named shard. Intentional and not undoable — correct for releasing. A raw `fulcra-api
file delete` of a hand-guessed filename silently misses the real shard; the lease then goes stale instead
of released.

### Determine role status (the fold) — use the engine, do not eyeball timestamps

Classifying a role from many lease files is a *fold* over derived state: two agents must agree on whether a
role is vacant before one escalates. Comparing timestamps by hand gives different answers to different
readers, so this is a deterministic command:

```bash
<skill-dir>/scripts/coord-engine roles status <team> reviewer --json
# -> {status, policy, sla_hours, holders, fresh_holders, escalation_due}
```

It reads the role's `policy`/`sla_hours`, folds the leases, and returns:

- `status` — **HELD** (≥1 fresh lease) / **VACANT** (none) / **DORMANT** (vacant but deliberately parked —
  see [Park a role](#park-a-role-dormancy)) / **CONTESTED** (`exclusive` + ≥2 fresh) / **UNKNOWN**
  (unreadable)
- `fresh_holders`
- `escalation_due` — true iff vacant past SLA, not parked, and today's marker isn't present

Resolve **CONTESTED** by having all but one holder release.

Freshness window: a lease is fresh if its `timestamp` is within the role's `sla_hours` (default 24h).

### Role-as-identity

When a session exists to serve one role, use the role name as its agent identity
(`FULCRA_COORD_AGENT=release-reviewer`) — see [presence](presence.md), "Pick your identity by role". Claim
the role's lease while you act as it. Know what each guard does and does not catch:

- **Different ids claiming an exclusive role** (e.g. `release-reviewer` and a stray
  `session-b7`): two fresh lease shards within `sla_hours` make `roles status` report
  `CONTESTED`. A stale stray shard yields `HELD` instead. This case is detected.
- **Two sessions under the same id string**: they write the same lease shard (shard names derive from the
  id), so leases alone cannot see this — the last write silently wins. The engine detects it via a session
  nonce: every `roles claim` writes a nonce into the lease and compares on refresh. A foreign nonce prints
  a warning to stderr ("nonce mismatch … same-id double-acting"); claiming with no local state over an
  existing shard prints a takeover note. Heed those. The manual fallback, in this order at the start of
  every work burst:
  1. `roles status <team> <role> --json` — proceed only if the status is `VACANT` or the sole holder is your id.
  2. Read your lease shard raw (`fulcra-api file download team/<team>/roles/<role>/leases/<agent-key>.md`
     — learn your `<agent-key>` by listing the leases dir, or from `presence beat` output, which prints the
     same key) and compare its `timestamp` to when you last claimed. A fresher timestamp you did not write
     means another session is acting under your id.
  3. Only then re-claim to refresh. Re-claiming before reading destroys that evidence.

Multi-host variants (`release-reviewer@host1`, `@host2`) are acceptable when one role legitimately runs in
several places — each host claims the one role (`roles claim <team> release-reviewer --agent
release-reviewer@host1`), never a role named after the variant. Such a role needs `policy: shared`; on
`exclusive` it would sit in permanent `CONTESTED` by construction. Note that `shared` trades away the
`CONTESTED` collision guard for that role. Keep the role doc's `maintainer:` field a distinct supervising
identity: vacancy escalations are assigned to that field, so pointing it at the role itself mails the alert
to the very inbox that just went dark.

### Escalate a vacancy — engine decides, you act

When `escalation_due` is true, perform the single-file actions (reliable as prose):

```bash
# 1. first-writer-wins daily marker (dedupe)
uv tool run fulcra-api file upload /tmp/escalation.md \
  "team/<team>/roles/reviewer/escalations/$(date -u +%Y-%m-%d).md"
# 2. notify the maintainer via the teams inbox lifecycle
uv tool run fulcra-api file upload /tmp/notice.md \
  "team/<team>/member/<maintainer>/inbox/$(date -u +%Y%m%d-%H%M%S)_<you>_role-vacant-reviewer.md"
```

State which role is vacant and for how long. [`escalate`](health.md#role-vacancy-escalation) does this
sweep for every role in one command if you'd rather not hand-roll it.

### Park a role (dormancy)

To deliberately leave a role unattended without alarming — a reviewer on leave, a seasonal on-call — set
`dormant_until: <ISO-8601>` in the role doc's frontmatter. While that timestamp is in the future the engine
treats the role as **DORMANT**: `roles status` prints `DORMANT (until <ts>)` instead of VACANT and the
vacancy escalation is suppressed. Escalation resumes automatically once the date passes (past-or-absent
`dormant_until` = normal behavior), and a live lease outranks the park (a held-and-dormant role still shows
HELD). An unparseable `dormant_until` fails **open** — treated as absent, a note is printed, and escalation
still fires — so a typo can never silently mute a role. Unpark early by deleting the field.

## When to use

- Establishing "someone owns X" in a team without pinning it to one session.
- Routing work by role ("the reviewer") instead of by name.
- Making sure a critical function (on-call, maintainer) is never silently unattended.

## Efficiency (per the teams OKF directive)

If you keep an optional `roles/index.md`, do **not** index every lease or escalation marker — describe the
`leases/` and `escalations/` directories as a whole. Keep the team `log.md` for role *creation* and
*handoff* milestones, not every lease refresh.
