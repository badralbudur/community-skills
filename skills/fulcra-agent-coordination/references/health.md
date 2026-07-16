# Health

When a team is healed by scheduled reconciles across several machines, the operational questions are *"is
anything actually running?"* and *"which host went dark?"* — health answers them deterministically. The
operator digest and the role-escalation sweep live here too.

## How it works

- Every `reconcile` writes a small **health shard** (`_coord/health/<host-key>.json`: host, timestamp,
  engine version, task count, warnings) and prunes shards older than 30 days (age-based GC).
- **`health <team>`** folds the shards: per-host last-reconcile age, STALE flag (>24h), engine version.
  Exits non-zero when no host is fresh, which makes it usable as a monitor probe.
- **`doctor [team]`** is the local preflight: storage CLI on PATH, File Store reachable, engine version.
  Run it after installing the skill and inside scheduled jobs' self-tests — silent heartbeat failure
  usually reduces to one of these three.

## Commands

```bash
<skill-dir>/scripts/coord-engine doctor <team>            # preflight; exit 0 = healthy
<skill-dir>/scripts/coord-engine health <team> [--json]   # fleet fold; exit 1 if no fresh reconciler
<skill-dir>/scripts/coord-engine digest <team> [--human H] [--json] [--store] [--emit-timeline]
<skill-dir>/scripts/coord-engine escalate <team>          # vacancy sweep (heartbeat-safe)
```

## Operator digest

`digest <team>` folds the reconcile aggregate + [presence](presence.md) into the four operator questions:

- **blocked on you** — `needs:human` tags, or tasks assigned to your handle (env `FULCRA_COORD_HUMAN`)
- **upcoming** — `not_before` within 7 days
- **agents** — liveness + open work each
- **stale** — active tasks untouched > 48h

`--store` persists it to `_coord/digests/<date>-<window>.md`, deduped per day+window (morning/evening), so
it is heartbeat-safe. `--emit-timeline` additionally emits the digest as a moment on the "Agent Tasks —
Digest" timeline track; the record id is deterministic per window, so fleets and retries converge on one
record rather than minting duplicates, and a failed emit retries on the next tick.

## Role-vacancy escalation

`escalate <team>` sweeps every role doc: if a role is VACANT past its `sla_hours` and today's marker
doesn't exist, it writes the marker and files a **P1 directive to the role's `maintainer`** ("claim it or
reassign"). Idempotent per day. See [roles](roles.md) for the underlying status fold and the dormancy
escape hatch.

## When to use

- After installing the skill on a new machine (`doctor`).
- In monitoring/heartbeat wrappers (`health --json`; alert on `healthy: false`).
- Diagnosing "the index looks stale" — `health` shows which reconciler stopped.

**User's call:** the reconcile cadence and which hosts run it, whether the digest is stored or piped
somewhere, and whether `escalate` runs on a schedule or by hand. The engine takes no position; it only
reports what the schedule you chose actually did.
