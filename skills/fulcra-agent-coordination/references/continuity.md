# Continuity

Teams already uses `member/<agent>/progress.md` to survive isolated cron/heartbeat runs, but it's
freeform — a fresh session has to re-read prose and guess what mattered. Continuity adds a **structured**
snapshot (objective, decisions, next actions, open questions, artifacts, context-used-%) and a
**deterministic resume brief**, so waking up is reliable instead of a re-read.

Whether and when to snapshot is a judgment call. Building the schema and folding many snapshots to the
newest is deterministic — that part is the engine's.

## Snapshot schema

`member/<agent>/continuity/<task>/latest.json`:

```json
{ "schema": "coord.teams.continuity.v1",
  "checkpoint_id": "CHK-<iso>-<task>",
  "agent": "alice", "task": "build-search",
  "objective": "ship the continuity layer",
  "decisions": ["chose structured json over freeform"],
  "next_actions": ["land the PR", "write the skill"],
  "open_questions": ["fold across tasks or per-task?"],
  "artifacts": ["https://github.com/org/repo/pull/5"],
  "context_used_percent": 40, "transcript_path": null,
  "created_at": "2026-07-01T18:00:00Z" }
```

## Snapshot

```bash
<skill-dir>/scripts/coord-engine continuity snapshot <team> <agent> <task> --objective "…" \
    [--next "…"]...            # repeatable
    [--decision "…"]...        # repeatable
    [--open-question "…"]...   # repeatable
    [--artifact "…"]...        # repeatable (links to deliverables)
    [--context-percent 40]     # how full your context was at snapshot time
    [--transcript <path>]
# writes team/<team>/member/<agent>/continuity/<task>/latest.json (versioned by the File Store)
```

One `latest.json` per task; re-snapshotting overwrites it and the File Store keeps prior versions.

## Resume

```bash
<skill-dir>/scripts/coord-engine continuity resume <team> <agent> <task>          # one task's latest
<skill-dir>/scripts/coord-engine continuity resume <team> <agent>                 # newest across tasks
<skill-dir>/scripts/coord-engine continuity resume <team> <agent> <task> --json   # raw snapshot JSON
```

The brief lists objective, next actions, open questions, recent decisions, and artifacts — deterministic,
so a fresh session or cron run re-establishes state without re-reading prose. With no `<task>`, `resume`
folds to the newest snapshot by `created_at` across the agent's tasks.

## Role checkpoints, park, and briefing

```bash
<skill-dir>/scripts/coord-engine continuity checkpoint <team> --role <r> [--ref PATH]
<skill-dir>/scripts/coord-engine continuity park <team> [--agent X] [--objective "…"] [--next "…"]
<skill-dir>/scripts/coord-engine briefing <team> [--agent X] [--json]
```

- `checkpoint` gets/sets a role's durable resume point.
- `park` is the session-exit verb: each role you hold (fresh lease) gets a snapshot and the role doc's
  `checkpoint_ref` points at it — the next holder, or your next session, resumes from there via
  `checkpoint --role`. See [roles](roles.md).
- `briefing` is the session-start verb: presence + board + inbox + needs-me + latest snapshot in ONE call.
  It **tolerates absent layers** — with no presence or directives in use the sections are simply empty; it
  never fails a cold start.

## When to use

- **Before context runs low**, or at a natural stopping point — capture what you'd need to resume.
- **On session end / hand-off** — the next session, or another agent picking up the work, resumes clean.
- **In a cron/heartbeat wake payload** — call `continuity resume` first to re-establish state, exactly as
  teams asks agents to read `progress.md` first, but structured.

**User's call:** the snapshot cadence, and whether a session ends with `park` or a bare `snapshot`. Both are
supported; neither is enforced.

Pairs with the teams skill's MEMORY.md / heartbeat conventions: keep those, and add a structured snapshot
for the work in flight.

Schema id: `coord.teams.continuity.v1`.
