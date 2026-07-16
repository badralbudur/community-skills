# Presence

Teams knows who its *members* are (prose in `index.md`) but not who is **alive right now**. Presence adds
a heartbeat plus deterministic liveness folds — the roster that [directives'](directives.md) broadcast
semantics, the operator digest, and [role-vacancy escalation](roles.md) all build on.

Without presence the rest still works; broadcasts just degrade to "acked-by-me hides it for me".

## How it works

**Beat** (single-file write, safe as a command): `presence beat` writes/refreshes your shard
`team/<team>/presence/<agent-key>.md` (collision-safe key; OKF `type: Presence` — agent, workstreams,
summary, timestamp).

**User's call:** when to beat. A reasonable default is on start of work, on heartbeat/cron ticks, and when
your focus changes. Beating more often costs one small write; beating less makes the roster lag.

**Folds** (deterministic, engine-side — never eyeball timestamps):
- `presence show` — roster with `live` (<1h) / `idle` (<24h) / `stale` per agent.
- `agents` — cross-agent digest: each agent's liveness, summary, and open work by status (union of
  presence ∪ task owners/assignees from the reconcile aggregate).

## Commands

```bash
<skill-dir>/scripts/coord-engine presence beat <team> [--agent X] [-w workstream]... [-s "one-liner"]
<skill-dir>/scripts/coord-engine presence show <team> [--json]
<skill-dir>/scripts/coord-engine agents <team> [--json]
```

`--agent` defaults to `$FULCRA_COORD_AGENT` (or a derived host id). Stale shards drop out of the presence
fold's `[live]` view by age; the shard FILES are not garbage-collected (reconcile's GC covers ack and
health shards only). A stale agent reappears by beating again.

## Shard shape

`team/<team>/presence/<agent-slug>.md`:

```yaml
---
type: Presence
agent: claude-code:host:repo
workstreams: [web, api]
summary: draining the review queue
timestamp: 2026-07-02T12:00:00Z
---
```

Liveness bands: live ≤1h, idle ≤24h, stale >24h (undatable = stale). The broadcast roster (used by
directives) = everyone not stale.

## Pick your identity by ROLE, not by folder

Set `FULCRA_COORD_AGENT` to the role you are acting as (`docs-maintainer`, `release-reviewer`), not a
host/cwd-derived string. Folder-derived ids collide the moment two sessions share a directory (shared
inbox, clobbered presence, ambiguous acks) and rot when a hostname or checkout path changes; a role-based
id survives both and is what teammates actually want to address.

Two rules make it safe:

1. **Claim the role's lease while you act as it** (`roles claim <team> <role>`; see [roles](roles.md)). An
   `exclusive` role turns two sessions acting as the same role under DIFFERENT ids into a visible
   CONTESTED state. It cannot see two sessions sharing one id string (same lease shard, last write wins) —
   see the roles reference's guard matrix for what covers that case.
2. **Session/host details are metadata, not address.** Put them in the presence `-s` summary or the lease
   body if useful; never in the agent id.

The derived host id remains only as a fallback for throwaway sessions that never take assignments — and it
is per-HOST, not per-session (`coord-reconcile:<hostname>`), so two env-less sessions on one machine still
share an id and clobber each other's shards. Any session that acts on the team should set an explicit role
id.

**User's call:** which roles exist and how you name them. The skill enforces no vocabulary.
