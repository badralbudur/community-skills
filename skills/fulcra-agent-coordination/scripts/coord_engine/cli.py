"""CLI for coord-engine — the shared coord engine.

    coord-engine reconcile <team>
    coord-engine status    <team> [--json]
    coord-engine board     <team> [--json]
    coord-engine needs-me  <team> --agent <id> [--json]
    coord-engine search    <team> <query> [--json]
    coord-engine roles status <team> <role> [--json]

Command functions take an injected ``transport`` so they're testable without the
network; ``main`` builds the real ``FulcraFileTransport``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import secrets
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from . import (
    aggregate, budget as budget_mod, config, continuity, continuity_audit,
    digest as digest_mod, directives, health as health_mod, okf, presence,
    query, review, roles, tasks,
)
from .budget import Deadline
from . import reconcile as rec
from .log import get_logger
from .transport import FulcraFileTransport, TransportError

__all__ = ["main"]

_log = get_logger("cli")

# Cohesive command groups extracted into focused modules (behavior-preserving
# split). Each imports ``cli`` and reaches shared helpers through it, so there is
# no module-load cycle and ``monkeypatch.setattr(cli, …)`` still steers. Their
# public names are re-exported at the BOTTOM of this module (after every helper is
# defined) so ``build_parser``'s dispatch table and existing ``cli.<name>`` call
# sites (and tests) resolve unchanged.


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _host() -> str:
    return os.environ.get("FULCRA_COORD_AGENT") or f"coord-reconcile:{socket.gethostname()}"


def _human() -> str:
    return os.environ.get("FULCRA_COORD_HUMAN") or "human"


def _known_sender(args: argparse.Namespace) -> Optional[str]:
    """The sender identity a reply would be addressed to, or None when only the
    anonymous host fallback is available. `_create_directive` records ownership as
    ``--from`` or ``FULCRA_COORD_AGENT`` (else ``coord-reconcile:<host>``); the
    breadcrumb points others at ``inbox --agent <sender>``, so we print it only
    when the sender is a real identity an agent actually uses — never the
    bare host tag."""
    return getattr(args, "sender", None) or os.environ.get("FULCRA_COORD_AGENT")


def _replies_breadcrumb(team: str, sender: str) -> str:
    return f"replies: coord-engine inbox {team} --agent {sender}"


#: Read-cap for the freshness overlay: at most this many absent-from-index docs
#: are read per row load. The overlay's normal bound is new-since-reconcile items
#: (typically zero or a handful), but under a SUSTAINED reconcile outage that set
#: grows without limit — 50 new docs would mean 50 reads per surface-read, per
#: agent, fleet-wide. A capped-but-VISIBLE overlay (the truncation degrades the
#: inbox source) beats both silent truncation and unbounded reads.
DEFAULT_OVERLAY_CAP = 16


def _overlay_cap() -> int:
    """Read-COUNT bound for the freshness overlay. Env ``COORD_OVERLAY_CAP``."""
    return config.env_int("COORD_OVERLAY_CAP", DEFAULT_OVERLAY_CAP)


#: Time budget (seconds) for the freshness overlay's doc reads. The cap bounds
#: READ COUNT, not TIME: under partial degradation (listing succeeds, each doc
#: read runs to the transport's subprocess timeout) 16 absent names could mean
#: minutes of serial timeouts inside EVERY canonical surface read — inbox/
#: needs-me/inbox have no other budget on this path (the briefing budget opens
#: only AFTER _load_rows). That latency is the hang class this branch kills;
#: the overlay carries its own deadline so a watcher's tick can never starve on
#: it. Fast failures (a doc deleted between list and read returns quickly) keep
#: the continue-and-degrade behavior — the budget only stops the SLOW bleed.
DEFAULT_OVERLAY_BUDGET = 10.0


def _overlay_budget() -> float:
    """Time bound (seconds) for the freshness overlay's doc reads. Env
    ``COORD_OVERLAY_BUDGET`` (see the DEFAULT_OVERLAY_BUDGET rationale)."""
    return config.env_float("COORD_OVERLAY_BUDGET", DEFAULT_OVERLAY_BUDGET)


def _fresh_overlay_rows(
    transport: Any, team: str, index_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], bool, str]:
    """Freshness overlay.

    ``inbox``/``needs-me``/every canonical surface read the reconcile-built summaries
    index, so a task/directive doc written BETWEEN reconciles is invisible to all of
    them until the next heartbeat rebuild — a watcher polling only the canonical
    surface can miss fresh work for up to a full reconcile period. When the index is
    present+readable we ALSO list the task dir once and parse ONLY docs whose slug is
    ABSENT from the index (bounded by new-since-reconcile items — typically zero or a
    handful — and hard-capped at ``COORD_OVERLAY_CAP``), unioning them into the fold.
    Rows already in the index are NOT re-read: the index row wins, so this is
    behavior-preserving for every summarized doc.

    Returns ``(overlay_rows, ok, reason)``. ``ok`` flips False — degrading the inbox
    source visibly, never silent, while the index rows are still served — when:
      * the task-dir LISTING raised (the overlay's view is unknown);
      * a LISTED absent doc could not be READ (None/raise): the listing just proved
        the doc exists, so an unreadable read is a transport problem, not a
        sanctioned skip — silently dropping it is the false-clear class this branch
        kills, at the overlay's own read step;
      * the absent set exceeded the cap (truncated — served subset is deterministic:
        absent names are read in sorted order, so every agent converges on the SAME
        served subset; the reason carries {served, absent_total});
      * the ``COORD_OVERLAY_BUDGET`` deadline expired with docs still unread (the
        cap bounds read COUNT, this bounds TIME — slow per-doc reads must not
        starve a surface read/watcher tick; checked AFTER each read, the after-op
        discipline). Everything read so far is still served. When both the budget
        and the cap trip, the budget reason wins (it is the truthful one — the cap
        wasn't what stopped us). Independent failures compose: an unreadable-doc
        reason is preserved alongside a later budget/cap truncation reason.
    Parse-garbage / not-a-Task docs remain sanctioned SILENT skips (mirrors
    reconcile's own tolerance). Cost: one extra ``list_dir`` per row load, plus one
    ``read`` per genuinely-new (unsummarized) slug, at most the cap, within the
    budget."""
    dl = Deadline.open(_overlay_budget())
    prefix = rec.task_prefix(team)
    try:
        listing = transport.list_dir(prefix)
    except Exception:
        # listing unknown -> degraded (caller surfaces it), never silent
        return [], False, "task-dir overlay unreadable"
    from . import model
    known = {str(r.get("name")) for r in index_rows if isinstance(r, dict)}
    absent: list[tuple[str, Any]] = []
    for entry in listing:
        name = entry.get("name") or ""
        if entry.get("is_dir") or not name.endswith(".md") or name in ("index.md", "log.md"):
            continue
        if name[:-3] in known:
            continue  # index row wins — never re-read an already-summarized doc
        absent.append((name, entry))
    absent.sort(key=lambda p: p[0])  # deterministic served subset under the cap
    cap = _overlay_cap()
    overlay: list[dict[str, Any]] = []
    ok = True
    reasons: list[str] = []
    served = 0
    budget_breached = False
    for name, entry in absent[:cap]:
        try:
            raw = transport.read(f"{prefix}{name}")
        except Exception:
            raw = None
        served += 1
        if raw is None:
            # LISTED but unreadable: a transport problem on a doc we know exists.
            # Degrade visibly (never a silent vanish); other overlay docs + the
            # index rows are still served. A FAST failure (doc deleted between
            # list and read) keeps this continue-and-degrade path — only the
            # budget check below stops the slow bleed.
            ok = False
            reasons.append(f"task-dir overlay: fresh doc {name} unreadable")
        else:
            try:
                fm = okf.parse_frontmatter(raw)
                if fm is not None and model.is_task(fm):
                    overlay.append(model.row_from_frontmatter(
                        fm, name=name[:-3], path=f"task/{name}", mtime=entry.get("mtime")))
                # else: parse-garbage / not a Task -> sanctioned silent skip
            except Exception:
                pass  # malformed content is a skip, not a transport failure
        if dl.expired():
            # After-op discipline: the budget bounds TIME where the cap bounds
            # COUNT — stop reading, serve what we have, degrade visibly.
            budget_breached = True
            break
    if budget_breached and served < len(absent):
        ok = False
        reasons.append(f"task-dir overlay budget exhausted: served {served} of "
                       f"{len(absent)} fresh docs")
    elif len(absent) > cap:
        ok = False
        reasons.append(f"task-dir overlay truncated: served {cap} of {len(absent)} "
                       f"fresh docs (COORD_OVERLAY_CAP={cap})")
    return overlay, ok, "; ".join(reasons)


def _load_rows_status(transport: Any, team: str) -> tuple[list[dict[str, Any]], bool, str]:
    """Summaries rows plus whether the fold was fully READABLE (``ok``) and, when it
    was not, a short ``reason`` for the degraded surface to print (attribution: a
    summaries-index failure and a freshness-overlay failure are different outages
    and must not report as one another). ``ok`` is False for an index we could not
    read as intended — present-but-unparseable, or a read/listing that failed under
    a degraded transport — AND for a freshness-overlay problem (listing raised, a
    listed fresh doc unreadable, or the overlay read-cap truncated the fresh set).
    A genuinely-absent index (a fresh team, no reconcile yet) is empty-and-readable
    (``ok`` True): absence is a normal empty state, never conflated with failure.

    ``read`` returning None is ambiguous (absent vs transport-down),
    so a None is disambiguated with one parent listing: ``list_dir`` RAISES on a
    transport failure and its entry names distinguish missing from present-but-
    unreadable. This is what lets one-shot queue reads surface a summaries
    failure instead of folding it to a silent [] indistinguishable from empty."""
    path = rec.summaries_path(team)
    try:
        raw = transport.read(path)
    except Exception:
        return [], False, "summaries index unreadable"
    if raw:
        try:
            rows = aggregate.aggregate_rows(json.loads(raw))
        except Exception:
            # index present but corrupt -> unreadable, surface it
            return [], False, "summaries index unreadable"
        # Live-freshness overlay: union in task docs written since the last
        # reconcile (absent from this index). Any overlay problem flips ``ok`` so
        # the inbox source degrades visibly; the index rows are still served.
        overlay, overlay_ok, overlay_reason = _fresh_overlay_rows(transport, team, rows)
        return rows + overlay, overlay_ok, overlay_reason
    parent, entry = path.rsplit("/", 1)
    try:
        names = {e.get("name") for e in transport.list_dir(parent + "/")}
    except TransportError:
        # transport down -> unknown, not a confirmed-empty index
        return [], False, "summaries index unreadable"
    if entry in names:
        # index there yet unreadable (read returned None) -> degraded
        return [], False, "summaries index unreadable"
    return [], True, ""  # genuinely absent -> a real, readable empty


def _load_rows(transport: Any, team: str) -> list[dict[str, Any]]:
    return _load_rows_status(transport, team)[0]


# --- The public-read failure contract (defined once) -----------------------
#
# Every aggregate-backed public read — `status`, `board`, `needs-me`, `search`,
# `inbox` (and the `briefing` bundle) — folds the summaries index via
# `_load_rows_status`, whose ``ok`` bit is False when the index/listing is
# UNKNOWN: an unreadable/corrupt index, a read that failed under a degraded
# transport, or a degraded freshness overlay. UNKNOWN is not the same as a
# genuinely-absent index (a fresh team, no reconcile yet), which is a real,
# readable empty (``ok`` True). The contract: a read whose ``ok`` is False must
# never return a clean-empty result. It emits the shared machine-parseable
# degraded row below (family-consistent with ``review-fold-degraded`` /
# ``presence-degraded``) and, in text mode, a stderr notice — so "unknown" is
# loud, never silently indistinguishable from "nothing to do". The hazard this
# closes: a silently-empty task fold reads as "all clear" while a real unacked
# P1 directive is merely unreadable, and an agent that cannot tell the two apart
# will confidently do nothing.
_READ_DEGRADED = "read-degraded"


def _read_degraded_row(reason: str, *, marker: str = _READ_DEGRADED) -> dict[str, Any]:
    """Build the ONE public-read degraded marker row — shape ``{type, reason}``
    (the degraded-row family shape ``{type, scanned?, total?, reason}`` with
    scanned/total omitted, because a summaries-index fold is all-or-nothing rather
    than a bounded partial scan). ``marker`` lets `inbox` stamp its named
    ``inbox-degraded`` type while every caller shares this one builder."""
    return {"type": marker, "reason": reason or "summaries index unreadable"}


def _surface_read_degraded(reason: str, *, json_mode: bool,
                           marker: str = _READ_DEGRADED) -> None:
    """Emit the degraded marker the house way for text mode / a stderr notice:
    under ``--json`` the caller is expected to carry the row IN its result (a
    list element or a reserved dict key, so stdout stays a single parseable
    value); this only prints the stderr notice consumed by humans and monitors
    (`json_mode` suppresses stdout noise so a piped consumer never confuses the
    notice for a result). Never suppresses data — the caller still prints its
    partial rows."""
    if not json_mode:
        print(f"{marker}: {reason or 'summaries index unreadable'} — "
              f"unknown, not empty; retry", file=sys.stderr)


def _line(row: dict[str, Any]) -> str:
    return (
        f"  [{row.get('priority', '?'):>2}] {str(row.get('status', '?')):8} "
        f"{row.get('title') or row.get('name')}"
        + (f"  ({row.get('assignee')})" if row.get("assignee") else "")
    )


def cmd_reconcile(args: argparse.Namespace, transport: Any) -> int:
    dt = _now()
    res = rec.reconcile(
        transport, args.team, now=_iso(dt), today=dt.strftime("%Y-%m-%d"), host=_host(),
        retention_days=getattr(args, "retention_days", None),
    )
    if res.get("degraded"):
        print(f"reconcile degraded (no writes): {res.get('reason')}", file=sys.stderr)
        return 1
    print(
        f"reconciled team/{args.team}: {res['tasks']} tasks "
        f"({res['parsed']} parsed, {res['reused']} reused), "
        f"{res['transitions']} log entries, {len(res['warnings'])} warnings"
        + (" [fast-path: no fold-relevant changes in store feed]" if res.get("fast_path") else "")
    )
    for w in res["warnings"]:
        print(f"  warn: {w}", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace, transport: Any) -> int:
    # Public-read failure contract (see _read_degraded_row): consume the readable
    # bit, never fold an UNKNOWN index to clean-empty (all-zero) counts.
    rows, ok, reason = _load_rows_status(transport, args.team)
    counts = query.status_counts(rows)
    if args.json:
        if not ok:
            # Embed the marker under a reserved key so stdout stays ONE parseable
            # object; a consumer summing status counts already knows its status
            # vocabulary and skips the namespaced marker.
            counts = {**counts, _READ_DEGRADED: _read_degraded_row(reason)}
        print(json.dumps(counts, indent=2))
    else:
        if not ok:
            _surface_read_degraded(reason, json_mode=False)
        elif not rows:
            print(f"(no aggregate for team/{args.team} — run `reconcile` first)")
        print(f"team/{args.team}: {len(rows)} tasks — " + ", ".join(
            f"{k}={v}" for k, v in sorted(counts.items())
            if k != _READ_DEGRADED))
    return 0


def cmd_board(args: argparse.Namespace, transport: Any) -> int:
    rows, ok, reason = _load_rows_status(transport, args.team)
    groups = query.board(rows)
    if args.json:
        if not ok:
            # Reserved section-shaped key: value is a list (like every other board
            # section) so stdout stays one parseable object and the text loop's
            # fixed section set ignores it.
            groups[_READ_DEGRADED] = [_read_degraded_row(reason)]
        print(json.dumps(groups, indent=2))
        return 0
    if not ok:
        _surface_read_degraded(reason, json_mode=False)
    for section in ("active", "waiting", "blocked", "proposed"):
        items = groups.get(section, [])
        if items:
            print(f"{section.upper()} ({len(items)})")
            for r in items:
                print(_line(r))
    return 0


def cmd_needs_me(args: argparse.Namespace, transport: Any) -> int:
    now = _iso(_now())
    rows, rows_ok, rows_reason = _load_rows_status(transport, args.team)
    # Role routing: work addressed to a role this agent holds IS work that needs
    # this agent (see _held_roles_for_rows). An unresolved role is UNKNOWN and gets
    # its own marker below — never folded into "no role work".
    held_roles, unresolved_roles = _held_roles_for_rows(
        transport, args.team, args.agent, rows, now=now)
    got = query.needs_me(rows, args.agent, now=now, held_roles=held_roles)
    # Public-read failure contract: an UNKNOWN task fold must announce itself with
    # the shared marker BEFORE the review add-on piles its own markers onto what
    # would otherwise read as a silently-empty (but "complete") needs-me.
    if not rows_ok:
        got = [_read_degraded_row(rows_reason)] + got
    if unresolved_roles:
        got = [_role_degraded_row(unresolved_roles)] + got
    # Shared add-on deadline (see _briefing_budget): opened here so the pending-
    # reviews fold is bounded against a single cumulative budget.
    add_on = Deadline.open(_briefing_budget())
    got += _pending_reviews_for(
        transport, args.team, args.agent, deadline=add_on.instant)
    if args.json:
        print(json.dumps(got, indent=2))
    else:
        print(f"{len(got)} item(s) need {args.agent}:")
        for r in got:
            if r.get("type") == _READ_DEGRADED:
                print(f"  read degraded: {r.get('reason')} — task fold unknown "
                      f"(not empty), retry")
            elif r.get("type") == _ROLE_DEGRADED:
                print(_role_degraded_line(r))
            elif r.get("type") == "review-pending":
                print(f"  [REVIEW] pending verdict: {r['name']} "
                      f"(required: {', '.join(r['pending_required'])})")
            elif r.get("type") == "review-fold-degraded":
                print(_review_degraded_line(r))
            elif r.get("type") == "review-orphan":
                print(f"  [REVIEW] orphan review dir (verdicts, no doc): "
                      f"{r['name']} — needs maintainer repair")
            elif r.get("type") == "review-orphan-degraded":
                if r.get("unclassified"):
                    print(f"  [REVIEW] dir classification degraded: "
                          f"{r['unclassified']} dir(s) unclassified before budget — retry")
                else:
                    print(f"  [REVIEW] orphan dir classification degraded: "
                          f"{r['name']} — verdicts listing unreadable, retry")
            elif r.get("type") == "review-role-degraded":
                print(f"  review role resolution degraded: "
                      f"{', '.join(r.get('roles') or [])} — holders unknown, retry")
            else:
                print(_line(r))
    return 0


def cmd_search(args: argparse.Namespace, transport: Any) -> int:
    rows, ok, reason = _load_rows_status(transport, args.team)
    degraded_reasons = [] if ok else [reason]
    if getattr(args, "archived", False):
        # cold path: read archived task docs directly (archives are small + rare)
        from . import model as _model
        months, archive_reason = _archive_months_status(transport, args.team)
        if archive_reason:
            degraded_reasons.append(archive_reason)
        for month in months:
            pfx = f"{rec.archive_prefix(args.team)}{month}/"
            try:
                for e in transport.list_dir(pfx):
                    n = e.get("name") or ""
                    if e.get("is_dir") or not n.endswith(".md"):
                        continue
                    fm = okf.parse_frontmatter(transport.read(pfx + n))
                    if fm is not None and _model.is_task(fm):
                        row = _model.row_from_frontmatter(fm, name=n[:-3],
                                                          path=f"task/archive/{month}/{n}")
                        row["archived"] = month
                        rows.append(row)
            except TransportError:
                degraded_reasons.append(f"task archive/{month} unreadable")
    got = query.search(rows, args.query)
    # Public-read failure contract: an UNKNOWN hot index or partial cold archive
    # must not return a confident match (or clean-empty result). Preserve readable
    # rows as evidence, but prefix the shared degraded marker so consumers fail
    # closed before acting on an incomplete identity view.
    degraded_reason = "; ".join(dict.fromkeys(filter(None, degraded_reasons)))
    if degraded_reason:
        got = [_read_degraded_row(degraded_reason)] + got
    if args.json:
        print(json.dumps(got, indent=2))
    else:
        if degraded_reason:
            _surface_read_degraded(degraded_reason, json_mode=False)
        real = [r for r in got if r.get("type") != _READ_DEGRADED]
        print(f"{len(real)} match(es) for {args.query!r}:")
        for r in real:
            print(_line(r))
    return 0


# --- roles (fulcra-agent-roles fold) ---

def _role_doc_path(team: str, role: str) -> str:
    return f"team/{team}/roles/{role}.md"


def _leases_prefix(team: str, role: str) -> str:
    return f"team/{team}/roles/{role}/leases/"


def _nonce_state_path(team: str, role: str, key: str) -> pathlib.Path:
    base = pathlib.Path(os.environ.get("COORD_ENGINE_STATE_DIR")
                        or pathlib.Path.home() / ".local" / "state" / "coord-engine")
    # agent_key over the (team, role) pair keeps the filename injective — raw
    # f"{team}-{role}" would collide ("a-b"/"c" vs "a"/"b-c"), the exact defect
    # agent_key exists to prevent for agent ids.
    return base / f"lease-nonce-{tasks.agent_key(f'{team}/{role}')}-{key}.txt"


def _escalation_marker_path(team: str, role: str, date: str) -> str:
    return f"team/{team}/roles/{role}/escalations/{date}.md"


def cmd_roles_status(args: argparse.Namespace, transport: Any) -> int:
    team, role = args.team, args.role
    now = _iso(_now())
    # A None role-doc read is DISAMBIGUATED with one roles/ listing (fetched only
    # on the None path, so healthy queries pay nothing): doc listed-but-unreadable
    # = transport failure = UNKNOWN rc 1 — a transient doc-read failure must not
    # collapse a long-SLA role onto the 24h default and print a false VACANT.
    # Doc genuinely ABSENT keeps the default-SLA fallback: querying an
    # unregistered role (leases without a doc — `roles claim` supports it) still
    # works. This supersedes the earlier single-read-ambiguity rationale: the
    # disambiguator (`_roles_listing_names`) now exists and its cost lands only
    # on the already-degraded path.
    raw_doc = transport.read(_role_doc_path(team, role))
    reg = okf.parse_frontmatter(raw_doc)
    if reg is None:
        # A read miss and a body that won't parse are the same fact — no usable
        # doc — so they take the same path. A listed-but-unparseable doc must not
        # fall through to `or {}`, i.e. onto the 24h default SLA and a confident
        # VACANT at rc 0, which is the precise collapse the comment above forbids.
        # `_role_fresh_holders` enforces the identical rule, so both surfaces agree;
        # otherwise the "same fold" contract between them is a lie.
        names = _roles_listing_names(transport, team)
        if names is None or f"{role}.md" in names:
            print(f"role doc unusable for {role} in team/{team} — state unknown "
                  f"(unreadable or corrupt), retry", file=sys.stderr)
            return 1
        reg = {}  # genuinely absent -> default-SLA fallback (leases without a doc)
    policy = reg.get("policy") or "shared"
    sla = roles.parse_sla_hours(reg.get("sla_hours"))
    if sla is None:
        # A readable doc whose `sla_hours` is EXPLICITLY invalid: same fact as an
        # unreadable one — the SLA is unknown, so every state below (HELD / VACANT /
        # escalation_due) would be asserted off a window we invented. rc 1, assert
        # nothing. Absent/blank keeps the default and prints normally.
        print(f"unusable sla_hours ({reg.get('sla_hours')!r}) for {role} in "
              f"team/{team} — state unknown; fix the role doc", file=sys.stderr)
        return 1
    try:
        entries = transport.list_dir(_leases_prefix(team, role))
        leases: Optional[list[dict[str, Any]]] = []
        for e in entries:
            n = e.get("name") or ""
            if e.get("is_dir") or not n.endswith(".md"):
                continue
            fm = okf.parse_frontmatter(transport.read(_leases_prefix(team, role) + n))
            if fm is None:
                # A JUST-LISTED lease shard read None/unparseable: folding it out
                # as `{}` (timestamp lost -> stale) would be a hidden vacancy.
                leases = None  # UNKNOWN
                break
            leases.append({"agent": fm.get("agent") or n[:-3], "timestamp": fm.get("timestamp")})
    except TransportError:
        leases = None  # unreadable -> UNKNOWN
    status = roles.classify(leases, now=now, sla_hours=sla, policy=policy)
    # Dormancy: a deliberately-parked role (future dormant_until) reads as DORMANT
    # instead of VACANT and never shows escalation_due — but a LIVE lease outranks
    # the park (HELD wins the display). Garbage dormant_until fails open with a note.
    dormant, dormant_err = roles.dormant_state(reg.get("dormant_until"), now=now)
    if dormant_err:
        print(f"roles status: unparseable dormant_until for {role} in team/{team} — "
              f"treated as absent (not dormant); fix the date to park it",
              file=sys.stderr)
    if status == roles.VACANT and dormant:
        status = roles.DORMANT
    today = _now().strftime("%Y-%m-%d")
    marker_exists = transport.read(_escalation_marker_path(team, role, today)) is not None
    esc = roles.escalation_due(leases, now=now, sla_hours=sla,
                               marker_exists_today=marker_exists, dormant=dormant)
    fresh = roles.fresh_holders(leases, now=now, sla_hours=sla) if leases else []
    result = {
        "team": team, "role": role, "status": status, "policy": policy, "sla_hours": sla,
        "holders": [l.get("agent") for l in (leases or [])],
        "fresh_holders": [l.get("agent") for l in fresh],
        "escalation_due": esc,
    }
    if status == roles.DORMANT:
        result["dormant_until"] = reg.get("dormant_until")
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        label = (f"DORMANT (until {reg.get('dormant_until')})"
                 if status == roles.DORMANT else status)
        print(f"role {role} in team/{team}: {label} (policy={policy}, sla={sla:g}h)")
        if fresh:
            print("  fresh holders: " + ", ".join(str(l.get("agent")) for l in fresh))
        if esc:
            print("  ESCALATION DUE — vacant past SLA, no marker today")
    if status == roles.UNKNOWN:
        # Fail closed: the lease listing was unreadable, so the role's
        # state is UNKNOWN — NOT vacant. A degraded transport must not let a caller
        # read this as VACANT and fire a false SLA escalation. rc 1, same register
        # as `review status`'s "tally unknown" (leases dropped/None never asserts).
        print(f"lease state unknown for role {role} in team/{team} — "
              f"degraded transport, retry", file=sys.stderr)
        return 1
    return 0


# --- tasks (fulcra-agent-tasks lifecycle) ---

def _task_path(team: str, name: str) -> str:
    return f"team/{team}/task/{name}.md"


def cmd_task_start(args: argparse.Namespace, transport: Any) -> int:
    try:
        slug, content = tasks.new_task_doc(
            args.title, now=_iso(_now()), workstream=args.workstream, status=args.status,
            priority=args.priority, owner=_host(), assignee=args.assignee,
            summary=args.summary or "", next_action=args.next, kind=args.kind,
            evidence=args.evidence,
        )
    except tasks.TaskError as e:
        print(f"task start failed: {e}", file=sys.stderr)
        return 1
    path = _task_path(args.team, slug)
    if not args.force and transport.read(path) is not None:
        print(f"task {slug} already exists (use --force)", file=sys.stderr)
        return 1
    transport.write(path, content)
    print(f"created team/{args.team}/task/{slug}.md ({args.status})")
    return 0


def cmd_task_update(args: argparse.Namespace, transport: Any) -> int:
    path = _task_path(args.team, args.name)
    try:
        out = tasks.apply_update(
            transport.read(path), now=_iso(_now()), status=args.status, summary=args.summary,
            next_action=args.next, assignee=args.assignee, blocked_on=args.blocked_on,
            priority=args.priority, evidence=args.evidence,
        )
    except tasks.TaskError as e:
        print(f"task update failed: {e}", file=sys.stderr)
        return 1
    transport.write(path, out)
    print(f"updated {args.name}" + (f" → {args.status}" if args.status else ""))
    return 0


def _task_apply(args, transport, **kw) -> int:
    """Shared read-modify-write for the dedicated lifecycle verbs."""
    path = _task_path(args.team, args.name)
    try:
        out = tasks.apply_update(transport.read(path), now=_iso(_now()), **kw)
    except tasks.TaskError as e:
        verb = getattr(args, "verb", getattr(args, "task_command", "update"))
        print(f"task {verb} failed: {e}", file=sys.stderr)
        return 1
    transport.write(path, out)
    print(f"{getattr(args, 'verb', 'updated')} {args.name}")
    return 0


def cmd_task_block(args: argparse.Namespace, transport: Any) -> int:
    if not args.blocked_on and not args.on_user:
        print("task block failed: requires --blocked-on or --on-user", file=sys.stderr)
        return 1
    if args.blocked_on and args.on_user:
        print("task block failed: pass --blocked-on OR --on-user, not both", file=sys.stderr)
        return 1
    if not args.unlock and not args.on_user:
        print("task block failed: --unlock <what specifically unblocks this> "
              "is required", file=sys.stderr)
        return 1
    blocked_on = f"user:{args.on_user}" if args.on_user else args.blocked_on
    unlock = args.unlock or f"answer from {args.on_user}"
    kw = {"status": "blocked", "blocked_on": blocked_on, "unlock": unlock}
    if args.on_user:
        kw["assignee"] = _human()
        kw["add_tags"] = ["needs:human"]
    return _task_apply(args, transport, **kw)


def cmd_task_supersede(args: argparse.Namespace, transport: Any) -> int:
    reason = args.reason or f"work re-dispatched as {args.by}"
    return _task_apply(
        args, transport, status="done", superseded_by=args.by,
        evidence=f"superseded by {args.by} ({reason})")


def cmd_task_pause(args: argparse.Namespace, transport: Any) -> int:
    return _task_apply(args, transport, status="waiting", next_action=args.next)


def cmd_task_abandon(args: argparse.Namespace, transport: Any) -> int:
    return _task_apply(args, transport, status="abandoned", evidence=args.reason)


def cmd_task_assign(args: argparse.Namespace, transport: Any) -> int:
    kw = {"assignee": args.assignee}
    if args.assignee != _human():
        kw["remove_tags"] = ["needs:human"]
    return _task_apply(args, transport, **kw)


def _archive_months_status(transport: Any, team: str) -> tuple[list[str], str]:
    try:
        return (
            [
                e["name"].rstrip("/")
                for e in transport.list_dir(rec.archive_prefix(team))
                if e.get("is_dir")
            ],
            "",
        )
    except TransportError:
        return [], "task archive months unreadable"


def _archive_months(transport: Any, team: str) -> list[str]:
    return _archive_months_status(transport, team)[0]


def cmd_task_restore(args: argparse.Namespace, transport: Any) -> int:
    """Move an archived task back into the hot path (verified move)."""
    for month in sorted(_archive_months(transport, args.team), reverse=True):
        src = f"{rec.archive_prefix(args.team)}{month}/{args.name}.md"
        if transport.read(src) is None:
            continue
        dst = _task_path(args.team, args.name)
        if transport.read(dst) is not None:
            print(f"restore failed: {args.name} already exists in the hot path", file=sys.stderr)
            return 1
        if rec._crash_safe_move(transport, src, dst):
            print(f"restored {args.name} from archive/{month}/ (run reconcile to reindex)")
            return 0
        print(f"restore failed: verified move from archive/{month}/ failed", file=sys.stderr)
        return 1
    print(f"restore failed: {args.name} not found in the archive", file=sys.stderr)
    return 1


def _review_archive_months(transport: Any, team: str) -> Optional[list[str]]:
    try:
        return [
            str(e.get("name") or "").rstrip("/")
            for e in transport.list_dir(rec.review_archive_prefix(team))
            if e.get("is_dir") and e.get("name")
        ]
    except TransportError:
        return None


def cmd_review_restore(args: argparse.Namespace, transport: Any) -> int:
    """Restore a cold-archived settled-single verdict to the hot review path."""
    months = _review_archive_months(transport, args.team)
    if months is None:
        print("review restore failed: archive root listing unknown", file=sys.stderr)
        return 1
    for month in sorted(months, reverse=True):
        cold_prefix = (
            f"{rec.review_archive_prefix(args.team)}{month}/{args.slug}/verdicts/"
        )
        try:
            entries = transport.list_dir(cold_prefix)
        except TransportError:
            print(f"review restore failed: archive listing unknown for {args.slug}",
                  file=sys.stderr)
            return 1
        files = [
            str(e.get("name") or "") for e in entries
            if not e.get("is_dir") and str(e.get("name") or "").endswith(".md")
        ]
        if not files:
            continue
        if files != ["codex-reviewer.md"]:
            print(f"review restore failed: unexpected archived verdict shape for {args.slug}",
                  file=sys.stderr)
            return 1
        filename = files[0]
        src = cold_prefix + filename
        dst = f"team/{args.team}/review/{args.slug}/verdicts/{filename}"
        if transport.read(dst) is not None:
            print(f"review restore failed: {args.slug} already exists in the hot path",
                  file=sys.stderr)
            return 1
        if rec._crash_safe_move(transport, src, dst):
            print(f"restored review {args.slug} from reviews/{month}/")
            return 0
        print(f"review restore failed: verified move from reviews/{month}/ failed",
              file=sys.stderr)
        return 1
    print(f"review restore failed: {args.slug} not found in the archive", file=sys.stderr)
    return 1


def cmd_task_done(args: argparse.Namespace, transport: Any) -> int:
    path = _task_path(args.team, args.name)
    try:
        out = tasks.mark_done(transport.read(path), now=_iso(_now()), evidence=args.evidence)
    except tasks.TaskError as e:
        print(f"task done failed: {e}", file=sys.stderr)
        return 1
    transport.write(path, out)
    print(f"done {args.name}")
    return 0


# --- review (fulcra-agent-review verdict tally) ---

def _review_doc_path(team: str, slug: str) -> str:
    return f"team/{team}/review/{slug}.md"


def _verdicts_prefix(team: str, slug: str) -> str:
    return f"team/{team}/review/{slug}/verdicts/"


# Settled-skip: once a review reaches a terminal APPROVED state with no
# outstanding required reviewers, a tiny cache marker is dropped in the verdicts
# prefix (so the one listing the fold already does reveals it — zero extra
# reads). It is not a `.md` file, so the verdict-reading loop already ignores it.
# Contract: a settled review is immutable — a new verdict on it is a no-op by
# definition (already APPROVED, required list frozen), and changing the required
# set re-opens the review only via a new slug. The marker is a fold cache, never
# a source of truth: `review status` recomputes the full tally every time and so
# self-heals a wrong/stale marker on direct query.
SETTLED_MARKER = ".settled"

#: Aggregate deadline (seconds) for ``_pending_reviews_for`` — never let a degraded
#: pending-review scan hang or (via a bad env value) run unbounded.
DEFAULT_REVIEW_FOLD_BUDGET = 45.0
#: Aggregate deadline (seconds) for the transport-heavy briefing/needs-me add-on
#: sections. One budget opens when the add-on stack begins and is spent cumulatively
#: across sections, so a bundle's bound is the bundle's — not per-section, which would
#: let N sections each spend the full budget. pending-reviews keeps its own independent
#: COORD_REVIEW_FOLD_BUDGET (sooner wins).
DEFAULT_BRIEFING_BUDGET = 60.0
#: Cumulative deadline (seconds) for ONE role-resolution pass (`_held_roles_for_rows`)
#: — the fold `briefing` / `inbox` / `needs-me` all run, i.e. every agent,
#: every tick. Its cost is 1 + sum(2 + lease_shards) over the roles the open work
#: references (see `_held_roles_for_rows`), and lease shards accumulate per claiming
#: agent forever (only `roles release` prunes one), so an unbudgeted pass could spend
#: one transport timeout per role doc, per lease listing AND per shard before the hot
#: path renders anything. 20s is a generous ~25 ops at the measured ~0.8s/op — far
#: past the 4-7 a real team pays — while still bounding a degraded transport.
DEFAULT_ROLE_FOLD_BUDGET = 20.0
def _settled_marker_path(team: str, slug: str) -> str:
    return _verdicts_prefix(team, slug) + SETTLED_MARKER


def _review_fold_budget() -> float:
    """Aggregate deadline for `_pending_reviews_for`, seconds. Env
    ``COORD_REVIEW_FOLD_BUDGET`` (see the DEFAULT_REVIEW_FOLD_BUDGET rationale)."""
    return config.env_float("COORD_REVIEW_FOLD_BUDGET", DEFAULT_REVIEW_FOLD_BUDGET)


def _briefing_budget() -> float:
    """Shared aggregate deadline (seconds) for the briefing/needs-me add-on stack.
    Env ``COORD_BRIEFING_BUDGET`` (see the DEFAULT_BRIEFING_BUDGET rationale). One
    absolute ``time.monotonic()`` deadline is computed where the stack opens and
    passed to each transport-heavy section, so an earlier section's spend shrinks
    what the next one gets; pending-reviews keeps its own independent
    ``COORD_REVIEW_FOLD_BUDGET`` (whichever bound is sooner wins)."""
    return config.env_float("COORD_BRIEFING_BUDGET", DEFAULT_BRIEFING_BUDGET)


def _role_fold_budget() -> float:
    """Cumulative deadline (seconds) for one role-resolution pass. Env
    ``COORD_ROLE_FOLD_BUDGET`` (see the DEFAULT_ROLE_FOLD_BUDGET rationale). Its own
    knob, like ``COORD_REVIEW_FOLD_BUDGET``: role resolution runs BEFORE the
    briefing/needs-me add-on stack opens its budget (the held set is an input to the
    inbox fold, not an add-on section), so it cannot spend that one."""
    return config.env_float("COORD_ROLE_FOLD_BUDGET", DEFAULT_ROLE_FOLD_BUDGET)


def _write_settled_marker(transport: Any, team: str, slug: str, *, now: str) -> None:
    """Best-effort settled-cache write. Failure is swallowed: the marker only
    speeds the fan-out fold; its absence just means the next fold recomputes."""
    try:
        transport.write(
            _settled_marker_path(team, slug),
            okf.render_frontmatter({"schema": "review-settled/v1",
                                    "state": review.APPROVED, "ts": now}),
        )
    except Exception:
        pass


def _is_settleable(tally: dict[str, Any]) -> bool:
    """True only for a tally that may be cached as settled: APPROVED, nothing
    pending, and a parsed non-empty required list. The required gate is the
    false-settle guard: ``transport.read()`` returns None on failure (incl.
    timeout — it never raises), so a transient doc-read failure yields
    required=None and ``review.tally(..., required=None)`` goes APPROVED off any
    one readable approval verdict — cache that and a genuinely-pending review is
    hidden from every fold, durably. ``review request`` refuses to open a review
    without --reviewer, so an absent/empty required list can only mean doc-read
    failure, doc corruption, or a legacy/malformed doc — never a legitimate
    settle state. Such tallies stay uncached (re-tallied each fold); only the
    marker write is gated here, never the reported state."""
    return (tally.get("state") == review.APPROVED
            and not tally.get("pending_required")
            and bool(tally.get("required")))


def _tally_from_verdict_entries(
    transport: Any, team: str, slug: str, entries: list[dict[str, Any]],
    doc_raw: Optional[str], *, deadline: Optional[float] = None,
) -> tuple[dict[str, Any], bool, bool]:
    """Verdict-shard reads -> ``(tally, verdict_reads_ok, fully_scanned)``, given
    an already-fetched verdicts listing and the already-read review doc
    (``doc_raw``). A None ``doc_raw`` means the doc read failed or the doc is
    missing — callers on the fold path must treat that as UNKNOWN (skip +
    count), not pass it here; this helper just tallies what it is given.

    ``verdict_reads_ok`` is False when any listed verdict file's read returned
    None (transport failure — the file EXISTS, its content is unknown): the
    tally is then a floor, not the truth — a lost CHANGES verdict would look
    APPROVED — so settle-marker writers must not cache it. A file that reads
    fine but parses to garbage is NOT a read failure (garbage is simply not a
    verdict). Split out so the fan-out fold can list ONCE, short-circuit on
    `.settled`, read the doc, and only then pay for the verdict reads.

    ``deadline`` is an absolute ``time.monotonic()`` instant bounding the
    per-verdict read loop: ONE review with many shards would otherwise read every
    shard unbounded (N x transport.timeout), blowing the aggregate fold budget
    with no degraded marker. The deadline is checked BOTH before and AFTER each
    shard read: a strict wall-clock bound is impossible without cancellable
    transport, so the guarantee is that an overrun is DETECTED and REPORTED
    immediately after the blocking op (a single stalled read that sleeps past the
    budget can no longer return a clean row) — budget overshoot is bounded by ONE
    transport timeout. On expiry the loop STOPS mid-slug and returns
    ``fully_scanned=False`` — the partial tally is a floor the caller MUST NOT
    trust (it counts the slug as skipped, surfaces the degraded marker). None
    (``review status``, no budget) never bounds and always scans fully."""
    req_doc = okf.parse_frontmatter(doc_raw) or {}
    required = req_doc.get("required")
    if isinstance(required, str):
        required = [r.strip() for r in required.split(",") if r.strip()]
    elif isinstance(required, list):
        required = [str(r).strip() for r in required if str(r).strip()]
    verdicts: list[dict[str, Any]] = []
    reads_ok = True
    fully_scanned = True
    dl = Deadline(deadline)
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        if dl.expired():
            # Budget expired mid-slug: stop reading shards. The tally built so far
            # is a floor, not the truth — the caller treats this slug as skipped.
            fully_scanned = False
            break
        raw_v = transport.read(_verdicts_prefix(team, slug) + n)
        if dl.expired():
            # The deadline passed DURING this read: checking only BEFORE
            # the read let one stalled read complete and return a clean row despite
            # blowing the budget. Detect the overrun immediately after the blocking
            # op — the slug is not fully scanned. Overshoot is bounded by ONE read.
            fully_scanned = False
            break
        if raw_v is None:
            reads_ok = False  # listed file unreadable -> tally is incomplete
        fm = okf.parse_frontmatter(raw_v) or {}
        # Key by the FILENAME stem (ACL-controlled path), not the frontmatter
        # `reviewer:` — otherwise a file `mallory.md` claiming `reviewer: alice`
        # could shadow alice's real verdict. One verdict file per reviewer.
        verdicts.append({"reviewer": n[:-3], "verdict": fm.get("verdict")})
    return review.tally(verdicts, required=required), reads_ok, fully_scanned


def _review_tally(
    transport: Any, team: str, slug: str
) -> tuple[dict[str, Any], bool, bool, bool]:
    """Shared review fold: doc + verdict shards ->
    ``(tally, doc_ok, verdict_reads_ok, listing_ok)``.

    ALWAYS computes the full tally — it never consults the `.settled` marker, so
    a corrupt/stale marker can never hide the truth on a direct `review status`
    query (the marker only serves the fan-out fold, `_pending_reviews_for`).

    ``doc_ok`` is False when the review doc could not be read (missing OR
    transport failure — ``read()`` returns None for both, indistinguishably):
    the tally was built on NO required list and must be treated as unknown,
    never as a clean state. ``verdict_reads_ok`` is False when a listed verdict
    file's content could not be read — the tally is a floor, not the truth.

    ``listing_ok`` is False when the verdicts LISTING raised (the prefix is
    unlistable under a degraded transport). We still fall back to ``entries=[]``
    so this never crashes, but that fallback makes ``verdict_reads_ok`` vacuously
    True (no listed files = no failed reads) and the tally a floor built over
    ZERO verdicts — so the caller MUST treat a False ``listing_ok`` exactly like
    the other unknowns (fail closed; never a clean state, never a marker
    delete/write). An EMPTY-but-readable listing (list_dir returns []) is a
    legitimate no-verdicts PENDING and keeps ``listing_ok`` True."""
    raw = transport.read(_review_doc_path(team, slug))
    listing_ok = True
    try:
        entries = transport.list_dir(_verdicts_prefix(team, slug))
    except TransportError:
        entries = []
        listing_ok = False
    # No deadline: `review status` is a direct, per-slug query with no fold
    # budget, so it always scans every verdict shard (fully_scanned ignored).
    tally, vok, _ = _tally_from_verdict_entries(transport, team, slug, entries, raw)
    return tally, raw is not None, vok, listing_ok


def _classify_orphan_dir(transport: Any, team: str, slug: str) -> str:
    """Classify a dir-only review slug — a ``<slug>/`` prefix under the review root
    with NO ``<slug>.md`` doc — via ONE listing of its verdicts prefix (the same
    listing the orphan feature needs, so classification is zero extra ops). The
    store's deletes are SOFT: an archived/deleted review leaves its dir prefix
    behind forever, so the three-way tells a live orphan from that ghost:

    - ``"orphan"``    — at least one verdict ``.md`` shard is present: real
      verdicts, no doc. Surface for maintainer repair (unchanged behavior).
    - ``"tombstone"`` — no verdict ``.md`` shards (empty, or only a stale
      ``.settled`` marker whose review doc is gone). The dir carries ZERO
      information; fold it away silently — an orphan/[?] row here is the WRONG
      ontology, not a real pending obligation, and a retry never resurrects a doc.
    - ``"unknown"``   — the verdicts listing RAISED (degraded transport). NEVER
      assume tombstone on a transport failure: the fail-closed rule outranks
      tombstone-skip, so this stays VISIBLY degraded and is retried."""
    try:
        ventries = transport.list_dir(_verdicts_prefix(team, slug))
    except TransportError:
        return "unknown"
    for x in ventries:
        n = x.get("name") or ""
        if not x.get("is_dir") and n.endswith(".md"):
            return "orphan"
    return "tombstone"


def _roles_listing_names(transport: Any, team: str) -> Optional[set[str]]:
    """Entry names under ``team/<team>/roles/``, or None if the listing itself
    raised (membership UNKNOWN). The disambiguator for a role-doc ``read`` that
    returned None: listed-but-unreadable = transport failure; absent = genuinely
    not a role."""
    try:
        return {(e.get("name") or "") for e in transport.list_dir(f"team/{team}/roles/")}
    except TransportError:
        return None


def _role_fresh_holders(
    transport: Any, team: str, name: str, *, now: str,
    listing_cache: Optional[dict[str, Any]] = None,
    deadline: Optional[Deadline] = None,
) -> tuple[list[str], bool]:
    """Fresh lease holders of role name per the CANONICAL fold: the role
    doc's own sla_hours (falling back to the default) fed to
    roles.fresh_holders — the same fold roles status uses, so the two
    can never disagree about a lease.

    Returns ``(holders, ok)``. Fail closed:
    ``ok`` is False whenever the lease state is UNKNOWN — never let a degraded
    transport read as "no holders" (asserting vacancy / silently dropping
    role-routed work). UNKNOWN cases:

    - the lease LISTING raises ``TransportError``;
    - a JUST-LISTED lease shard reads None or unparseable (previously ``or {}``
      dropped its timestamp and silently folded the holder out as stale — a
      fail-open vacancy INSIDE the fold);
    - no USABLE role document — the read returned None, or returned a body that
      does not parse as frontmatter — for a name the roles/ listing SHOWS is a
      registered role (or while that listing itself raised, leaving membership
      unknown);
    - the doc parses but its ``sla_hours`` is EXPLICITLY INVALID (``abc``, a
      negative, a non-finite): the operator stated a window and it did not parse,
      so there is nothing to measure freshness against. An ABSENT or blank
      ``sla_hours`` is NOT this case — the field is optional and omitting it
      legitimately selects the default (``roles.parse_sla_hours`` draws the line);
    - ``deadline`` expires with role state still unread (see below).

    **Only a complete, successfully parsed LISTING is negative membership
    evidence.** The one non-degraded absence is a doc-read miss for a name the
    listing affirmatively does NOT contain (``([], True)`` — the literal-agent-id
    case). A failed read and a failed PARSE are the same fact: we do not know what
    that document says. An unparseable body must not short-circuit to "affirmative
    non-role" — the listing has already proved the name IS a role, so a truncated
    or malformed doc would otherwise serve its holder a clean, role-blind queue
    with no ``role-degraded`` marker at all: an empty inbox AND an empty needs_me
    that silently drop role-routed work. A parse result is not evidence about
    registration; the listing is.

    ``deadline`` bounds the role's own fan-out (its doc read, its lease listing,
    and a read per lease shard — unbounded in the shard count, since shards
    accumulate per claiming agent). Checked before each blocking op that follows
    another, per the module deadline discipline: an overrun is detected
    immediately after the op that caused it, overshoot is bounded by one op, and a
    completed fold is never degraded merely for finishing late (its answer is
    definitive knowledge — keep it). ``None`` -> unbounded, for the direct callers
    (`roles status`) that are not on the hot path.

    ``listing_cache`` (a per-tick/per-fold dict) memoizes the one roles/ listing
    across role-shaped assignees; pass the same dict for every call in a pass."""
    if "/" in name:
        return [], True  # a role name is a single path segment; anything else is not a role
    dl = deadline if deadline is not None else Deadline(None)  # None -> never expires
    raw_doc = transport.read(_role_doc_path(team, name))
    reg = okf.parse_frontmatter(raw_doc)
    if reg is None:
        # No usable role document: absent, empty, truncated, or unparseable. Which
        # of those it is does not matter here — none of them is evidence about
        # whether `name` is a registered role. Only the listing answers that.
        cache = listing_cache if listing_cache is not None else {}
        if "names" not in cache:
            cache["names"] = _roles_listing_names(transport, team)
        names = cache["names"]
        if names is None or f"{name}.md" in names:
            # roles/ listing unreadable (membership unknown) OR the doc is listed
            # yet unusable (transport failure / corrupt doc): UNKNOWN, fail closed.
            return [], False
        return [], True  # genuinely absent -> not a role (literal agent id case)
    sla = roles.parse_sla_hours(reg.get("sla_hours"))
    if sla is None:
        # The doc parsed, but its `sla_hours` did not: an EXPLICITLY invalid value.
        # UNKNOWN — freshness has no window to be measured against. Absent/blank
        # still means "use the default" and resolves normally; see
        # `roles.parse_sla_hours` for why those two are not the same fact.
        return [], False
    if dl.expired():
        return [], False  # the doc read spent the budget; the lease state is UNREAD
    leases: list[dict[str, Any]] = []
    try:
        for f in transport.list_dir(_leases_prefix(team, name)):
            fn = f.get("name") or ""
            if f.get("is_dir") or not fn.endswith(".md"):
                continue
            if dl.expired():
                # The listing (or the previous shard read) spent the budget with
                # shards still unread. A lease we never read is UNKNOWN, exactly as
                # if its read had failed — folding the rest out would assert a
                # vacancy we did not observe.
                return [], False
            fm = okf.parse_frontmatter(transport.read(_leases_prefix(team, name) + fn))
            if fm is None:
                # Listed shard, failed/unparseable read: this lease's freshness is
                # UNKNOWN — folding it out as stale would be a hidden vacancy.
                return [], False
            leases.append({"agent": fm.get("agent") or fn[:-3],
                           "timestamp": fm.get("timestamp")})
    except TransportError:
        return [], False  # lease state UNKNOWN -> fail closed, never assert vacant
    return [str(l.get("agent"))
            for l in roles.fresh_holders(leases, now=now, sla_hours=sla)], True


# --- role routing on the read folds ---------------------------------------
#
# A directive assigned to a role is directed at whoever holds a fresh lease on it,
# which is the reason role-based identity exists at all: work addressed to a role
# must outlive the session that was holding it. So every read fold that answers
# "what needs me" — `briefing`, `inbox`, `needs-me`, and `queue` — must fold the
# holder's role inboxes, not just their identity inbox; otherwise a role-addressed
# `tell` returns 0 and silently lands in a fold nobody reads.
#
# One resolver for every caller (`_held_roles_for_rows`). The alternative — each
# fold resolving roles its own way — lets the paths diverge, and the failure is
# invisible by construction (a fold that resolves no roles looks exactly like an
# agent who holds none).
_ROLE_DEGRADED = "role-degraded"


def _role_degraded_row(roles_unknown: "set[str] | list[str]") -> dict[str, Any]:
    """The marker for roles whose holder set could NOT be determined — shape
    ``{type, roles}``, same family as ``review-role-degraded`` (which reports the
    same UNKNOWN for the review fold). Never omitted: an unresolved role means
    role-routed work may be missing from the fold, and "unknown" must never render
    as "nothing for you"."""
    return {"type": _ROLE_DEGRADED, "roles": sorted(roles_unknown)}


def _role_degraded_line(r: dict[str, Any]) -> str:
    return (f"  role resolution degraded: {', '.join(r.get('roles') or [])} — "
            f"your role inbox is unknown (not empty); role-routed work may be "
            f"missing, retry")


def _held_roles_for_rows(
    transport: Any, team: str, agent: str, rows: list[dict[str, Any]], *,
    now: str, skip_slugs: "Optional[set[str]]" = None,
    deadline_seconds: Optional[float] = None,
) -> tuple[set[str], set[str]]:
    """Roles ``agent`` holds a FRESH lease on, among the role-shaped assignees the
    given rows actually reference. Returns ``(held, unresolved)``.

    The candidate set is the first bound: only DISTINCT foreign assignees on OPEN
    rows are probed, and the roles/ LISTING (one op, cached for the pass) settles
    which of them are roles at all — so the literal-agent-id majority costs ZERO
    reads, and only genuine roles pay. A team with no role-addressed open work pays
    nothing. Self / ``*`` / ``@backlog`` / path-shaped assignees are skipped without
    a read. ``skip_slugs`` lets queue readers narrow further to UNSEEN directives (an
    already-fired id needs no route).

    **The honest op bound.** A pass costs::

        1 + SUM over probed roles r of (2 + L_r)

    ops: one roles/ listing, then per probed role a doc read + a lease listing +
    ``L_r`` shard reads. ``L_r`` is the number of ``.md`` shards in the role's
    leases/ prefix — one per agent that has ever claimed the role and not
    ``roles release``-d it. Nothing prunes an abandoned shard, so ``L_r`` tracks
    lifetime holder CHURN, not current holders, and is unbounded in principle: a
    role with ten lease shards costs 13 ops, not 4. ``3R`` is only the ``L_r == 1``
    special case. "Probed roles" = the candidates the roles/ listing confirms are
    roles; if that listing RAISES, membership is unknown and EVERY candidate is
    probed at 1 op (its doc read) plus the lease terms for those whose docs parse.
    A transport op is a `fulcra-api` subprocess + HTTPS round trip (~0.8s measured)
    and this runs on `briefing` — the hot path — so the terms matter. The per-role
    ops buy a FAIL-CLOSED answer: reading the agent's own lease shard directly
    would be 1 op, but ``read()`` can't tell absent from failed, which is exactly
    why ``_held_roles`` (the older sweep) reports a transport outage as "no roles".

    **The wall-clock bound** is what actually holds under a degraded transport,
    because no op count bounds LATENCY when each op can burn a full transport
    timeout. One cumulative ``COORD_ROLE_FOLD_BUDGET`` deadline opens here — before
    the roles/ listing, which is itself a blocking op (the recurring pre-budget
    class) — and is spent across the listing, every role, and every lease shard
    within a role. Total latency is the budget plus ONE transport timeout of
    overshoot, no matter how many roles or shards exist.

    On a budget cut every candidate not FINISHED — unscanned, or scanned partway —
    lands in ``unresolved``, never in "not held". Running out of time is UNKNOWN,
    the same as a failed read: serving a role-blind queue because the clock ran out
    is the exact failure this fold exists to close.

    The prefilter is PER PASS, never persistent: leases change, and a name later
    registered as a role must route on the very next fold (the staleness hole that
    got a persistent negative cache rejected for queue reads — see there).

    ``unresolved`` is FAIL-CLOSED and load-bearing: a role whose lease state is
    UNKNOWN (see ``_role_fresh_holders``) is neither held nor not-held. Callers
    MUST surface it (``_role_degraded_row``) rather than let it fold into "no
    roles" — that would be the original silent bug one layer down.
    """
    if deadline_seconds is None:
        deadline_seconds = _role_fold_budget()
    candidates: set[str] = set()
    for r in rows:
        if r.get("status") not in directives.OPEN_STATUSES:
            continue
        a = str(r.get("assignee") or "")
        if not a or a in (agent, "*", directives.BACKLOG) or "/" in a:
            continue
        if skip_slugs is not None:
            slug = str(r.get("name") or "")
            if not slug or slug in skip_slugs:
                continue
        candidates.add(a)
    held: set[str] = set()
    unresolved: set[str] = set()
    listing_cache: dict[str, Any] = {}  # one roles/ listing per pass
    # The pass's ONE deadline opens HERE — ahead of the roles/ listing, not after
    # it. That listing is a blocking op like any other, and a deadline opened past
    # it leaves a transport timeout sitting AHEAD of the budget (the pre-budget
    # class the review fold was bitten by). Everything below spends this same
    # deadline cumulatively: the listing, each role's doc + lease listing, and each
    # lease shard read within a role.
    dl = Deadline.open(deadline_seconds)
    if candidates:
        # Prime the cache `_role_fresh_holders` already consults, and use it to
        # drop candidates that are affirmatively NOT roles before paying a read
        # for them. A listing that RAISES (names is None) means membership is
        # unknown: probe every candidate exactly as before — a role with a
        # readable doc still resolves off its leases, and skipping here would
        # manufacture a degraded marker for work we can in fact route.
        listing_cache["names"] = _roles_listing_names(transport, team)
        names = listing_cache["names"]
        if names is not None:
            candidates = {c for c in candidates if f"{c}.md" in names}
    ordered = sorted(candidates)
    for i, role in enumerate(ordered):
        if dl.expired():
            # Budget cut. Every candidate we have not FINISHED is UNKNOWN — mark
            # the whole tail unresolved and stop. The alternative (return what we
            # got) renders a role-blind queue that is indistinguishable from "you
            # hold no roles", which is the silent failure this fold exists to
            # close, now triggered by a slow transport instead of a missing fold.
            # A candidate scanned PARTWAY degrades inside `_role_fresh_holders`
            # (it shares this deadline) and comes back ok=False, so it lands in
            # `unresolved` through the branch below — no candidate can be dropped
            # by the clock without being reported.
            unresolved.update(ordered[i:])
            break
        holders, ok = _role_fresh_holders(transport, team, role, now=now,
                                          listing_cache=listing_cache, deadline=dl)
        if not ok:
            unresolved.add(role)
            continue
        if agent in holders:
            held.add(role)
    return held, unresolved


def _pending_reviews_for(
    transport: Any, team: str, agent: str, *, deadline_seconds: Optional[float] = None,
    deadline: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Reviews whose pending_required names the agent — directly or via a role
    it holds a fresh lease on. Best-effort: the top listing failing yields []
    (needs-me/briefing must not fail because the review add-on is absent).

    Bounded. Two guards keep a degraded transport from turning this into a
    multi-minute hang read as a store outage:

    - **Settled-skip.** Each unsettled review costs one verdicts listing + a doc
      read + a read per verdict. Once a review is terminal-APPROVED with no
      outstanding required reviewers, a `.settled` marker is dropped IN the
      verdicts prefix; the ONE listing this fold already does then reveals it and
      the slug is skipped with ZERO further reads. The fold also drops that marker
      the first time it computes such a tally, so settled history stops costing.

    - **Aggregate budget.** A wall-clock deadline (default 45s, env
      ``COORD_REVIEW_FOLD_BUDGET``) checked BETWEEN slugs. On breach the scan
      STOPS and a ``review-fold-degraded`` marker (``scanned``/``total``) is
      appended — never a clean-looking partial. A single slug whose tally raises
      ``TransportError`` (Task-1 timeout) or whose review DOC read returns None
      (``read()`` never raises — None here means the read failed, since the slug
      came from the listing) is skipped, counted in ``skipped``, and surfaced
      via the same marker (an unreadable slug is UNKNOWN — not settled, not
      silently pending; partial knowledge must be VISIBLE).

    If review counts keep growing the right home for this is the reconcile
    pre-fold (like task rows) — tracked on the bus."""
    if deadline_seconds is None:
        deadline_seconds = _review_fold_budget()
    # The review fold owns a standalone budget, but bundled callers also have a
    # shared aggregate deadline.  Spend whichever expires first.  Before this
    # clamp, ``briefing`` claimed one cumulative add-on budget while reviews
    # silently opened a fresh 45-second window; on a team with 193 historical
    # review directories the session wake could expire here before current tasks
    # were rendered.  Accept the absolute instant so time already spent by an
    # earlier bundled section is preserved rather than reset.
    # Preserve the standalone fold's historical measurable-progress contract:
    # its own budget opens after the top-level listing.  A bundled caller passes
    # an already-open absolute deadline, which *must* include that listing time.
    dl: Optional[Deadline] = None
    if deadline is not None:
        # Re-open from the smaller REMAINING budget rather than constructing from
        # the absolute instant.  ``Deadline.reserve`` needs the retained budget
        # value to protect the doc scan from orphan-classification starvation;
        # the bare constructor deliberately has no reservable budget.
        remaining = max(0.0, deadline - time.monotonic())
        dl = Deadline.open(min(deadline_seconds, remaining))
    out: list[dict[str, Any]] = []
    now = _iso(_now())
    role_holders: dict[str, list[str]] = {}
    degraded_roles: set[str] = set()  # roles whose lease read was UNKNOWN (fail-closed)
    roles_listing_cache: dict[str, Any] = {}  # one roles/ listing per pass (doc-None disambiguation)
    try:
        entries = transport.list_dir(f"team/{team}/review/")
    except TransportError:
        return []
    slug_entries = [
        e for e in entries
        if not e.get("is_dir") and (e.get("name") or "").endswith(".md")
    ]
    # The fold's ONE deadline opens HERE — before the dir-classification loop, not
    # after it (the recurring pre-budget class): classification does
    # one verdicts listing per dir-only slug, and the store's soft deletes make
    # those dirs permanent (15 tombstones today, forever) — under a degraded
    # transport an unbudgeted loop is up to N x timeout of listings AHEAD of the
    # budget, the same shape the presence fold guards against. Everything below —
    # classification and
    # the doc scan — spends this same budget cumulatively.
    total = len(slug_entries)
    scanned = 0
    skipped = 0
    if dl is None:
        dl = Deadline.open(deadline_seconds)
    if dl.expired():
        return [budget_mod.degraded_row("review-fold-degraded", 0, total)]
    # Dir-only review slugs (a `<slug>/` dir with no `<slug>.md` doc) are invisible
    # to the doc-keyed scan below. Classify each via the tombstone three-way (one
    # verdicts listing apiece): a dir with real verdict shards is an ORPHAN (surface
    # a `review-orphan` row EVERY pass — repair stays a human/maintainer action); an
    # EMPTY dir (no shards, or only a stale `.settled` marker) is a soft-delete
    # TOMBSTONE carrying zero information — skip it silently (no orphan, no [?] row);
    # a verdicts listing that RAISES is UNKNOWN — fail closed, surface a per-dir
    # `review-orphan-degraded` row (never assume tombstone on transport failure).
    # BUDGETED (the recurring pre-budget class): soft deletes make these dirs
    # permanent, so under a degraded transport an unbudgeted loop is up to
    # N x timeout of listings AHEAD of the fold's budget. Classification runs
    # under a RESERVED sub-deadline — half the fold budget — so the doc scan (the
    # load-bearing output) always keeps the other half and its measurable-progress
    # guarantee (the reserved-budget pattern from the reconcile starve fix; a
    # visibility-only pass must never starve the critical one). The sub-deadline
    # is checked before each listing (equivalently after the previous — adjacent
    # iterations — so an overrun is detected immediately; overshoot is bounded by
    # ONE listing, whose completed result is definitive knowledge and is kept).
    # On breach the REMAINING unclassified dirs fold into ONE aggregate
    # `review-orphan-degraded` row ({unclassified: k}) — their state is UNKNOWN,
    # never assumed tombstone — and the fold proceeds to the doc scan with the
    # budget that remains (its existing between-slug/mid-slug checks, against the
    # full fold deadline, then govern).
    classify_dl = dl.reserve(0.5)
    doc_slugs = {(e.get("name") or "")[:-3] for e in slug_entries}
    dir_slugs = []
    for e in entries:
        if not e.get("is_dir"):
            continue
        oslug = (e.get("name") or "").rstrip("/")
        if oslug and oslug not in doc_slugs:
            dir_slugs.append(oslug)
    for i, oslug in enumerate(dir_slugs):
        if classify_dl.expired():
            out.append({"type": "review-orphan-degraded",
                        "unclassified": len(dir_slugs) - i})
            break
        kind = _classify_orphan_dir(transport, team, oslug)
        if kind == "orphan":
            out.append({"type": "review-orphan", "name": oslug})
        elif kind == "unknown":
            out.append({"type": "review-orphan-degraded", "name": oslug})
        # tombstone -> silently skipped
    for e in slug_entries:
        # Budget is checked BETWEEN slugs (after at least one is scanned, so a
        # slow transport still makes measurable progress before degrading).
        if scanned and dl.expired():
            out.append(budget_mod.degraded_row(
                "review-fold-degraded", scanned, total, skipped))
            return out
        slug = (e.get("name") or "")[:-3]
        scanned += 1
        try:
            ventries = transport.list_dir(_verdicts_prefix(team, slug))
            if any((x.get("name") or "") == SETTLED_MARKER for x in ventries):
                continue  # settled -> skip entirely, zero reads beyond this listing
            doc_raw = transport.read(_review_doc_path(team, slug))
            if doc_raw is None:
                # The slug came from the review/ listing, so its doc exists —
                # a None read is a transport failure (read() returns None on
                # timeout, it never raises). The slug's state is UNKNOWN: not
                # settled, not silently pending. Count it, keep scanning.
                skipped += 1
                continue
            if dl.expired():
                # The doc read itself pushed us over budget: check AFTER the
                # blocking op, not only between slugs. Don't start the verdict
                # reads — this slug is UNKNOWN. Count it skipped and surface the
                # degraded marker; the budget is spent.
                skipped += 1
                out.append(budget_mod.degraded_row(
                    "review-fold-degraded", scanned, total, skipped))
                return out
            tally, vreads_ok, fully = _tally_from_verdict_entries(
                transport, team, slug, ventries, doc_raw, deadline=dl.instant)
            if not fully:
                # Budget expired MID-SLUG: a single review with many verdict
                # shards would otherwise read them all unbounded. The partial
                # tally is untrusted. This slug was reached (scanned already
                # counts it), so it joins `skipped` — same accounting as a
                # doc-read failure (scanned includes skipped; unscanned=total-scanned).
                # The budget is spent: stop and surface the degraded marker.
                skipped += 1
                out.append(budget_mod.degraded_row(
                    "review-fold-degraded", scanned, total, skipped))
                return out
        except TransportError:
            # A single slug's tally timed out (Task-1 contract): skip it, keep
            # scanning the rest, and make the gap visible via `skipped` below.
            skipped += 1
            continue
        state = tally.get("state")
        pending = tally.get("pending_required") or []
        if state == review.APPROVED and not pending:
            # Cache only a PROVEN settle: non-empty required (false-settle
            # guard, see _is_settleable) AND every listed verdict actually read
            # (an unreadable verdict could be a hidden CHANGES).
            if _is_settleable(tally) and vreads_ok:
                _write_settled_marker(transport, team, slug, now=now)
            continue
        if state != "PENDING" or not pending:
            continue
        if agent not in pending:  # direct hit needs no role folding at all
            for r in pending:
                if r not in role_holders:
                    holders, ok = _role_fresh_holders(
                        transport, team, r, now=now,
                        listing_cache=roles_listing_cache)
                    role_holders[r] = holders
                    if not ok:
                        # Fail-closed: the role's lease read is UNKNOWN. Do NOT let
                        # it read as "no holders" (a silently dropped obligation) —
                        # record it so a degraded marker surfaces below.
                        degraded_roles.add(r)
        if review.is_pending_for(pending, agent, role_holders):
            out.append({"type": "review-pending", "name": slug,
                        "state": "PENDING", "pending_required": pending})
    if degraded_roles:
        # A role's lease read degraded: the agent might be a holder we couldn't
        # resolve, so a role-routed obligation may be missing. Make it VISIBLE.
        out.append({"type": "review-role-degraded",
                    "roles": sorted(degraded_roles)})
    if skipped:
        # Completed inside budget but some slugs were unreadable: partial
        # knowledge must be visible, so emit the degraded marker anyway.
        out.append(budget_mod.degraded_row(
            "review-fold-degraded", scanned, total, skipped))
    return out


def _review_degraded_line(r: dict[str, Any]) -> str:
    return budget_mod.fold_degraded_line(
        r, label="review", remedy="run per-slug review status for the rest",
        noun="slug")


def _normalize_required(required: Any) -> list[str]:
    """Coerce a doc's ``required:`` field (list or legacy comma-string) into a
    clean list of stripped, non-empty reviewer names — the shape `review.tally`
    and the request-identity comparison both consume."""
    if isinstance(required, str):
        return [r.strip() for r in required.split(",") if r.strip()]
    if isinstance(required, list):
        return [str(r).strip() for r in required if str(r).strip()]
    return []


def _review_request_diff(
    fm: dict[str, Any], *, of: Any, required: list[str], requested_by: str,
) -> Optional[tuple[str, str, str]]:
    """Compare an existing review doc's frontmatter against the request being made.

    Returns ``None`` when it is the SAME request (idempotent recovery), else
    ``(field, existing_value, requested_value)`` naming the FIRST identity field
    that differs. Request identity is ``requested_by`` + ``of`` + the required SET
    (order-normalized): a different requester re-opening someone else's review is a
    conflict (not a silent recovery), and a changed required set re-opens a review
    only via a NEW slug (the settled-review immutability contract)."""
    ex_rb = str(fm.get("requested_by") or "")
    if ex_rb != (requested_by or ""):
        return ("requested_by", ex_rb, requested_by or "")
    ex_of = str(fm.get("of") or "")
    if ex_of != (str(of) if of is not None else ""):
        return ("of", ex_of, str(of) if of is not None else "")
    ex_req = sorted(_normalize_required(fm.get("required")))
    if ex_req != sorted(required):
        return ("required set", ", ".join(ex_req), ", ".join(sorted(required)))
    return None


def _deliver_all_review_directives(
    transport: Any, team: str, slug: str, required: list[str], *, owner: str, of: str,
) -> tuple[list[str], list[str]]:
    """Deliver ONE directive per required reviewer through the canonical hash-slug
    path. Returns ``(delivered, failed)``. Payload-hash dedup makes this idempotent:
    a reviewer whose directive already landed re-verifies as "already delivered"
    (rc 0), so this is safe to re-run on a recovery retry — it fills the gaps."""
    delivered: list[str] = []
    failed: list[str] = []
    for r in required:
        if _deliver_review_directive(transport, team, slug, r,
                                     sender=owner, of=of) == 0:
            delivered.append(r)
        else:
            failed.append(r)
    return delivered, failed


def _print_partial_review_failure(
    slug: str, delivered: list[str], failed: list[str], *, doc_note: str,
) -> None:
    """The loud partial-failure line: names exactly who was NOT notified and who
    was, and points the requester at the retry that dedupes the delivered ones."""
    print(f"review {slug} {doc_note} but reviewer notification FAILED for: "
          f"{', '.join(failed)} (delivered: {', '.join(delivered) or 'none'}) — "
          f"retry the request to re-notify; delivered directives dedupe by payload "
          f"hash", file=sys.stderr)


def _print_review_success(
    args: argparse.Namespace, team: str, slug: str, required: list[str], *,
    recovered: bool,
) -> None:
    if recovered:
        print(f"review {slug} already exists (matching) — re-verified reviewer "
              f"delivery (required: {', '.join(required)})")
    else:
        print(f"review {slug} requested (required: {', '.join(required)})")
    for r in required:
        print(f"  reviewer {r} -> file verdict at {_verdicts_prefix(team, slug)}{r}.md")
    # Point the requester at the await primitive for the verdict wait (they poll
    # `review status`; queue is the same explicit read discipline every ask uses).
    sender = _known_sender(args)
    if sender:
        print(f"await verdicts: coord-engine inbox {team} --agent {sender}")


def cmd_review_request(args: argparse.Namespace, transport: Any) -> int:
    """Open a review with named REQUIRED reviewers, making the obligation
    structurally durable: the doc lands at the SAME path `_review_tally` reads
    (`_review_doc_path`), so each required reviewer's `pending_required` marker
    surfaces in `needs-me` and stays there until their verdict file exists.

    Requesters SHOULD name roles, not identities (role-routing doctrine) — a
    role name is resolved to its fresh lease holders by the needs-me fold."""
    team = args.team
    # A title slugs like `tell` slugs titles; an already-slug-like arg round-trips
    # through the same helper unchanged (single path segment).
    slug = tasks.slugify(args.name)
    required = [r.strip() for r in (args.reviewer or []) if r and r.strip()]
    if not required:
        # An empty/whitespace-only --reviewer list would gate on nothing: the
        # tally has no pending_required marker, so any stray verdict flips the
        # review to APPROVED and no reviewer ever sees it in needs-me. Refuse,
        # writing no doc, rather than open a review that gates on nothing.
        print("review request needs at least one non-empty --reviewer",
              file=sys.stderr)
        return 2
    path = _review_doc_path(team, slug)
    owner = getattr(args, "sender", None) or _host()
    existing = transport.read(path)
    if existing is not None:
        # A doc already occupies the slot. This is NOT automatically a conflict:
        # the atomic-delivery partial-failure path below tells the requester to
        # RETRY, and after a partial failure the doc necessarily EXISTS — so a
        # blanket "already exists" rc 1 would strand the un-notified reviewers
        # forever (the exact orphan class this command exists to kill). Parse the
        # doc and adjudicate: matching request -> idempotent recovery; different
        # request -> loud conflict; unparseable -> loud, never overwrite.
        existing_fm = okf.parse_frontmatter(existing)
        if existing_fm is None:
            # Present but unparseable/corrupt: we cannot prove it is OUR request,
            # and overwriting could clobber a live review. Fail loud, never write.
            print(f"review {slug} already exists but is unreadable (corrupt "
                  f"frontmatter) — cannot verify, will not overwrite; retry",
                  file=sys.stderr)
            return 1
        diff = _review_request_diff(existing_fm, of=args.of, required=required,
                                    requested_by=owner)
        if diff is not None:
            field, existing_val, requested_val = diff
            print(f"review {slug} already exists with a different {field} "
                  f"(existing: {existing_val!r}, requested: {requested_val!r}) — a "
                  f"different {field} re-opens a review only via a new slug; "
                  f"refusing to overwrite", file=sys.stderr)
            return 1
        # IDEMPOTENT RECOVERY: same requested_by + of + required set. Skip the doc
        # write (it already holds our request), keep the harmless stale-marker
        # delete (a prior fold may have settled it; its absence just makes the next
        # fold recompute), and RE-RUN reviewer delivery for EVERY required reviewer
        # — hash-path dedup re-verifies the ones that landed (rc 0 "already
        # delivered") and delivers the ones a prior partial failure dropped. This
        # is what makes a partial-delivery retry CONVERGE instead of dying here.
        transport.delete(_settled_marker_path(team, slug))
        delivered, failed = _deliver_all_review_directives(
            transport, team, slug, required, owner=owner, of=args.of)
        if failed:
            _print_partial_review_failure(slug, delivered, failed,
                                          doc_note="already exists (matching)")
            return 1
        _print_review_success(args, team, slug, required, recovered=True)
        return 0
    # existing is None is AMBIGUOUS (a read timeout and a genuinely-absent doc
    # both map to None). Treating it as an empty slot would let a degraded transport
    # clobber a live review. Confirm absence via a directory
    # listing before writing: list_dir RAISES TransportError on failure (loud
    # through main's catch-all), and its entry names distinguish missing from
    # present-but-unreadable. One list_dir per request is cheap.
    parent, entry = path.rsplit("/", 1)
    names = {e.get("name") for e in transport.list_dir(parent + "/")}
    if entry in names:
        # Present in the listing yet the read returned None: transport degraded
        # mid-op. We cannot verify what the doc holds and must not overwrite it.
        print(f"review {slug}: doc present but unreadable (transport degraded) — "
              f"cannot verify, will not overwrite; retry", file=sys.stderr)
        return 1
    # Genuinely absent -> write the fresh review doc.
    fm = {
        "type": "Review",
        "schema": "review-request/v1",
        "requested_by": owner,
        "of": args.of,
        "required": required,
        "ts": _iso(_now()),
    }
    body = f"\nReview requested: {args.of}\n"
    if not transport.write(path, okf.render_frontmatter(fm) + body):
        # A timed-out write returns False, not a raise. An rc-0 "review requested"
        # that never landed is the requester-side mirror of a lost write: fail loud
        # so the requester retries rather than believing the obligation is durable.
        print("review request write failed (transport)", file=sys.stderr)
        return 1
    # A fresh doc can carry no stale `.settled` marker, but a since-deleted-and-
    # reopened slug at the same path could; clear it best-effort (delete is
    # timeout-safe -> False, which we ignore) so the next fold recomputes.
    transport.delete(_settled_marker_path(team, slug))
    # Atomic notification: with the doc durably landed, deliver ONE directive per
    # required reviewer through the canonical hash-slug directive path, so a
    # verb-opened review appears in the reviewer's inbox/queue — this is what removes
    # the reason agents hand-send review tells (which historically produced
    # orphaned reviews: a directive with no verdict target) and makes
    # the requester's `await verdicts` breadcrumb is genuine. Same write-verification discipline
    # as the doc: any reviewer-directive fail is reported LOUD naming exactly what
    # landed and what did not (partial is never silent), and the requester's retry
    # re-enters the idempotent-recovery path above to fill the gaps.
    delivered, failed = _deliver_all_review_directives(
        transport, team, slug, required, owner=owner, of=args.of)
    if failed:
        _print_partial_review_failure(slug, delivered, failed,
                                      doc_note="requested (doc written)")
        return 1
    _print_review_success(args, team, slug, required, recovered=False)
    return 0


def cmd_review_status(args: argparse.Namespace, transport: Any) -> int:
    team, slug = args.team, args.slug
    result, doc_ok, vreads_ok, listing_ok = _review_tally(transport, team, slug)
    if not doc_ok:
        # The doc read returned None: no doc. If the verdicts dir is also empty
        # (or holds only a stale `.settled` marker), this is a TOMBSTONE — an
        # archived/deleted review whose dir prefix soft-deletes lingered. Keep rc 1
        # (still non-clean for a caller sweep), but say tombstone: a retry never
        # resurrects a gone doc, so the generic "unknown, retry" would be dishonest.
        # A dir with real verdict shards (orphan) or a verdicts listing that RAISED
        # (unknown) is NOT a tombstone — fall through to the generic fail-closed
        # message, where a retry may genuinely help.
        if _classify_orphan_dir(transport, team, slug) == "tombstone":
            print(f"review status: {slug} in team/{team} is a tombstone "
                  f"(archived/deleted review) — no doc, no verdicts",
                  file=sys.stderr)
            return 1
        # Missing slug OR transport failure — indistinguishable, and either way the
        # tally is UNKNOWN. Without the required list, one readable approval verdict
        # tallies as a clean APPROVED with pending:[] — printing that (or caching
        # it) under a transient timeout would durably hide a pending review. Fail loud.
        print(f"review status failed: {_review_doc_path(team, slug)} unreadable "
              f"(missing slug or degraded transport) — tally unknown, retry",
              file=sys.stderr)
        return 1
    if not listing_ok:
        # The verdicts LISTING raised, so `_review_tally` fell back to
        # entries=[] and the tally is a floor built over ZERO verdicts —
        # vreads_ok is vacuously True. Printing that (a false PENDING) rc 0 gives
        # clean output on a failed listing, and letting the marker-delete self-heal below
        # run on it would DELETE a legitimate `.settled` marker off a vacuous
        # non-settleable tally. Fail closed FIRST — same register as the doc /
        # shard-unreadable cases — so neither the report nor the marker-delete
        # gate is ever reached on an unknown tally.
        print(f"review status failed: verdicts listing unreadable under "
              f"{_verdicts_prefix(team, slug)} — tally unknown, retry",
              file=sys.stderr)
        return 1
    if not vreads_ok:
        # A listed verdict shard read returned None (the file EXISTS, its
        # content is unknown under a degraded transport). The tally is a FLOOR,
        # not the truth — a lost CHANGES verdict reads as APPROVED. Printing that
        # partial tally rc 0 defeats the exact-slug fail-closed sweep watchers
        # run. Fail closed, same register as the doc-unreadable case.
        print(f"review status failed: verdict shard unreadable under "
              f"{_verdicts_prefix(team, slug)} — tally unknown, retry",
              file=sys.stderr)
        return 1
    # A direct query recomputes the truth (never trusts the marker). doc_ok and
    # vreads_ok are both proven above, so the tally is trustworthy here.
    if _is_settleable(result):
        # PROVEN terminal-settled (non-empty required, every listed verdict read):
        # refresh the fold cache so the fan-out fold can skip this slug next time.
        _write_settled_marker(transport, team, slug, now=_iso(_now()))
    else:
        # A full, trustworthy tally that is NOT settleable, yet a `.settled`
        # marker may linger (e.g. a since-reopened review). It is provably stale —
        # the marker only ever caches a terminal-APPROVED state. Best-effort
        # delete (delete is timeout-safe -> False, ignored) so the next fan-out
        # fold recomputes and sees the pending obligation, complementing the
        # re-request delete. Self-healing on direct query.
        transport.delete(_settled_marker_path(team, slug))
    result.update({"team": team, "slug": slug})
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"review {slug} in team/{team}: {result['state']}")
        if result["approvals"]:
            print("  approvals: " + ", ".join(result["approvals"]))
        if result["changes"]:
            print("  changes requested: " + ", ".join(result["changes"]))
        if result["pending_required"]:
            print("  awaiting required: " + ", ".join(result["pending_required"]))
    return 0


# --- continuity (fulcra-agent-continuity snapshots) ---

def _continuity_path(team: str, agent: str, task: str) -> str:
    return f"team/{team}/member/{agent}/continuity/{task}/latest.json"


def _continuity_prefix(team: str, agent: str) -> str:
    return f"team/{team}/member/{agent}/continuity/"


def cmd_continuity_snapshot(args: argparse.Namespace, transport: Any) -> int:
    task = tasks.slugify(args.task)  # single path segment; a slash breaks the no-task fold
    snap = continuity.build_snapshot(
        agent=args.agent, task=task, objective=args.objective, now=_iso(_now()),
        decisions=args.decision, next_actions=args.next, open_questions=args.open_question,
        artifacts=args.artifact, context_used_percent=args.context_percent,
        transcript_path=args.transcript,
    )
    transport.write(_continuity_path(args.team, args.agent, task), json.dumps(snap, indent=2))
    print(f"snapshot {snap['checkpoint_id']}")
    return 0


def _agent_snapshots(transport: Any, team: str, agent: str) -> list[dict[str, Any]]:
    """All of one agent's latest-per-task continuity snapshots.

    Same transport mechanism ``cmd_continuity_resume`` uses to find an agent's
    single latest snapshot — here every task's ``latest.json`` is collected so
    the health audit can fold across agents.
    """
    snaps: list[dict[str, Any]] = []
    try:
        for e in transport.list_dir(_continuity_prefix(team, agent)):
            n = (e.get("name") or "").rstrip("/")
            if not e.get("is_dir") or not n:
                continue
            raw = transport.read(_continuity_path(team, agent, n))
            if raw:
                try:
                    snaps.append(json.loads(raw))
                except Exception:
                    pass
    except TransportError:
        pass
    return snaps


def cmd_continuity_resume(args: argparse.Namespace, transport: Any) -> int:
    if args.task:
        raw = transport.read(_continuity_path(args.team, args.agent, tasks.slugify(args.task)))
        try:
            snap = json.loads(raw) if raw else None
        except Exception:
            snap = None
    else:
        snap = continuity.latest(_agent_snapshots(transport, args.team, args.agent))
    if args.json:
        print(json.dumps(snap, indent=2))
    else:
        print(continuity.render_resume(snap))
    return 0


# --- directives (fulcra-agent-directives) ---

def _ack_path(team: str, slug: str, agent: str) -> str:
    return f"team/{team}/_coord/acks/{slug}/{tasks.agent_key(agent)}.md"


def _responses_prefix(team: str) -> str:
    return f"team/{team}/_coord/responses/"


def _response_path(team: str, slug: str, stamp: str) -> str:
    return f"team/{team}/_coord/responses/{slug}/{stamp}.md"


def _stamp_for_path(now: str, agent: str) -> str:
    safe_time = now.replace(":", "").replace("-", "").replace(".", "")
    return f"{safe_time}-{tasks.agent_key(agent)}"


def _directive_payload(title: Optional[str], summary: Optional[str],
                       next_action: Optional[str],
                       assignee: Optional[str]) -> tuple[str, str, str, str]:
    """The message-identity fields — title, summary, next_action, ASSIGNEE.

    Identity == path: ``_create_directive`` hashes this payload into the canonical
    directive slug (``<title-slug>-<sha256(payload)[:8]>``), so identical payloads
    map to one path (dedupe by construction) and distinct payloads to distinct
    paths (they can never race). Timestamp, owner, and not_before are delivery
    metadata, not the message, so they never enter the identity/dedup comparison
    (a relay re-sending the same reminder to the same agent is the same message).
    Assignee IS identity: the
    same text told to a DIFFERENT agent is a different directive (each recipient
    must get their copy), while broadcast's ``*`` audience means identical
    re-broadcasts still dedupe — and a broadcast stays distinct from a directed
    tell of the same text (different audiences). None and "" normalize to the
    same value so a missing summary compares equal to an empty one.

    By design, not_before and priority are delivery metadata OUTSIDE this
    identity, so a reschedule or priority change of the same title dedupes onto
    the original doc (keeping its schedule) rather than re-delivering: to re-arm
    with a new schedule or priority, send a new title."""
    def norm(x: Optional[str]) -> str:
        return "" if x is None else str(x)
    return (norm(title), norm(summary), norm(next_action), norm(assignee))


def _doc_payload(doc: Optional[str]) -> Optional[tuple[str, str, str, str]]:
    """Message-identity payload of an existing task doc, or ``None`` when its
    frontmatter won't parse. On the write path an unparseable/corrupt doc at our
    canonical (hash-bearing) slot can no longer be a colliding DIFFERENT message —
    only corruption — so the caller fails loud (cannot verify delivery) rather
    than overwriting: never claim a delivery we can't confirm."""
    fm = okf.parse_frontmatter(doc)
    if fm is None:
        return None
    return _directive_payload(fm.get("title"), fm.get("description"),
                              fm.get("next_action"), fm.get("assignee"))


def _payload_hash(payload: tuple[str, str, str, str]) -> str:
    """Stable short id carried by EVERY directive slug. Hashes the payload (NOT
    the time), so a retry of the same message maps to the same slug (dedupe) and
    distinct messages to distinct slugs (no shared slot to race over)."""
    return hashlib.sha256("\x00".join(payload).encode("utf-8")).hexdigest()[:8]


def _write_directive(transport: Any, args: argparse.Namespace, *, slug: str,
                     content: str, payload: tuple[str, str, str, str], assignee: str,
                     not_before: Optional[str]) -> int:
    """Deliver ``content`` at ``slug`` — whose canonical path already carries the
    payload hash (see ``_create_directive``), so the path IS the message identity.

    Two senders of the SAME payload compute the SAME path and write the SAME
    bytes: a race is idempotent (last-writer-wins is a no-op), so the existence
    of the slot means "already delivered". Distinct payloads land on DISTINCT
    paths and can never race each other — the lost-race case that the old
    read-back guarded against cannot arise, so a read-back MISMATCH now means
    only transport corruption (or an astronomically improbable hash collision),
    never a racer's different message. We never overwrite and never claim a
    delivery we cannot verify.
    """
    path = _task_path(args.team, slug)
    existing = transport.read(path)
    if existing is not None:
        # The path is the payload identity, so an existing readable doc here IS
        # our message. Matching payload -> sanctioned dedup (already delivered).
        if _doc_payload(existing) == payload:
            print(f"directive {slug} already delivered")
            return 0
        # Present but NOT our payload: unparseable/corrupt content (or a hash
        # collision). We cannot verify our message is the one on the bus and must
        # never overwrite — fail loud so the caller retries.
        print(f"directive {slug}: slot holds unverifiable content, "
              f"cannot verify delivery, retry", file=sys.stderr)
        return 1
    # existing is None is AMBIGUOUS (timeout and genuinely-absent both map to
    # None). Treating it as "empty slot" would let a degraded transport clobber an
    # occupied slot. Confirm absence via a directory listing: list_dir RAISES
    # TransportError on failure (loud through main's catch-all), and its entry
    # names distinguish missing from unreadable. One list_dir per tell is fine.
    parent, entry = path.rsplit("/", 1)
    names = {e.get("name") for e in transport.list_dir(parent + "/")}
    if entry in names:
        # Present in the listing yet the read returned None: transport degraded
        # mid-op. Cannot verify delivery, must not overwrite.
        print(f"directive {slug}: slot present but unreadable "
              f"(transport degraded), cannot verify delivery, retry", file=sys.stderr)
        return 1
    # Genuinely absent -> write. A write that fails (False, not a raise) must
    # NOT be reported as delivered: a failed write leaves the slot empty, so
    # a retry re-enters this dedup logic cleanly.
    if not transport.write(path, content):
        print("directive write failed (transport)", file=sys.stderr)
        return 1
    # Post-write read-back as WRITE-VERIFICATION only: None (read-back failed) or a
    # mismatch (corruption) both mean we cannot confirm our bytes landed -> fail
    # loud rather than claim an unverifiable delivery. A mismatch can no
    # longer mean a lost race (distinct payloads never share this path).
    readback = transport.read(path)
    if readback is None:
        print(f"directive {slug}: write unverifiable (read-back failed, "
              f"transport degraded)", file=sys.stderr)
        return 1
    if _doc_payload(readback) != payload:
        print(f"directive {slug}: write unverifiable (read-back mismatch, "
              f"transport corruption)", file=sys.stderr)
        return 1
    print(f"directive {slug} -> {assignee}"
          + (f" (visible {not_before})" if not_before else ""))
    return 0


def _create_directive(args: argparse.Namespace, transport: Any, *, assignee: str,
                      not_before: Optional[str] = None) -> int:
    # The canonical directive path ALWAYS carries the payload hash: identical
    # payloads (any senders, any order) converge on one path and dedupe by
    # construction; distinct payloads occupy distinct paths and can never race.
    payload = _directive_payload(args.title, args.summary, args.next, assignee)
    slug = f"{tasks.slugify(args.title)}-{_payload_hash(payload)}"
    try:
        _, content = tasks.new_task_doc(
            args.title, now=_iso(_now()), workstream=args.workstream,
            status="proposed", priority=args.priority,
            owner=getattr(args, "sender", None) or _host(), assignee=assignee,
            summary=args.summary or "", next_action=args.next, kind="directive",
            not_before=not_before, slug=slug,
        )
    except tasks.TaskError as e:
        print(f"directive failed: {e}", file=sys.stderr)
        return 1
    rc = _write_directive(transport, args, slug=slug, content=content,
                          payload=payload, assignee=assignee, not_before=not_before)
    # On a delivered ask (not a backlog capture — @backlog awaits no reply), point
    # the sender at the reply leg: the return of `respond` surfaces in their queue.
    if rc == 0 and assignee != directives.BACKLOG:
        sender = _known_sender(args)
        if sender:
            print(_replies_breadcrumb(args.team, sender))
    return rc


def _deliver_review_directive(transport: Any, team: str, slug: str, reviewer: str,
                              *, sender: str, of: str) -> int:
    """Deliver ONE review-request directive to ``reviewer`` via the canonical
    hash-slug directive path — the SAME ``_write_directive`` delivery (payload-hash
    dedup + write-verification) every ``tell`` gets, so a verb-opened review
    NOTIFIES its reviewers instead of relying on a hand-sent tell (which yields
    an orphaned review: a directive sent by hand, with no verdict target). The
    text carries the exact slug AND the verdict-file path (the fail-closed watcher
    contract). Returns ``_write_directive``'s rc (0 delivered/deduped, 1 failed).

    Distinct (slug, reviewer) pairs produce distinct payloads -> distinct paths,
    so reviewers never collide and a re-request idempotently dedupes."""
    verdict_file = f"{_verdicts_prefix(team, slug)}{reviewer}.md"
    title = f"REVIEW REQUEST: {slug}"
    summary = f"Verdict owed on {of} — file it at {verdict_file}"
    next_action = f"file your verdict at {verdict_file}"
    payload = _directive_payload(title, summary, next_action, reviewer)
    dslug = f"{tasks.slugify(title)}-{_payload_hash(payload)}"
    try:
        _, content = tasks.new_task_doc(
            title, now=_iso(_now()), status="proposed", owner=sender,
            assignee=reviewer, summary=summary, next_action=next_action,
            kind="directive", slug=dslug,
        )
    except tasks.TaskError as e:
        print(f"review-request directive for {reviewer} failed: {e}", file=sys.stderr)
        return 1
    # `_write_directive` only needs args.team; a minimal namespace carries it.
    return _write_directive(transport, argparse.Namespace(team=team), slug=dslug,
                            content=content, payload=payload, assignee=reviewer,
                            not_before=None)


def cmd_tell(args: argparse.Namespace, transport: Any) -> int:
    return _create_directive(args, transport, assignee=args.assignee)


def cmd_broadcast(args: argparse.Namespace, transport: Any) -> int:
    return _create_directive(args, transport, assignee="*")


def cmd_remind(args: argparse.Namespace, transport: Any) -> int:
    when = directives.parse_when(args.when, now=_iso(_now()))
    if when is None:
        print(f"remind failed: cannot parse WHEN {args.when!r} (ISO or 5d/36h/10m)", file=sys.stderr)
        return 1
    return _create_directive(args, transport, assignee=args.assignee, not_before=when)


def cmd_later(args: argparse.Namespace, transport: Any) -> int:
    return _create_directive(args, transport, assignee=directives.BACKLOG)


def _update_intent_window(transport: Any, path: str, existing: str, *, slug: str,
                          intent_by: str) -> int:
    """Rewrite ONLY ``intent_by`` on an existing intent doc, in place, then verify
    by read-back — the trust-eroding-false-drop guard from Surface 2.

    THE SEAM (deliberate divergence from ``_write_directive``'s read-back): the
    generic write-verification compares ``_doc_payload`` — title/summary/next/
    assignee — and ``intent_by`` is NOT in that tuple. So a window change is
    INVISIBLE to the generic read-back (it would pass a stale-window write as
    verified). The update therefore does its OWN ``intent_by``-specific read-back:
    None/unparseable/mismatch all mean "cannot confirm the new window landed" ->
    rc 1 retry, never a claimed-but-false deadline. Identity fields (title/
    assignee) are untouched, so the slot keeps its identity and later identical
    restatements still dedupe onto it.
    """
    split = okf.split_frontmatter(existing)
    fm = okf.parse_frontmatter(existing)
    if split is None or fm is None:  # defensive: caller already parsed, but never write blind
        print(f"intent {slug}: doc unparseable, cannot verify, retry", file=sys.stderr)
        return 1
    fm["intent_by"] = intent_by
    content = okf.render_frontmatter(fm) + "\n" + split[1]
    if not transport.write(path, content):
        print("intent window update failed (transport)", file=sys.stderr)
        return 1
    # intent_by-SPECIFIC read-back (the seam): confirm the NEW window is on the bus.
    readback = transport.read(path)
    if readback is None:
        print(f"intent {slug}: window update unverifiable "
              f"(read-back failed, transport degraded), retry", file=sys.stderr)
        return 1
    rb = okf.parse_frontmatter(readback)
    if rb is None or str(rb.get("intent_by") or "") != str(intent_by or ""):
        print(f"intent {slug}: window update unverifiable "
              f"(read-back mismatch, transport degraded), retry", file=sys.stderr)
        return 1
    print("intent window updated")
    return 0


def cmd_intent(args: argparse.Namespace, transport: Any) -> int:
    """Capture a spoken commitment as an ``intent:<principal>`` directive.

    DELIBERATE IDENTITY DEVIATION from the plain directive path: an intent's
    identity is ``text + assignee ONLY`` — ``intent_by`` (the declared window) is
    EXCLUDED from the hash-slug. Restating the SAME commitment with a revised
    deadline must NOT fork a second item, so the window cannot be part of identity;
    but the plain path's "metadata outside identity dedupes onto the original doc"
    rule would then silently PRESERVE a stale deadline on restatement (the
    trust-eroding false-drop). So intent_by gets a VERIFIED in-place update path
    instead (see ``_update_intent_window``). Identity = ``_directive_payload(text,
    None, None, principal)`` — summary/next_action are constant-empty, so the hash
    ranges over text + assignee exactly.

    Delivery reuses the directive machinery: a genuinely-new capture goes through
    ``_write_directive`` (its absence-confirmation, write, and write-verification
    guards — no second delivery implementation). Only the two intent-specific
    outcomes are handled here: identical restatement -> rc 0 "intent already
    captured"; a different ``--by`` -> in-place window update.
    """
    principal = args.principal
    text = args.title
    now_iso = _iso(_now())
    intent_by: Optional[str] = None
    by = getattr(args, "by", None)
    if by:
        intent_by = directives.parse_when(by, now=now_iso)
        if intent_by is None:
            print(f"intent failed: cannot parse --by {by!r} (ISO or 5d/36h/10m)",
                  file=sys.stderr)
            return 1

    # Identity: text + assignee ONLY (intent_by excluded — see docstring).
    payload = _directive_payload(text, None, None, principal)
    slug = f"{tasks.slugify(text)}-{_payload_hash(payload)}"
    path = _task_path(args.team, slug)

    existing = transport.read(path)
    if existing is not None:
        # Present + readable at our hash slot. Confirm it IS our message (identity
        # match); a payload mismatch/unparseable means corruption or a hash
        # collision — never overwrite, fail loud (mirrors _write_directive).
        doc_payload = _doc_payload(existing)
        if doc_payload is None or doc_payload != payload:
            print(f"intent {slug}: slot holds unverifiable content, "
                  f"cannot verify, retry", file=sys.stderr)
            return 1
        # Our intent already exists. Same window (or no --by) -> pure dedup.
        existing_by = (okf.parse_frontmatter(existing) or {}).get("intent_by")
        if intent_by is None or str(existing_by or "") == str(intent_by or ""):
            print("intent already captured")
            return 0
        # A revised deadline: verified in-place window update, never a fork.
        return _update_intent_window(transport, path, existing, slug=slug,
                                     intent_by=intent_by)

    # existing is None -> absent OR present-but-unreadable. Reuse
    # _write_directive's guards: it re-confirms absence via a directory listing
    # (present-but-unreadable -> rc 1 cannot-verify, no overwrite) then writes +
    # verifies. Build the doc with the capture doctrine: intent:<principal> tag +
    # intent_by frontmatter (both invisible to the payload identity).
    try:
        _, base = tasks.new_task_doc(
            text, now=now_iso, status="proposed",
            priority=getattr(args, "priority", None) or "P2",
            owner=getattr(args, "sender", None) or _host(), assignee=principal,
            summary="", next_action=None, kind="directive", slug=slug,
        )
    except tasks.TaskError as e:
        print(f"intent failed: {e}", file=sys.stderr)
        return 1
    fm = okf.parse_frontmatter(base)
    split = okf.split_frontmatter(base)
    if fm is None or split is None:  # unreachable (we just rendered it), never write blind
        print("intent failed: could not build doc", file=sys.stderr)
        return 1
    tags = fm.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    fm["tags"] = tags + [f"intent:{principal}"]
    fm["intent_by"] = intent_by  # None omitted by render_frontmatter (undeclared)
    content = okf.render_frontmatter(fm) + "\n" + split[1]
    return _write_directive(transport, args, slug=slug, content=content,
                            payload=payload, assignee=principal, not_before=None)


def cmd_handoff(args: argparse.Namespace, transport: Any) -> int:
    """Atomic handoff: checkpoint ref + assignee land in ONE task write."""
    path = _task_path(args.team, args.name)
    try:
        out = tasks.apply_update(
            transport.read(path), now=_iso(_now()), assignee=args.to,
            checkpoint_ref=args.checkpoint, next_action=args.next,
        )
    except tasks.TaskError as e:
        print(f"handoff failed: {e}", file=sys.stderr)
        return 1
    transport.write(path, out)
    print(f"handed off {args.name} -> {args.to}"
          + (f" (checkpoint {args.checkpoint})" if args.checkpoint else ""))
    return 0


def _directed_inbox(transport: Any, team: str, agent: str,
                    rows: list[dict[str, Any]], *,
                    held_roles: "Optional[set[str]]" = None,
                    include_backlog: bool = False) -> list[dict[str, Any]]:
    """The open-directive fold over ALREADY-LOADED ``rows`` — directives assigned
    to ``agent``, ``*``, or a role in ``held_roles`` (role routing), with the same
    ack + read-your-write gating `inbox` applies. Split out from
    ``_inbox_rows_status`` so queue reads can resolve held roles from the rows FIRST
    (bounding the lease reads to role-shaped assignees on unseen directives) and
    then fold once, without re-reading the summaries index."""
    now = _iso(_now())
    acks = {str(r.get("name")): (r.get("acked_by") or []) for r in rows}
    stale_visible = directives.inbox(rows, acks, agent, now=now,
                                     include_backlog=include_backlog,
                                     held_roles=held_roles)
    for r in stale_visible:
        slug = str(r.get("name") or "")
        if agent not in (acks.get(slug) or []) and transport.read(_ack_path(team, slug, agent)):
            acks.setdefault(slug, []).append(agent)
    got = directives.inbox(rows, acks, agent, now=now,
                           include_backlog=include_backlog, held_roles=held_roles)
    # read-your-write: an ack written since the last reconcile hides the item
    # for the acking agent immediately (live shard check, only for shown items).
    return [r for r in got
            if transport.read(_ack_path(team, str(r.get("name")), agent)) is None]


def _inbox_rows_status(transport: Any, team: str, agent: str, *,
                       include_backlog: bool = False
                       ) -> tuple[list[dict[str, Any]], bool, str, set[str]]:
    """The open-directive fold `inbox` surfaces for `agent` — role-routed
    directives included — plus the readability of the underlying summaries fold:
    ``ok`` False (with a ``reason``) when the index/listing is UNKNOWN — see the
    public-read failure contract at ``_read_degraded_row``. Extracted so queue
    awaits the SAME source `inbox` shows — one inbox computation, no second
    implementation to drift. Never raises: an unreadable summaries read folds to
    an empty list, but with ``ok=False`` and a ``reason`` so EVERY caller (inbox,
    reads and briefing surface the degradation as the loud marker rather than
    mistaking UNKNOWN for an empty inbox — the silent clean-``[]`` that would
    suppress a live unacked directive.

    The fourth element is the UNRESOLVED role set (``_held_roles_for_rows``): roles
    whose holders could not be determined. The caller MUST surface it — see
    ``_role_degraded_row``."""
    rows, ok, reason = _load_rows_status(transport, team)
    held, unresolved = _held_roles_for_rows(transport, team, agent, rows,
                                            now=_iso(_now()))
    return (_directed_inbox(transport, team, agent, rows,
                            held_roles=held or None,
                            include_backlog=include_backlog),
            ok, reason, unresolved)


def cmd_inbox(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    if args.ack:
        fm = {"type": "Ack", "agent": agent, "timestamp": _iso(_now())}
        transport.write(_ack_path(args.team, args.ack, agent),
                        okf.render_frontmatter(fm) + "\nacked\n")
        print(f"acked {args.ack}")
        return 0
    # Public-read failure contract (see _read_degraded_row): consume the readable
    # bit. Under a degraded transport the summaries index is UNKNOWN, not empty —
    # emit the `inbox-degraded` marker (json row / stderr notice) and RETAIN any
    # partial rows, NEVER a clean-``[]`` exit 0 that would suppress a live unacked
    # directive.
    got, ok, reason, unresolved_roles = _inbox_rows_status(
        transport, args.team, agent, include_backlog=args.all)
    if args.json:
        rows_out = ([_read_degraded_row(reason, marker="inbox-degraded")] + got
                    if not ok else got)
        if unresolved_roles:
            rows_out = [_role_degraded_row(unresolved_roles)] + rows_out
        print(json.dumps(rows_out, indent=2))
        return 0
    if not ok:
        _surface_read_degraded(reason, json_mode=False, marker="inbox-degraded")
    print(f"inbox — {agent}: {len(got)} item(s)")
    if unresolved_roles:  # always shown — an unknown role inbox must never hide
        print(_role_degraded_line(_role_degraded_row(unresolved_roles)))
    for r in got:
        print(_line(r))
    return 0


def cmd_respond(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    now = _iso(_now())
    path = _task_path(args.team, args.name)
    doc = transport.read(path)
    if doc is None:
        # Fail-loud (same doctrine as `review status` rc-1): the name resolves to
        # NO directive doc — either a display TITLE was used in place of the
        # hash-suffixed slug, or the read failed. Recording a response here would
        # GHOST-CLOSE: the shard lands under a slug nobody owns while the real
        # directive stays open in the owner's needs-me forever (cost three
        # ghost-closes in one day). Write nothing; make the caller retry with the
        # exact slug.
        print(f"respond: no directive '{args.name}' in team/{args.team} "
              f"(absent or unreadable) — nothing recorded. Use the exact slug from "
              f"`inbox`/`briefing --json` (the hash-suffixed name, not the display "
              f"title).", file=sys.stderr)
        return 1
    stamp = _stamp_for_path(now, agent)
    fm = {"type": "Response", "agent": agent, "outcome": args.outcome, "timestamp": now}
    transport.write(_response_path(args.team, args.name, stamp),
                    okf.render_frontmatter(fm) + f"\n{args.evidence or args.outcome}\n")
    try:
        out = tasks.apply_update(doc, now=now, status="done",
                                 evidence=f"{args.outcome} (respond by {agent})")
        transport.write(path, out)
        print(f"responded {args.name}: {args.outcome} (closed)")
    except tasks.TaskError as e:
        print(f"responded {args.name}: {args.outcome} (response recorded; not closed: {e})")
    # The reply leg: this shard is what the directive owner's inbox surfaces.
    print("response recorded — the owner's inbox surfaces it")
    return 0


# --- continuity: role checkpoints, park, briefing ---

def _set_role_field(transport: Any, team: str, role: str, key: str, value: str) -> bool:
    """Read-modify-write one frontmatter field on a role doc, preserving the rest."""
    path = _role_doc_path(team, role)
    doc = transport.read(path)
    fm = okf.parse_frontmatter(doc)
    if fm is None:
        return False
    split = okf.split_frontmatter(doc or "")
    body = split[1] if split else ""
    fm[key] = value
    return transport.write(path, okf.render_frontmatter(fm) + "\n" + body.lstrip("\n"))


def cmd_continuity_checkpoint(args: argparse.Namespace, transport: Any) -> int:
    if args.ref:
        if not _set_role_field(transport, args.team, args.role, "checkpoint_ref", args.ref):
            print(f"checkpoint failed: role {args.role} not found/parseable", file=sys.stderr)
            return 1
        print(f"checkpoint_ref for role {args.role} -> {args.ref}")
        return 0
    reg = okf.parse_frontmatter(transport.read(_role_doc_path(args.team, args.role))) or {}
    ref = reg.get("checkpoint_ref")
    if not ref:
        print(f"role {args.role}: no checkpoint_ref set")
        return 0
    print(f"role {args.role}: checkpoint_ref = {ref}")
    if "/continuity/" in str(ref):
        raw = transport.read(str(ref))
        try:
            snap = json.loads(raw) if raw else None
        except Exception:
            snap = None
        if snap:
            print(continuity.render_resume(snap))
    return 0


def _held_roles(transport: Any, team: str, agent: str) -> tuple[list[str], bool]:
    """Roles where ``agent`` holds a FRESH lease. Returns ``(held, ok)``.

    ``ok`` is False whenever the answer is UNKNOWN — the roles/ listing raised, or
    any single role's state could not be resolved. FAIL CLOSED: an empty ``held``
    with ``ok=True`` means "holds nothing"; with ``ok=False`` it means "we could not
    find out", and those are different facts that callers must not conflate.

    This is the write path's fold (``continuity park``), the role surface behind
    session-exit checkpointing, and it must draw exactly the same UNKNOWN-vs-vacant
    distinctions the read folds do. Each hole they close, it must close too: a
    raised listing must not return a partial list as if complete; ``or {}`` on the
    role doc must not turn an unparseable body into the default SLA; a bare-except
    ``float(...) or DEFAULT`` must not map an explicitly-invalid ``sla_hours`` onto
    24h; and ``or {}`` on the lease read must not fold an unreadable shard out as
    "not a holder".

    On a write path those failures are worse than on a read one: if ``park`` treated
    UNKNOWN as "nothing to park" and exited 0, a transport blip at session exit would
    silently discard the checkpoint and report a clean no-op — at exactly the moment
    nobody is watching, because the session is ending.

    So it delegates per-role state to ``_role_fresh_holders``, the canonical fold
    that already draws every one of those distinctions, so park and ``roles status``
    can never disagree about a lease.
    """
    now = _iso(_now())
    names = _roles_listing_names(transport, team)
    if names is None:
        return [], False  # membership UNKNOWN — only a complete listing is evidence
    held: list[str] = []
    ok_all = True
    cache: dict[str, Any] = {}
    for n in sorted(names):
        if not n.endswith(".md") or n == "index.md":
            continue
        role = n[:-3]
        holders, ok = _role_fresh_holders(
            transport, team, role, now=now, listing_cache=cache)
        if not ok:
            ok_all = False  # this role's state is unknown; do not read it as "not held"
            continue
        if agent in holders:
            held.append(role)
    return held, ok_all


def cmd_continuity_park(args: argparse.Namespace, transport: Any) -> int:
    """Session-exit checkpoint: snapshot every role the agent holds and point
    each role's checkpoint_ref at it."""
    agent = args.agent or _host()
    now = _iso(_now())
    held, ok = _held_roles(transport, args.team, agent)
    if not ok:
        # UNKNOWN is not "nothing to park". Refusing here is the whole point: a
        # session runs park as it exits, so a silent no-op discards the checkpoint
        # the NEXT session resumes from, and nobody is watching to notice. Say the
        # checkpoint was not written, loudly and non-zero, while the operator can
        # still retry with the context still alive.
        print(f"park: could not determine which roles {agent} holds in "
              f"team/{args.team} (role state unreadable, not empty) — "
              f"CHECKPOINT NOT WRITTEN. Nothing was parked; retry before ending "
              f"the session.", file=sys.stderr)
        return 1
    if not held:
        print(f"park: {agent} holds no fresh roles in team/{args.team} — nothing to park")
        return 0
    for role in held:
        task_slug = f"role-{tasks.slugify(role)}"
        snap = continuity.build_snapshot(
            agent=agent, task=task_slug,
            objective=args.objective or f"parked role {role} at session exit",
            now=now, next_actions=args.next or [],
            open_questions=args.open_question or [],
        )
        path = _continuity_path(args.team, agent, task_slug)
        if not transport.write(path, json.dumps(snap, indent=2)):
            print(f"park: snapshot write FAILED for {role}; checkpoint_ref left unchanged",
                  file=sys.stderr)
            continue
        if not _set_role_field(transport, args.team, role, "checkpoint_ref", path):
            print(f"park: checkpoint_ref update FAILED for {role}", file=sys.stderr)
            continue
        print(f"parked {role} -> {path}")
    return 0


def cmd_briefing(args: argparse.Namespace, transport: Any) -> int:
    """One-call session-start bundle. Every section tolerates absent add-ons."""
    agent = args.agent or _host()
    now = _iso(_now())
    out: dict[str, Any] = {"schema": "coord.teams.briefing.v1", "team": args.team,
                           "agent": agent, "at": now}
    # Public-read failure contract (see _read_degraded_row): the CORE task fold is
    # not an add-on — an UNKNOWN summaries index must surface as the shared marker,
    # never a silently-empty board/inbox/needs-me that reads as "all clear". The
    # bundle stays tolerant (rc 0); the marker + stderr notice make it loud.
    rows, rows_ok, rows_reason = _load_rows_status(transport, args.team)
    if not rows_ok:
        out["read_degraded"] = _read_degraded_row(rows_reason)
    # One shared add-on deadline (see _briefing_budget), opened here — before the
    # first unbudgeted transport-heavy section (presence) — and spent cumulatively
    # across presence + pending-reviews + resume, so the whole add-on stack is
    # bounded, not just one section. Opening the deadline before the presence read
    # matters: presence shard reads are otherwise unbudgeted, so a degraded
    # transport would hang `briefing` in `presence.roster(_presence_shards(...))`
    # before any bound applied. (`_load_rows` above carries its own
    # COORD_OVERLAY_BUDGET; pending-reviews keeps its own independent
    # COORD_REVIEW_FOLD_BUDGET.)
    add_on = Deadline.open(_briefing_budget())
    try:
        shards, pres_degraded = _presence_shards_bounded(
            transport, args.team, deadline=add_on.instant)
        out["presence"] = presence.roster(shards, now=now)
        if pres_degraded is not None:
            # Append the degraded marker to the section list so partial presence
            # knowledge stays visible (json + text).
            out["presence"].append(pres_degraded)
    except Exception as e:
        print(f"briefing: presence section unavailable ({type(e).__name__})", file=sys.stderr)
        out["presence"] = []
    try:
        out["board"] = query.board(rows)
    except Exception as e:
        print(f"briefing: board section unavailable ({type(e).__name__})", file=sys.stderr)
        out["board"] = {}
    # One role resolution for the whole bundle, shared by the inbox and needs-me
    # sections (the two folds that make up an agent's work queue). Both consume the
    # same held set, so they can never disagree about a lease, and the lease read
    # is paid once per briefing rather than once per section. Unresolved roles are
    # unknown — surfaced below as `role_degraded`, never folded to "no roles".
    try:
        held_roles, unresolved_roles = _held_roles_for_rows(
            transport, args.team, agent, rows, now=now)
    except Exception as e:
        # The resolver never raises by contract; if it somehow does, the role set is
        # UNKNOWN for EVERY role-shaped assignee in the bundle — say so, don't
        # quietly serve a role-blind queue.
        print(f"briefing: role resolution unavailable ({type(e).__name__})", file=sys.stderr)
        held_roles, unresolved_roles = set(), {"(all)"}
    if unresolved_roles:
        out["role_degraded"] = _role_degraded_row(unresolved_roles)
    try:
        out["inbox"] = _directed_inbox(transport, args.team, agent, rows,
                                       held_roles=held_roles or None)
    except Exception as e:
        print(f"briefing: inbox section unavailable ({type(e).__name__})", file=sys.stderr)
        out["inbox"] = []
    try:
        out["needs_me"] = query.needs_me(rows, agent, now=now, held_roles=held_roles)
    except Exception as e:
        print(f"briefing: needs_me section unavailable ({type(e).__name__})", file=sys.stderr)
        out["needs_me"] = []
    # The shared add-on deadline (add_on) was opened at the top of this
    # function, before the presence section — time already burned by presence
    # shrinks the window the pending-reviews and resume reads get, so the whole
    # add-on stack is bounded cumulatively. pending-reviews keeps its own tighter
    # budget (whichever bound is sooner).
    try:
        out["pending_reviews"] = _pending_reviews_for(
            transport, args.team, agent, deadline=add_on.instant)
    except Exception as e:
        print(f"briefing: pending_reviews section unavailable ({type(e).__name__})", file=sys.stderr)
        out["pending_reviews"] = []
    try:
        snaps = []
        resume_cut = False
        for e in transport.list_dir(_continuity_prefix(args.team, agent)):
            if add_on.expired():
                # Shared budget spent by the earlier add-on sections: stop reading
                # this agent's snapshots (a per-file read fan-out) rather than let a
                # slow tail hang the briefing. The resume is a floor, not the truth.
                resume_cut = True
                break
            n = (e.get("name") or "").rstrip("/")
            if e.get("is_dir") and n:
                raw = transport.read(_continuity_path(args.team, agent, n))
                if raw:
                    try:
                        snaps.append(json.loads(raw))
                    except Exception:
                        pass
        out["resume"] = continuity.latest(snaps)
        if resume_cut:
            print("briefing: resume section truncated (shared budget spent) — "
                  "resume may be stale; run `continuity resume` for the latest",
                  file=sys.stderr)
    except Exception as e:
        print(f"briefing: resume section unavailable ({type(e).__name__})", file=sys.stderr)
        out["resume"] = None
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"briefing — {agent} in team/{args.team}")
    if not rows_ok:
        _surface_read_degraded(rows_reason, json_mode=False)
    live = [p["agent"] for p in out["presence"] if p.get("liveness") == "live"]
    print(f"  live now: {', '.join(live) if live else '(nobody)'}")
    for r in out["presence"]:  # always shown — a degraded roster must never hide
        if r.get("type") == "presence-degraded":
            print(_presence_degraded_line(r))
    open_counts = {k: len(v) for k, v in (out["board"] or {}).items() if v}
    print("  board: " + (", ".join(f"{k}={v}" for k, v in open_counts.items()) or "empty"))
    print(f"  inbox: {len(out['inbox'])} item(s)")
    for r in out["inbox"][:5]:
        print(_line(r))
    print(f"  needs-me: {len(out['needs_me'])} item(s)")
    if out.get("role_degraded"):
        # Always shown, and printed against BOTH counts above — the two sections it
        # qualifies. Without it, an unresolved role renders as a clean queue that
        # reads "no role work", which is the bug this whole change closes.
        print(_role_degraded_line(out["role_degraded"]))
    pend_rows = [r for r in out["pending_reviews"]
                 if r.get("type") != "review-fold-degraded"]
    degraded_rows = [r for r in out["pending_reviews"]
                     if r.get("type") == "review-fold-degraded"]
    print(f"  pending reviews: {len(pend_rows)} item(s)")
    for r in pend_rows[:5]:
        print(_line(r))
    for r in degraded_rows:  # always shown — a degraded fold must never hide
        print(_review_degraded_line(r))
    print(continuity.render_resume(out["resume"]))
    return 0


# --- presence (fulcra-agent-presence) ---

def _presence_prefix(team: str) -> str:
    return f"team/{team}/presence/"


def _presence_shards(transport: Any, team: str) -> list[dict[str, Any]]:
    shards: list[dict[str, Any]] = []
    try:
        for e in transport.list_dir(_presence_prefix(team)):
            n = e.get("name") or ""
            if e.get("is_dir") or not n.endswith(".md"):
                continue
            fm = okf.parse_frontmatter(transport.read(_presence_prefix(team) + n)) or {}
            fm.setdefault("agent", n[:-3])
            shards.append(fm)
    except TransportError:
        pass
    return shards


def _presence_shards_bounded(
    transport: Any, team: str, *, deadline: Optional[float] = None
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    """Read presence shards into the roster-fold shape, bounded by an absolute
    ``time.monotonic()`` deadline (None = unbounded/legacy). Returns
    ``(shards, degraded_marker_or_None)``.

    The presence section is a team-global fan-out — one shard per agent, a
    ``list_dir`` plus one read each — a fan-out, so it must be bounded or a single
    degraded transport hangs the whole ``briefing`` with no way out but a signal.
    This mirrors the review fold discipline: the deadline is checked both before and after
    each blocking read (a single stalled read can't return a clean row — overshoot
    is bounded by one read), a listed-but-unreadable shard (read -> None) counts as
    ``skipped``, and a top-level listing failure yields ``scanned=0``. The listing
    itself is a blocking op under the same discipline: a deadline
    already spent when we get here skips the call entirely (an earlier section spent
    the budget — paying one more transport timeout of stall would re-open the hang),
    and an overrun detected after the listing surfaces the marker even when the
    listing returned [] (otherwise a slow empty listing fell through the per-shard
    loop to ``([], None)`` — a falsely-clean empty roster). On any breach/failure a
    single ``presence-degraded`` row ``{type, scanned, total[, skipped]}`` (the
    shared degraded-marker shape) is returned alongside the partial roster — the
    section never hangs, never crashes, never silently truncates. Digests keep the
    unbounded ``_presence_shards``: they are not on the briefing hang path."""
    dl = Deadline(deadline)
    if dl.expired():
        # Budget already spent before the section started: skip the listing — don't
        # pay one more blocking op. total=0: the roster size is UNKNOWN (never listed).
        return [], budget_mod.degraded_row("presence-degraded", 0, 0)
    pfx = _presence_prefix(team)
    try:
        entries = transport.list_dir(pfx)
    except TransportError:
        # The listing itself failed: the roster is UNKNOWN, not empty. Surface a
        # degraded marker (scanned=0) so absence-vs-outage isn't folded to silence.
        return [], budget_mod.degraded_row("presence-degraded", 0, 0)
    files = [e for e in entries
             if not e.get("is_dir") and (e.get("name") or "").endswith(".md")]
    total = len(files)
    if dl.expired():
        # The deadline passed DURING the listing: detect the overrun immediately
        # after the blocking op — even for total==0, where the per-shard loop below
        # never runs and could not surface it. No shard is read (the budget is
        # spent); the listing we already paid for still prices ``total`` honestly.
        return [], budget_mod.degraded_row("presence-degraded", 0, total)
    shards: list[dict[str, Any]] = []
    scanned = 0
    skipped = 0
    degraded = False
    for e in files:
        if dl.expired():
            degraded = True
            break
        scanned += 1
        n = e.get("name") or ""
        raw = transport.read(pfx + n)
        if dl.expired():
            # The deadline passed DURING this read: detect the overrun immediately
            # after the blocking op. Keep the shard we already paid for, then stop.
            degraded = True
            if raw is not None:
                fm = okf.parse_frontmatter(raw) or {}
                fm.setdefault("agent", n[:-3])
                shards.append(fm)
            else:
                skipped += 1
            break
        if raw is None:
            # Listed yet unreadable -> UNKNOWN shard (a transport problem, never a
            # silent vanish): count it skipped and keep scanning the rest.
            skipped += 1
            degraded = True
            continue
        fm = okf.parse_frontmatter(raw) or {}
        fm.setdefault("agent", n[:-3])
        shards.append(fm)
    marker: Optional[dict[str, Any]] = None
    if degraded:
        marker = budget_mod.degraded_row("presence-degraded", scanned, total, skipped)
    return shards, marker


def _presence_degraded_line(r: dict[str, Any]) -> str:
    return budget_mod.fold_degraded_line(
        r, label="presence",
        remedy="roster may be partial, run `presence show` for the rest",
        noun="shard")


def cmd_presence_beat(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    fm = {
        "type": "Presence", "title": f"presence — {agent}", "agent": agent,
        "workstreams": args.workstream or [], "summary": args.summary or "",
        "timestamp": _iso(_now()),
    }
    body = f"\n# Presence: {agent}\n"
    slug = tasks.agent_key(agent)
    transport.write(f"{_presence_prefix(args.team)}{slug}.md", okf.render_frontmatter(fm) + body)
    print(f"beat {agent} ({slug}.md)")
    return 0


def cmd_presence_show(args: argparse.Namespace, transport: Any) -> int:
    ros = presence.roster(_presence_shards(transport, args.team), now=_iso(_now()))
    if args.json:
        print(json.dumps(ros, indent=2))
        return 0
    print(f"presence — team/{args.team}: {len(ros)} agent(s)")
    for r in ros:
        ws = ", ".join(r["workstreams"])
        print(f"  [{r['liveness']:5}] {r['agent']}" + (f"  ({ws})" if ws else "")
              + (f" — {r['summary']}" if r["summary"] else ""))
    return 0


def cmd_agents(args: argparse.Namespace, transport: Any) -> int:
    # Public-read failure contract (see _read_degraded_row): an UNKNOWN task fold
    # must not read as every agent having "no open work".
    rows, ok, reason = _load_rows_status(transport, args.team)
    digest = presence.agents_digest(rows, _presence_shards(transport, args.team), now=_iso(_now()))
    if args.json:
        out = digest + [_read_degraded_row(reason)] if not ok else digest
        print(json.dumps(out, indent=2))
        return 0
    if not ok:
        _surface_read_degraded(reason, json_mode=False)
    for a in digest:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(a["open"].items())) or "no open work"
        print(f"  [{a['liveness']:7}] {a['agent']} — {counts}"
              + (f" — {a['summary']}" if a["summary"] else ""))
    return 0


def cmd_roles_claim(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    slug = tasks.agent_key(agent)
    if okf.parse_frontmatter(transport.read(_role_doc_path(args.team, args.role))) is None:
        print(f"note: role {args.role!r} has no registered role doc — status folds fall back "
              f"to defaults and review role-routing will NOT match this role's holders; "
              f"create team/{args.team}/roles/{args.role}.md", file=sys.stderr)
    shard_path = f"{_leases_prefix(args.team, args.role)}{slug}.md"
    state = _nonce_state_path(args.team, args.role, slug)
    # Same-id double-acting check: leases can't distinguish two sessions sharing one
    # id (same shard file), so compare the shard's nonce to the one THIS session wrote.
    existing = okf.parse_frontmatter(transport.read(shard_path)) or {}
    try:
        stored = state.read_text().strip() if state.exists() else None
    except OSError:
        stored = None
    shard_nonce = existing.get("nonce")  # absent for pre-nonce shards: overwrites by
    # old-engine sessions are undetectable by design — nothing to compare against.
    if stored and shard_nonce and shard_nonce != stored:
        print(f"WARNING: nonce mismatch on {slug}.md — another session has been acting "
              f"as {agent} since your last claim (same-id double-acting). Give each "
              f"session its own FULCRA_COORD_AGENT identity, or stop one.", file=sys.stderr)
    elif stored is None and shard_nonce:
        print(f"note: taking over an existing lease shard for {agent} written by another "
              f"session (no local nonce state to compare)", file=sys.stderr)
    nonce = secrets.token_hex(8)
    fm = {"type": "Lease", "title": f"{args.role} lease — {agent}", "agent": agent,
          "timestamp": _iso(_now()), "nonce": nonce}
    transport.write(shard_path, okf.render_frontmatter(fm) + f"\nHolding {args.role}.\n")
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(nonce + "\n")
    except OSError as e:
        print(f"note: could not persist nonce state (double-acting check disabled "
              f"until it can be written): {e}", file=sys.stderr)
    print(f"claimed {args.role} as {agent} ({slug}.md; refresh by re-running)")
    return 0


def cmd_roles_release(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    slug = tasks.agent_key(agent)
    path = f"{_leases_prefix(args.team, args.role)}{slug}.md"
    state = _nonce_state_path(args.team, args.role, slug)
    if transport.read(path) is None:
        try:
            state.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"no lease for {agent} on {args.role}", file=sys.stderr)
        return 1
    ok = transport.delete(path) if hasattr(transport, "delete") else False
    if ok:
        try:
            state.unlink(missing_ok=True)
        except OSError:
            pass
    print(f"released {args.role} ({agent})" if ok else f"release failed for {path}",
          file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


# --- health / doctor (fulcra-agent-health) ---

def cmd_health(args: argparse.Namespace, transport: Any) -> int:
    shards = []
    try:
        for e in transport.list_dir(health_mod.health_prefix(args.team)):
            n = e.get("name") or ""
            if not e.get("is_dir") and n.endswith(".json"):
                sh = health_mod.parse_shard(transport.read(health_mod.health_prefix(args.team) + n))
                if sh:
                    shards.append(sh)
    except TransportError:
        pass
    view = health_mod.fold(shards, now=_iso(_now()))
    code = 0 if view["healthy"] else 1
    # Tier-1 continuity audit: an agent beating presence but with no fresh
    # snapshot is working without a recoverable trail. Compute it here so both
    # the JSON payload and the text output surface it; it does not move health's
    # exit code — that stays reconciler-driven.
    now_dt = _now()
    pres_rows: list[dict[str, Any]] = []
    snap_rows: list[dict[str, Any]] = []
    for r in presence.roster(_presence_shards(transport, args.team), now=_iso(now_dt)):
        pts = roles._parse(r.get("last_seen"))
        if pts is None:
            continue
        pres_rows.append({"agent": r["agent"], "ts": pts})
        for snap in _agent_snapshots(transport, args.team, r["agent"]):
            sts = continuity._parse_created_at(snap.get("created_at"))
            if sts is not None:
                snap_rows.append({"agent": r["agent"], "ts": sts})
    flagged_agents = continuity_audit.stale_agents(pres_rows, snap_rows, now=now_dt)
    # Same row fields stale_agents returns: agent/presence_age_h/snapshot_age_h.
    view["continuity_stale"] = flagged_agents
    if args.json:
        print(json.dumps(view, indent=2))
        return code
    print(f"health — team/{args.team}: {view['fresh']}/{view['total']} host(s) fresh"
          + ("" if view["healthy"] else "  [NO FRESH RECONCILER]"))
    if view["total"] == 0:
        print("  (no health shards at all — nobody has ever reconciled this team)")
    for h in view["hosts"]:
        age = "?" if h["age_hours"] is None else f"{h['age_hours']:g}h"
        flag = "STALE" if h["stale"] else "ok"
        print(f"  [{flag:5}] {h['host']} — last reconcile {age} ago"
              f" (v{h.get('engine_version')}, {h.get('tasks')} tasks, {h.get('warnings')} warn)")
    # Tier-1 continuity audit (computed above): an agent beating presence but
    # with no fresh snapshot is working without a recoverable trail.
    for flagged in flagged_agents:
        y = flagged["snapshot_age_h"]
        snap_desc = "missing" if y is None else f"stale ({y}h)"
        print(f"  continuity-stale: {flagged['agent']}"
              f" presence-fresh ({flagged['presence_age_h']}h)"
              f" but snapshot {snap_desc} — see fulcra-agent-continuity contract")
    # empty fleet reads UNHEALTHY: "nobody ever reconciled" is the primary
    # cold-start failure a monitor probe exists to catch (review finding).
    return code


def cmd_doctor(args: argparse.Namespace, transport: Any) -> int:
    """Local preflight: tooling on PATH + store reachable. Exit 0 = healthy."""
    import shutil
    ok = True
    from .transport import _split_command
    full_cmd = " ".join(_split_command())
    launcher = _split_command()[0]
    if shutil.which(launcher):
        print(f"  ✓ storage command launcher on PATH ({launcher}; full: {full_cmd!r})")
    else:
        print(f"  ✗ storage command launcher NOT found ({launcher}; full: {full_cmd!r}) — "
              f"install fulcra-api + auth login", file=sys.stderr)
        ok = False
    try:
        transport.list_dir(f"team/{args.team}/" if args.team else "team/")
        print("  ✓ File Store reachable")
    except Exception as e:
        print(f"  ✗ File Store unreachable: {type(e).__name__}: {e}", file=sys.stderr)
        ok = False
    from . import __version__ as _v
    print(f"  ✓ coord-engine v{_v}")
    print("doctor: healthy" if ok else "doctor: PROBLEMS FOUND")
    return 0 if ok else 1


# --- digest + escalate (fulcra-agent-health) ---

def _digest_record_id(team: str, day: str, window: str) -> str:
    """Deterministic record id for the (team, day, window) digest moment.

    The typed ingest endpoint upserts on an explicit id, so every host that emits
    this window's digest converges on one timeline record — idempotency lives at
    the ingestion layer, not in any read-then-write marker race."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"fulcra-coord-digest:{team}:{day}:{window}"))


def _emit_digest_timeline(*, name: str, note: str, window: str, agent: str,
                          record_id: str) -> bool:
    """Hand one rendered digest to the timeline annotation writer.

    Best-effort: coord-engine is stdlib-only, so the writer package (and the API
    client / token it needs) may be entirely absent — that degrades to False,
    never an exception. Lands on the 'Agent Tasks — Digest' track via the writer's
    own definition resolution."""
    try:
        from fulcra_common import annotations as _ann
    except Exception:
        return False
    try:
        # gated=False: this seam's opt-in is the heartbeat's explicit
        # --emit-timeline flag, not the machine-local writer mode. The
        # deterministic record_id makes concurrent same-window emits upsert
        # into one record.
        return bool(_ann.emit_digest_annotation(
            name=name, note=note, window=window, agent=agent, gated=False,
            id=record_id))
    except Exception:
        return False


def cmd_digest(args: argparse.Namespace, transport: Any) -> int:
    now = _iso(_now())
    # Public-read failure contract (see _read_degraded_row): don't fold an UNKNOWN
    # index into a falsely-quiet health digest.
    rows, ok, reason = _load_rows_status(transport, args.team)
    d = digest_mod.build(rows, _presence_shards(transport, args.team),
                         now=now, human=args.human or _human())
    if args.json:
        if not ok:
            d = {**d, _READ_DEGRADED: _read_degraded_row(reason)}
        print(json.dumps(d, indent=2))
    else:
        if not ok:
            _surface_read_degraded(reason, json_mode=False)
        print(digest_mod.render(d), end="")
    emit_timeline = getattr(args, "emit_timeline", False)
    if args.store or emit_timeline:
        day = now[:10]
        window = digest_mod.window_for(now)
        marker = f"team/{args.team}/_coord/digests/{day}-{window}.md"
        # The store marker dedups the stored copy (a lost race just re-writes an
        # equivalent copy as a new version — harmless). It is not the timeline
        # correctness guard: that lives in the deterministic record id below.
        stored_body = transport.read(marker)
        if stored_body is not None:
            print(f"(digest for {day} {window} already stored — skipped)", file=sys.stderr)
        else:
            stored_body = digest_mod.render(d)
            transport.write(marker, stored_body)
            print(f"stored digest -> _coord/digests/{day}-{window}.md", file=sys.stderr)
        if emit_timeline:
            # Timeline emit state is separate from the store marker and written
            # only after a confirmed emit, so a transient failure (missing
            # writer, token flake, HTTP error) retries on the next heartbeat
            # tick instead of consuming the window. The deterministic record id
            # makes any concurrent or ambiguously-acked re-emit an ingestion-layer
            # upsert of the same record, so retries and races can never duplicate
            # the digest.
            emitted_marker = f"team/{args.team}/_coord/digests/{day}-{window}.emitted"
            if transport.read(emitted_marker) is not None:
                pass  # this window's digest is confirmed on the timeline
            else:
                rid = _digest_record_id(args.team, day, window)
                if _emit_digest_timeline(
                        name=f"Agent digest — {day} {window}",
                        note=stored_body, window=window, agent=_host(),
                        record_id=rid):
                    transport.write(emitted_marker,
                                    f"emitted {now} by {_host()} record {rid}\n")
                    print(f"emitted digest timeline moment ({day} {window})",
                          file=sys.stderr)
                else:
                    # Loud but rc 0: the stored copy exists; the next heartbeat
                    # tick retries this window's emit (no marker written).
                    print("digest timeline emit failed (timeline writer "
                          "missing or degraded) — stored copy kept; will retry "
                          "on the next heartbeat tick", file=sys.stderr)
    return 0


def cmd_escalate(args: argparse.Namespace, transport: Any) -> int:
    """Role-vacancy sweep: for every role doc, if vacancy past SLA and no marker
    today, write the marker + a P1 directive to the role's maintainer.
    Heartbeat-safe (idempotent per day)."""
    now = _iso(_now()); today = _now().strftime("%Y-%m-%d")
    escalated = checked = 0
    try:
        entries = transport.list_dir(f"team/{args.team}/roles/")
    except TransportError:
        print("escalate: roles dir unreadable", file=sys.stderr)
        return 1
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md") or n == "index.md":
            continue
        role = n[:-3]; checked += 1
        doc = transport.read(_role_doc_path(args.team, role))
        reg = okf.parse_frontmatter(doc)
        if reg is None:
            # Fail closed: this doc was JUST LISTED by the parent roles/ scan, so
            # no usable doc is knowably transient-or-deleted-or-corrupt — never a
            # live role to judge under DEFAULT_SLA_HOURS. Falling through with the
            # 24h default would collapse a longer-SLA role's window and fire a false
            # VACANT escalation (the failure this guards, on the acting path). Skip:
            # transient -> retried next sweep (correct); deleted -> role gone (also
            # correct); corrupt -> a human must fix the doc, and a P1 minted off a
            # doc we cannot read is noise at best. This guard must test usability,
            # not just `doc is None`, or an unparseable body sails past it into
            # exactly that false escalation; `_role_fresh_holders` and `roles status`
            # enforce the same rule, so all three surfaces agree: no usable doc for
            # a LISTED role is UNKNOWN.
            print(f"escalate: role doc unusable for {role} — state unknown, "
                  f"skipped (unreadable or corrupt, retry)", file=sys.stderr)
            continue
        sla = roles.parse_sla_hours(reg.get("sla_hours"))
        if sla is None:
            # An EXPLICITLY invalid `sla_hours` on the ACTING path. Judging the role
            # under the 24h default would collapse an unknown (possibly much longer)
            # window and fire a false VACANT — the same failure this function's
            # doc-guard above already names, reached through the value instead of
            # the document. A P1 to a human minted off an SLA we invented is worse
            # than noise; a malformed field is a doc fix, not an escalation. Skip:
            # the sweep retries every heartbeat, so a repaired doc escalates on the
            # next pass if it genuinely is vacant.
            print(f"escalate: unusable sla_hours ({reg.get('sla_hours')!r}) for "
                  f"{role} — state unknown, skipped (fix the role doc)",
                  file=sys.stderr)
            continue
        # Dormancy: a deliberately-parked role (future dormant_until) is exempt from
        # the mechanical vacancy sweep regardless of lease state — the parked role
        # is vacant BY DESIGN, so re-firing a P1 every heartbeat host, daily, is the
        # bug. Garbage dormant_until fails OPEN (treated absent + a visible note) so
        # a typo can never silently suppress escalations.
        dormant, dormant_err = roles.dormant_state(reg.get("dormant_until"), now=now)
        if dormant_err:
            print(f"escalate: unparseable dormant_until for {role} — treated as "
                  f"absent, escalation NOT suppressed (fix the date to park it)",
                  file=sys.stderr)
        if dormant:
            print(f"escalate: {role} dormant until {reg.get('dormant_until')} — "
                  f"vacancy escalation suppressed", file=sys.stderr)
            continue
        leases: Optional[list[dict[str, Any]]] = []
        try:
            for f in transport.list_dir(_leases_prefix(args.team, role)):
                fn = f.get("name") or ""
                if not f.get("is_dir") and fn.endswith(".md"):
                    fm = okf.parse_frontmatter(
                        transport.read(_leases_prefix(args.team, role) + fn))
                    if fm is None:
                        # A JUST-LISTED lease shard read None/unparseable: `or {}`
                        # here would drop the timestamp and silently fold the holder
                        # out as stale — a fail-open VACANCY on the ACTING path
                        # (the same class the read folds guard against). UNKNOWN:
                        # never escalate.
                        print(f"escalate: lease shard unreadable for {role} — "
                              f"state unknown, skipped", file=sys.stderr)
                        leases = None
                        break
                    leases.append({"agent": fm.get("agent") or fn[:-3],
                                   "timestamp": fm.get("timestamp")})
        except TransportError:
            leases = None
        marker_path = _escalation_marker_path(args.team, role, today)
        marker_exists = transport.read(marker_path) is not None
        if not roles.escalation_due(leases, now=now, sla_hours=sla,
                                    marker_exists_today=marker_exists):
            continue
        maintainer = str(reg.get("maintainer") or _human())
        transport.write(marker_path, okf.render_frontmatter(
            {"type": "Escalation", "role": role, "timestamp": now}) + "\nescalated\n")
        slug, content = tasks.new_task_doc(
            f"ROLE VACANT {today}: {role} unattended past {sla:g}h SLA",
            now=now, status="proposed", priority="P1", owner=_host(),
            assignee=maintainer, kind="directive",
            summary=f"Role {role} in team/{args.team} has no fresh lease past its SLA. "
                    f"Claim it (coord-engine roles claim {args.team} {role}) or reassign.",
        )
        dst = _task_path(args.team, slug)
        if transport.read(dst) is None:
            transport.write(dst, content)
            escalated += 1
            print(f"escalated {role} -> {maintainer}")
        else:
            print(f"re-escalation suppressed for {role} (today's directive already exists)")
    print(f"escalate: {checked} role(s) checked, {escalated} escalated")
    return 0


# --- operator loop (fulcra-agent-operator): asks + answer ---

def cmd_asks(args: argparse.Namespace, transport: Any) -> int:
    # Public-read failure contract (see _read_degraded_row): an UNKNOWN index must
    # not read as "nothing waiting on the human".
    rows, ok, reason = _load_rows_status(transport, args.team)
    got = query.asks(rows, now=_iso(_now()), human=args.human or _human())
    if args.json:
        out = [_read_degraded_row(reason)] + got if not ok else got
        print(json.dumps(out, indent=2))
        return 0
    if not ok:
        _surface_read_degraded(reason, json_mode=False)
    print(f"asks — {len(got)} waiting on {args.human or _human()} (oldest first)")
    for r in got:
        age = "?" if r.get("age_hours") is None else f"{r['age_hours']:g}h"
        print(f"  [{age:>6}] [{r.get('priority')}] {r.get('title')}")
        ask = str(r.get('blocked_on') or r.get('next_action') or '').strip()
        if ask:
            print(f"           ask: {ask[:140]}")
        print(f"           slug: {r.get('name')}  owner: {r.get('owner')}")
    return 0


def cmd_answer(args: argparse.Namespace, transport: Any) -> int:
    path = _task_path(args.team, args.name)
    try:
        doc, owner = tasks.apply_answer(transport.read(path), now=_iso(_now()),
                                        answer=args.with_text, relayer=_host(),
                                        human=args.human or _human())
    except tasks.TaskError as e:
        print(f"answer failed: {e}", file=sys.stderr)
        return 1
    if not transport.write(path, doc):
        print("answer failed: write did not land", file=sys.stderr)
        return 1
    print(f"answered {args.name} -> handed back to {owner} (unblocked; will surface in their inbox)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coord-engine", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def add_json(sp):
        sp.add_argument("--json", action="store_true", help="emit JSON")

    r = sub.add_parser("reconcile", help="scan + heal a team's task views")
    r.add_argument("team")
    r.add_argument("--retention-days", dest="retention_days",
                   help="archive quiet terminal/proposed tasks and settled-single orphan reviews older than N days (or env COORD_RETENTION_DAYS)")
    r.set_defaults(func=cmd_reconcile)

    s = sub.add_parser("status", help="counts by status")
    s.add_argument("team"); add_json(s); s.set_defaults(func=cmd_status)

    b = sub.add_parser("board", help="open work grouped by status")
    b.add_argument("team"); add_json(b); b.set_defaults(func=cmd_board)

    nm = sub.add_parser("needs-me", help="open work assigned to / blocking an agent")
    nm.add_argument("team"); nm.add_argument("--agent", required=True); add_json(nm)
    nm.set_defaults(func=cmd_needs_me)

    sc = sub.add_parser("search", help="substring search over tasks")
    sc.add_argument("team"); sc.add_argument("query"); add_json(sc)
    sc.add_argument("--archived", action="store_true", help="also search the cold archive")
    sc.set_defaults(func=cmd_search)

    rl = sub.add_parser("roles", help="role status fold (fulcra-agent-roles)")
    rlsub = rl.add_subparsers(dest="roles_command", required=True)
    rst = rlsub.add_parser("status", help="HELD/VACANT/CONTESTED + escalation-due")
    rst.add_argument("team"); rst.add_argument("role"); add_json(rst)
    rst.set_defaults(func=cmd_roles_status)
    rcl = rlsub.add_parser("claim", help="claim/refresh a lease on a role")
    rcl.add_argument("team"); rcl.add_argument("role"); rcl.add_argument("--agent", "-a")
    rcl.set_defaults(func=cmd_roles_claim)
    rre = rlsub.add_parser("release", help="release your lease on a role")
    rre.add_argument("team"); rre.add_argument("role"); rre.add_argument("--agent", "-a")
    rre.set_defaults(func=cmd_roles_release)

    pr = sub.add_parser("presence", help="presence beats + roster (fulcra-agent-presence)")
    prsub = pr.add_subparsers(dest="presence_command", required=True)
    prb = prsub.add_parser("beat", help="write/refresh your presence shard")
    prb.add_argument("team"); prb.add_argument("--agent", "-a")
    prb.add_argument("--workstream", "-w", action="append")
    prb.add_argument("--summary", "-s")
    prb.set_defaults(func=cmd_presence_beat)
    prs = prsub.add_parser("show", help="roster with live/idle/stale liveness")
    prs.add_argument("team"); add_json(prs)
    prs.set_defaults(func=cmd_presence_show)

    ag = sub.add_parser("agents", help="cross-agent digest (open work by agent + liveness)")
    ag.add_argument("team"); add_json(ag)
    ag.set_defaults(func=cmd_agents)

    def add_directive_flags(sp):
        sp.add_argument("--priority", "-p", default="P2"); sp.add_argument("--workstream", "-w")
        sp.add_argument("--summary", "-s"); sp.add_argument("--next", "-n")
        sp.add_argument("--from", dest="sender")

    tl = sub.add_parser("tell", help="direct work at an agent (directive = task w/ assignee)")
    tl.add_argument("team"); tl.add_argument("assignee"); tl.add_argument("title")
    add_directive_flags(tl); tl.set_defaults(func=cmd_tell)
    bc = sub.add_parser("broadcast", help="direct work at every agent (*)")
    bc.add_argument("team"); bc.add_argument("title")
    add_directive_flags(bc); bc.set_defaults(func=cmd_broadcast)
    rm = sub.add_parser("remind", help="scheduled directive, hidden until WHEN (ISO or 5d/36h/10m)")
    rm.add_argument("team"); rm.add_argument("assignee"); rm.add_argument("when"); rm.add_argument("title")
    add_directive_flags(rm); rm.set_defaults(func=cmd_remind)
    lt = sub.add_parser("later", help="capture a backlog idea (@backlog)")
    lt.add_argument("team"); lt.add_argument("title")
    add_directive_flags(lt); lt.set_defaults(func=cmd_later)
    it = sub.add_parser("intent", help="capture a spoken commitment (intent:<principal>); restatement never forks, a new --by updates the window in place")
    it.add_argument("team"); it.add_argument("title", help="the commitment text")
    it.add_argument("--for", dest="principal", required=True, help="the principal who owes the commitment (e.g. ash)")
    it.add_argument("--by", help="declared window (ISO or 5d/36h/10m); absent = undeclared -> fold uses capture+grace")
    it.add_argument("--from", dest="sender", help="capturing agent (records ownership)")
    it.add_argument("--priority", "-p", default="P2")
    it.set_defaults(func=cmd_intent)
    ho = sub.add_parser("handoff", help="atomic handoff: assignee + checkpoint ref in one write")
    ho.add_argument("team"); ho.add_argument("name"); ho.add_argument("--to", required=True)
    ho.add_argument("--checkpoint"); ho.add_argument("--next", "-n")
    ho.set_defaults(func=cmd_handoff)
    ib = sub.add_parser("inbox", help="open directives for an agent (--ack <slug> to ack)")
    ib.add_argument("team"); ib.add_argument("--agent", "-a"); ib.add_argument("--ack")
    ib.add_argument("--all", action="store_true", help="include @backlog"); add_json(ib)
    ib.set_defaults(func=cmd_inbox)
    hl = sub.add_parser("health", help="fleet health: which hosts reconcile this team (fulcra-agent-health)")
    hl.add_argument("team"); add_json(hl)
    hl.set_defaults(func=cmd_health)


    dr = sub.add_parser("doctor", help="local preflight: tooling + store reachability")
    dr.add_argument("team", nargs="?")
    dr.set_defaults(func=cmd_doctor)

    ak = sub.add_parser("asks", help="waiting-for-operator asks, oldest first (orchestrator pull)")
    ak.add_argument("team"); ak.add_argument("--human"); add_json(ak)
    ak.set_defaults(func=cmd_asks)
    aw = sub.add_parser("answer", help="operator return-leg: unblock + answer + hand back to owner")
    aw.add_argument("team"); aw.add_argument("name")
    aw.add_argument("--with", dest="with_text", required=True, help="the answer text")
    aw.add_argument("--human", help="operator handle (default $FULCRA_COORD_HUMAN or 'human') — must match the handle used with `asks`")
    aw.set_defaults(func=cmd_answer)

    bf = sub.add_parser("briefing", help="one-call session-start bundle (tolerates absent add-ons)")
    bf.add_argument("team"); bf.add_argument("--agent", "-a"); add_json(bf)
    bf.set_defaults(func=cmd_briefing)

    dg = sub.add_parser("digest", help="operator digest: blocked-on-you / upcoming / agents / stale")
    dg.add_argument("team"); dg.add_argument("--human"); add_json(dg)
    dg.add_argument("--store", action="store_true",
                    help="persist to _coord/digests/<date>-<window>.md (deduped per day+window)")
    dg.add_argument("--emit-timeline", action="store_true",
                    help="also emit the digest as a moment on the 'Agent Tasks — Digest' "
                         "timeline track (deterministic per-window record id upserts at "
                         "ingestion, so fleets and retries converge on one record; failed "
                         "emits retry on the next tick; best-effort, degrades to a no-op "
                         "when the timeline writer is absent)")
    dg.set_defaults(func=cmd_digest)
    es = sub.add_parser("escalate", help="role-vacancy sweep -> daily marker + P1 directive to maintainer")
    es.add_argument("team")
    es.set_defaults(func=cmd_escalate)

    rp = sub.add_parser("respond", help="answer + close a directive with an outcome")
    rp.add_argument("team"); rp.add_argument("name"); rp.add_argument("--outcome", "-o", required=True)
    rp.add_argument("--evidence", "-e"); rp.add_argument("--agent", "-a")
    rp.set_defaults(func=cmd_respond)

    tk = sub.add_parser("task", help="typed task lifecycle (fulcra-agent-tasks)")
    tksub = tk.add_subparsers(dest="task_command", required=True)
    tst = tksub.add_parser("start", help="create a task doc")
    tst.add_argument("team"); tst.add_argument("title")
    tst.add_argument("--workstream", "-w"); tst.add_argument("--status", default="proposed")
    tst.add_argument("--priority", "-p", default="P2"); tst.add_argument("--assignee")
    tst.add_argument("--summary", "-s"); tst.add_argument("--next", "-n")
    tst.add_argument("--kind", "-k"); tst.add_argument("--evidence", "-e")
    tst.add_argument("--force", action="store_true")
    tst.set_defaults(func=cmd_task_start)
    tup = tksub.add_parser("update", help="update a task (enforces the status machine)")
    tup.add_argument("team"); tup.add_argument("name")
    tup.add_argument("--status"); tup.add_argument("--priority", "-p"); tup.add_argument("--assignee")
    tup.add_argument("--summary", "-s"); tup.add_argument("--next", "-n")
    tup.add_argument("--blocked-on", dest="blocked_on"); tup.add_argument("--evidence", "-e")
    tup.set_defaults(func=cmd_task_update)
    tdn = tksub.add_parser("done", help="mark done (requires evidence)")
    tdn.add_argument("team"); tdn.add_argument("name"); tdn.add_argument("--evidence", "-e", required=True)
    tdn.set_defaults(func=cmd_task_done)
    tbl = tksub.add_parser("block", help="mark blocked (sets blocked_on; --on-user routes to a human)")
    tbl.add_argument("team"); tbl.add_argument("name")
    tbl.add_argument("--blocked-on", dest="blocked_on")
    tbl.add_argument("--on-user", dest="on_user", help="human-facing ask; assigns to FULCRA_COORD_HUMAN/human + tags needs:human")
    tbl.add_argument("--unlock", help="REQUIRED: what specifically unblocks this")
    tbl.set_defaults(func=cmd_task_block, verb="block")
    tpa = tksub.add_parser("pause", help="pause to waiting (requires --next)")
    tpa.add_argument("team"); tpa.add_argument("name"); tpa.add_argument("--next", "-n", required=True)
    tpa.set_defaults(func=cmd_task_pause, verb="pause")
    tab = tksub.add_parser("abandon", help="abandon (requires --reason)")
    tab.add_argument("team"); tab.add_argument("name"); tab.add_argument("--reason", "-r", required=True)
    tab.set_defaults(func=cmd_task_abandon, verb="abandon")
    trs = tksub.add_parser("restore", help="move an archived task back to the hot path")
    trs.add_argument("team"); trs.add_argument("name")
    trs.set_defaults(func=cmd_task_restore, verb="restore")
    tas = tksub.add_parser("assign", help="set/redirect assignee")
    tas.add_argument("team"); tas.add_argument("name"); tas.add_argument("assignee")
    tas.set_defaults(func=cmd_task_assign, verb="assign")
    tsp = tksub.add_parser("supersede", help="close live work and name its successor")
    tsp.add_argument("team"); tsp.add_argument("name")
    tsp.add_argument("--by", required=True, help="successor task slug or artifact")
    tsp.add_argument("--reason", "-r")
    tsp.set_defaults(func=cmd_task_supersede, verb="supersede")

    rv = sub.add_parser("review", help="review verdict tally (fulcra-agent-review)")
    rvsub = rv.add_subparsers(dest="review_command", required=True)
    rvq = rvsub.add_parser("request", help="open a review with required reviewers (durable obligation)")
    rvq.add_argument("team"); rvq.add_argument("name", help="slug or title")
    rvq.add_argument("--of", required=True, help="artifact under review (PR url or description)")
    rvq.add_argument("--reviewer", action="append", required=True,
                     help="required reviewer (role preferred); repeat for many")
    rvq.add_argument("--from", dest="sender", help="requesting agent (defaults to host)")
    rvq.set_defaults(func=cmd_review_request)
    rvs = rvsub.add_parser("status", help="APPROVED/CHANGES/PENDING from reviewers' verdicts")
    rvs.add_argument("team"); rvs.add_argument("slug"); add_json(rvs)
    rvs.set_defaults(func=cmd_review_status)
    rvr = rvsub.add_parser("restore", help="move an archived settled-single review back to the hot path")
    rvr.add_argument("team"); rvr.add_argument("slug")
    rvr.set_defaults(func=cmd_review_restore)

    ct = sub.add_parser("continuity", help="structured resumable snapshots (fulcra-agent-continuity)")
    ctsub = ct.add_subparsers(dest="continuity_command", required=True)
    cts = ctsub.add_parser("snapshot", help="write a structured resume snapshot")
    cts.add_argument("team"); cts.add_argument("agent"); cts.add_argument("task")
    cts.add_argument("--objective", required=True)
    cts.add_argument("--next", action="append", dest="next")
    cts.add_argument("--decision", action="append", dest="decision")
    cts.add_argument("--open-question", action="append", dest="open_question")
    cts.add_argument("--artifact", action="append", dest="artifact")
    cts.add_argument("--context-percent", type=float, dest="context_percent")
    cts.add_argument("--transcript", dest="transcript")
    cts.set_defaults(func=cmd_continuity_snapshot)
    ctc = ctsub.add_parser("checkpoint", help="get/set a role's durable checkpoint_ref")
    ctc.add_argument("team"); ctc.add_argument("--role", required=True); ctc.add_argument("--ref")
    ctc.set_defaults(func=cmd_continuity_checkpoint)
    ctp = ctsub.add_parser("park", help="session-exit: snapshot every held role + set checkpoint_refs")
    ctp.add_argument("team"); ctp.add_argument("--agent", "-a"); ctp.add_argument("--objective")
    ctp.add_argument("--next", action="append"); ctp.add_argument("--open-question", action="append", dest="open_question")
    ctp.set_defaults(func=cmd_continuity_park)

    ctr = ctsub.add_parser("resume", help="print a resume brief from the latest snapshot")
    ctr.add_argument("team"); ctr.add_argument("agent"); ctr.add_argument("task", nargs="?")
    ctr.add_argument("--json", action="store_true")
    ctr.set_defaults(func=cmd_continuity_resume)
    return p


def main(argv: Optional[list[str]] = None, transport: Any = None) -> int:
    args = build_parser().parse_args(argv)
    transport = transport if transport is not None else FulcraFileTransport()
    try:
        return args.func(args, transport)
    except Exception as e:  # never dump a traceback at the user
        # Registered error envelope. An unexpected exception is not a retryable
        # degrade: the `error:` register token (distinct from the "…, retry" /
        # tombstone voice of the degraded single-slug paths) makes it
        # machine-distinguishable to a watcher grepping stderr, carrying the
        # command + exception type as structured fields rather than an off-register
        # `coord-engine: {type}: {e}` prose line. rc 1 is preserved (behavior
        # unchanged); only the surface is now parseable — same register family as
        # the public-read degraded marker.
        cmd = getattr(args, "command", None) or "?"
        print(f"coord-engine: error: command={cmd} type={type(e).__name__}: {e}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
