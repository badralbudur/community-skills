"""The ``_coord/summaries.json`` aggregate + row diffing for the log.

The aggregate is a cache of the concept docs (never authoritative) — deleting it
and re-running reproduces it exactly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA = "coord.teams.summaries.v1"

#: The top-level keys this function owns: recomputed from scratch every pass, so
#: whatever the prior aggregate held for them is authoritative-stale and gets
#: overwritten. Every other top-level key is fold state — carried, not read.
OWNED_KEYS = ("schema", "team", "generated_at", "reconcile_host", "rows", "warnings")


def build_aggregate(
    team: str,
    rows: list[dict[str, Any]],
    *,
    generated_at: str,
    reconcile_host: str,
    warnings: Optional[list[str]] = None,
    state: Optional[dict[str, Any]] = None,
    prior: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the aggregate document one reconcile pass writes.

    ``state`` is this pass's own fold state (e.g. the ack fold's anchor + streak)
    merged in at the top level. ``prior`` is the aggregate this pass read; any
    top-level key in it that this build does not own (``OWNED_KEYS``) is carried
    forward untouched.

    The invariant, and why the passthrough exists: **summaries.json is one shared
    document written by many hosts at many versions, and any top-level key added
    in version N is silently wiped by every host older than N** — an older host
    rebuilds the document from the keys it knows about and writes the result over
    everyone else's. The wipe is not a race that eventually settles: the older
    host does it on every pass, so a newer host's fold state never survives to be
    read, and whatever fold depends on that state is pinned to its fallback for
    as long as the old host runs.

    So: **never rebuild this document from a fixed key set.** Preserving unknown
    keys cannot rescue a fleet from a host that predates the passthrough itself —
    only upgrading it can — but it stops the defect recurring one version later,
    when an older-but-still-preserving host meets a newer host's fold state. A key
    you do not recognize belongs to a version you are not; carry it.
    """
    out: dict[str, Any] = {
        "schema": SCHEMA,
        "team": team,
        "generated_at": generated_at,
        "reconcile_host": reconcile_host,
        "rows": rows,
        "warnings": warnings or [],
    }
    if isinstance(prior, dict):
        out.update({k: v for k, v in prior.items() if k not in OWNED_KEYS})
    if state:
        out.update(state)
    return out


def aggregate_rows(aggregate: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract rows from an aggregate dict, tolerating None/garbage."""
    if not isinstance(aggregate, dict):
        return []
    rows = aggregate.get("rows")
    return rows if isinstance(rows, list) else []


def rows_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(r.get("id")): r for r in rows if isinstance(r, dict) and r.get("id")}


def rows_by_name(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(r.get("name")): r for r in rows if isinstance(r, dict) and r.get("name")}


def _label(row: dict[str, Any]) -> str:
    title = row.get("title") or row.get("name") or row.get("id") or "untitled"
    link = row.get("name") or row.get("id") or "untitled"
    href = link if str(link).endswith(".md") else f"{link}.md"
    return f"[{title}]({href})"


# ---------------------------------------------------------------------------
# Categorization — the single source of truth for what counts as a transition
# ---------------------------------------------------------------------------
#
# Which changes count as a transition, and in what order, is decided here and
# nowhere else. Rendering is a separate concern layered on top: the byte-identity
# guard pins ``diff_rows``' formatting, not its categorization, so keeping the
# rule in one generator is what stops a formatter and a fold from drifting on the
# rule itself.

def _categorize(
    prior_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any], Optional[dict[str, Any]]]]:
    """Three-way categorization of the change from ``prior_rows`` to ``new_rows``,
    keyed by task id: ``(kind, row, prior_row)`` tuples where ``kind`` is one of
    ``create`` / ``update`` / ``deprecate``.

    * ``create``   — id present in new only; ``row`` = new row, ``prior_row`` None.
    * ``update``   — id in both with a changed ``status``; ``row`` = new row,
                     ``prior_row`` = the prior row (its old status, for the arrow).
    * ``deprecate``— id present in prior only; ``row`` = the removed prior row,
                     ``prior_row`` None.

    Content-only edits (same status) are intentionally not a change (they live in
    the file's own version history). Order is stable: creations + status-updates
    over ``new`` (by id) first, then removals over ``prior`` (by id) — the order
    ``diff_rows`` has always emitted, which the byte-identity guard pins.
    """
    prior = rows_by_id(prior_rows)
    new = rows_by_id(new_rows)
    out: list[tuple[str, dict[str, Any], Optional[dict[str, Any]]]] = []
    for rid, r in new.items():
        if rid not in prior:
            out.append(("create", r, None))
        elif prior[rid].get("status") != r.get("status"):
            out.append(("update", r, prior[rid]))
    for rid, r in prior.items():
        if rid not in new:
            out.append(("deprecate", r, None))
    return out


def diff_rows(
    prior_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> list[str]:
    """OKF §7 log bullets for changes from ``prior_rows`` to ``new_rows``.

    Creations, status transitions, and removals — keyed by task id. Content-only
    edits (no status change) are intentionally not logged (they're in the file's
    own version history). Categorization comes from :func:`_categorize`; this
    function only renders the bullets.
    """
    out: list[str] = []
    for kind, r, prior_r in _categorize(prior_rows, new_rows):
        if kind == "create":
            out.append(f"* **Creation**: {_label(r)} created ({r.get('status')}).")
        elif kind == "update":
            out.append(
                f"* **Update**: {_label(r)} "
                f"{(prior_r or {}).get('status')} → {r.get('status')}."
            )
        else:  # deprecate
            out.append(f"* **Deprecation**: {_label(r)} removed.")
    return out


#: The store's ``file list`` mtime format(s) — UTC, minute-granular, e.g.
#: ``2026-07-01 04:12PM UTC`` (see ``transport.parse_list_output``). It carries a
#: full date (year included), so it normalizes cleanly to a UTC-``Z`` ISO instant:
#: there is no ls-style yearless ambiguity to resolve.
_STORE_MTIME_FORMATS = ("%Y-%m-%d %I:%M%p %Z", "%Y-%m-%d %I:%M%p")


def _parse_store_mtime(s: str) -> Optional[datetime]:
    """Parse the transport's list-style mtime string into a datetime, or None.

    The store lists times in UTC; a naive parse (``%Z`` absent) is stamped UTC so
    the normalized result is a real UTC instant, not a floating one. Never raises."""
    for fmt in _STORE_MTIME_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None
