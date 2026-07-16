# Directives

Teams' native inbox is a drop-zone of markdown files. Directives adds **structured directed work**: a
directive IS a task with an `assignee`, so it shows up in the reconcile views, carries a priority and a
status machine, and has a deterministic per-agent **inbox** with **acks** (acking hides an item for you and
stops re-notify) — without replacing the teams inbox for freeform messages.

All verbs create/read ordinary Task docs (`team/<team>/task/<slug>.md`). Run
`<skill-dir>/scripts/coord-engine reconcile <team>` — or let the heartbeat do it — to refresh the inbox
aggregate.

## Verbs

```bash
<skill-dir>/scripts/coord-engine tell      <team> <assignee> "<title>" [-p P0..P3] [-s "…"] [-n "…"] [--from <me>]
<skill-dir>/scripts/coord-engine broadcast <team> "<title>" [flags]              # assignee '*'
<skill-dir>/scripts/coord-engine remind    <team> <assignee> <when> "<title>"    # when: ISO | 5d | 36h | 10m
<skill-dir>/scripts/coord-engine later     <team> "<title>"                      # backlog (@backlog)
<skill-dir>/scripts/coord-engine handoff   <team> <slug> --to <agent> [--checkpoint CHK-…] [-n "…"]
<skill-dir>/scripts/coord-engine inbox     <team> --agent <X> [--json] [--all]   # --all includes @backlog
<skill-dir>/scripts/coord-engine inbox     <team> --agent <X> --ack <slug>
<skill-dir>/scripts/coord-engine respond   <team> <slug> --outcome "…" [-e "…"] [--agent <X>]
```

- `tell` — direct work at one agent.
- `broadcast` — assignee `*`; reaches every non-stale agent on the [presence](presence.md) roster.
- `remind` — hidden until WHEN.
- `later` — backlog capture; `inbox --all` surfaces it.
- `handoff` — atomic: assignee + checkpoint ref in one write.
- `respond` — record a response and close the loop.

## How the deterministic parts work

- **Inbox fold** (engine): open tasks assigned to you or `*`, minus your acks
  (`_coord/acks/<slug>/<agent-key>.md`, one file per agent — collision-safe key), gated on `not_before`,
  priority-sorted. Served O(1) from the reconcile aggregate (`acked_by` is folded in at reconcile time;
  freshness is bounded by the reconcile cadence).
- **Broadcast completion**: with [presence](presence.md) in use, a `*` directive is complete when every
  non-stale roster agent has acked. Without presence, acking still hides per-agent — a documented
  degradation, not a failure.
- **Re-notify**: unacked P0/P1 directives keep surfacing (inbox top, digest) until acked. An ack is a
  deliberate act; a mis-fired ack permanently silences that item for you.
- **Handoff is atomic**: the checkpoint ref and the new assignee land in ONE task-file write, so there is
  no window where the work moved but the resume state doesn't exist. Pairs with
  [continuity](continuity.md).
- **Shard-GC**: reconcile prunes ack shards whose task no longer exists, orphan-proofing the ack dir. It
  only deletes ack shards that are datable AND older than 24h AND whose task is absent from a non-empty
  listing.

## Ack shard

`team/<team>/_coord/acks/<slug>/<agent-key>.md`:

```yaml
---
type: Ack
agent: claude-code:host:repo
timestamp: 2026-07-02T12:00:00Z
---
```

The filename key is collision-safe (`slug+sha1[:6]`); reconcile trusts the frontmatter `agent:` only when
it round-trips to the filename. Response shards live at `_coord/responses/<slug>/<stamp>.md`.

If an agent's raw id changes, its `agent_key` changes and old acks stop applying — it gets re-notified
under the new identity. Intentional; see [presence](presence.md) on picking a durable id.

## Fail-closed notes

- `respond` records the response shard first, then closes the task (done, evidence = outcome). If the close
  is an illegal transition, the response is still recorded and the failure reported.
- `respond` performs no assignee authorization — anyone on the team can close a directive. The File Store
  write ACL is the trust boundary.
- A `remind` with an unparseable WHEN errors; it never creates a directive that fires at the wrong time.

**User's call:** your priority ladder (what P0 means to your team), whether unattended agents run an
operator loop that drains the inbox, and whether directives or the freeform teams inbox is the right
channel for a given message.
