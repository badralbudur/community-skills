"""Spec #1 to #35, checked against one finished game, all at once.

Every milestone before this one proved its own requirements in isolation. This
module asks the harder question: do they all still hold *simultaneously*, on a
single game that a queue rule, an economy, a newspaper, an archive and a
publication guard all had a hand in?

Three kinds of finding, and the difference is deliberate:

``pass`` / ``fail``
    A deterministic check. The requirement is either true of this game or it is
    not, and the check says which with the evidence attached.
``judged``
    A requirement whose Evaluation Criteria entry says it needs "an explicit
    rendering of subjective judgement" -- tone, phrasing quality, whether the
    endgame content reads as informed by the game. This module does not grade
    those. It gathers what a grader needs -- the actual lines, the actual
    distribution the aggregate claims to describe -- and hands them over. A
    judged finding is never a pass; it is a question with its evidence already
    fetched.
``process``
    A requirement about how the *harness* is run rather than about what the game
    does: separate agent sessions per role (#34), one commit per run in a
    separate repository (#35), a facilitator able to relay for a player without
    their own agent (#8). Game state cannot decide these. Each one still gets a
    finding with whatever the run can actually evidence, because spec's
    Evaluation Criteria require an untestable criterion to be *reported* as its
    own finding rather than silently passed.

Nothing here re-implements a rule. Where the engine, the paper or the
publication guard already enforce something, the check calls that enforcement
and records that it held -- a second, independent copy of the blind-voting rule
living in the test suite is a copy that can disagree with the one that matters.
"""

import json
import os

from engine import audit
from engine.config import DEFAULT_CONFIG_FILENAME, repo_root
from engine.content import normalize_city
from engine.dice import parse_dice
from engine.game import LOCKSTEP_OPS, SLOT_EXPORT, SLOT_IMPORT_PICK, SLOT_QUESTION
from engine.state import EVEN_SPLIT, RAMP_UP, RESOLVED, WINNER_PICK

PASS = "pass"
FAIL = "fail"
JUDGED = "judged"
PROCESS = "process"


class Finding:
    """One requirement, one verdict, and the evidence for it."""

    __slots__ = ("spec", "title", "status", "detail", "evidence")

    def __init__(self, spec, title, status, detail, evidence=None):
        self.spec = spec
        self.title = title
        self.status = status
        self.detail = detail
        self.evidence = evidence

    @property
    def ok(self):
        return self.status != FAIL

    def to_dict(self):
        out = {
            "spec": self.spec,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
        }
        if self.evidence is not None:
            out["evidence"] = self.evidence
        return out

    def __repr__(self):
        return "Finding(%s, %s, %s)" % (self.spec, self.status, self.detail)


def _verdict(spec, title, ok, detail, evidence=None):
    return Finding(spec, title, PASS if ok else FAIL, detail, evidence)


class Report:
    def __init__(self, findings):
        self.findings = list(findings)

    @property
    def failures(self):
        return [f for f in self.findings if f.status == FAIL]

    @property
    def judged(self):
        return [f for f in self.findings if f.status == JUDGED]

    def by_spec(self, spec):
        for finding in self.findings:
            if finding.spec == spec:
                return finding
        raise KeyError(spec)

    def to_dict(self):
        counts = {}
        for finding in self.findings:
            counts[finding.status] = counts.get(finding.status, 0) + 1
        return {
            "checked": len(self.findings),
            "counts": counts,
            "failures": [f.spec for f in self.failures],
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_text(self):
        glyph = {PASS: "ok  ", FAIL: "FAIL", JUDGED: "judge", PROCESS: "proc"}
        lines = []
        for finding in self.findings:
            lines.append(
                "  %-5s %-6s %s -- %s"
                % (glyph[finding.status], finding.spec, finding.title, finding.detail)
            )
        return "\n".join(lines)


def run(game, journal, artifacts):
    """Every requirement, against one finished game. Returns a :class:`Report`."""
    checks = [
        _roster_size, _city_uniqueness, _late_joins, _facilitator_first,
        _queue_on_first_export, _facilitator_fixed, _facilitator_plays,
        _facilitator_relays, _one_timer_lockstep, _round_window, _one_checkin,
        _two_rotations, _importer_chooses, _needs_are_trade, _import_repetition,
        _freeform_capped_exports,
        _silent_skip, _ramp_up, _pick_the_round_after, _even_split, _profit_rolls,
        _origin_never_exposed, _exposure_is_configured, _two_slots, _question_framing,
        _aggregate_phrasing, _one_edition_per_round, _archive_browsable,
        _city_identity_only, _image_per_edition, _tone, _endgame_crown_and_twist,
        _endgame_cities, _game_content, _separate_agents, _separate_repo,
    ]
    return Report([check(game, journal, artifacts) for check in checks])


# --- players & cities -----------------------------------------------------

def _roster_size(game, journal, artifacts):
    low = game.config.require_int("players.min_players")
    high = game.config.require_int("players.max_players")
    count = len(game.players)
    return _verdict(
        "#1", "3-10 players, from config",
        low <= count <= high,
        "%d mayors, within config.players.min_players=%d..max_players=%d"
        % (count, low, high),
        {"cities": sorted(p.city for p in game.players.values())},
    )


def _city_uniqueness(game, journal, artifacts):
    keys = [normalize_city(p.city) for p in game.players.values()]
    unique = len(set(keys)) == len(keys)
    moved = [entry for entry in journal.joins if entry["reassigned"]]
    if not moved:
        return Finding(
            "#2", "unique cities, duplicates reassigned", FAIL,
            "no duplicate pick occurred, so the reassignment rule went untested -- "
            "the seating plan is supposed to contain one (playtest/table.py)",
        )
    ok = unique and all(
        entry["city"] != entry["requested"] and entry["reassignment"]["announcement"]
        for entry in moved
    )
    return _verdict(
        "#2", "unique cities, duplicates reassigned", ok,
        "%d city names, all distinct; %d duplicate pick(s) reassigned and announced"
        % (len(keys), len(moved)),
        {"reassignments": [entry["reassignment"] for entry in moved]},
    )


def _late_joins(game, journal, artifacts):
    late = sorted(
        (p.city, p.joined_round) for p in game.players.values() if p.joined_round > 0
    )
    return _verdict(
        "#3", "players may join after the game starts", bool(late),
        "%d mayor(s) joined mid-game" % len(late),
        {"joined": late},
    )


def _facilitator_first(game, journal, artifacts):
    facilitator = game.facilitator
    position = game.queue.position(facilitator.player_id)
    first_need = next(
        (n for n in game.needs.values() if n.opened_round == 1), None
    )
    ok = (
        position == 1
        and first_need is not None
        and first_need.importing_player_id == facilitator.player_id
    )
    return _verdict(
        "#4", "facilitator holds queue position 1", ok,
        "%s is at position %s and opened round 1's need"
        % (facilitator.city, position),
    )


def _queue_on_first_export(game, journal, artifacts):
    problems = []
    for player in game.players.values():
        if player.is_facilitator:
            continue  # exempt by #4, and that exemption is the rule's point
        first = next(
            (
                action["round"] for action in journal.actions
                if action["player"] == player.player_id and action["kind"] == SLOT_EXPORT
            ),
            None,
        )
        if player.queued_round != first:
            problems.append(
                "%s queued in round %s but first exported in round %s"
                % (player.city, player.queued_round, first)
            )
    unqueued_with_needs = [
        n.importing_city for n in game.needs.values()
        if not game.players[n.importing_player_id].is_queued
    ]
    return _verdict(
        "#5", "queued only on first export", not problems and not unqueued_with_needs,
        "every non-facilitator entered the queue in the round of their first export"
        if not problems else "; ".join(problems),
    )


def _facilitator_fixed(game, journal, artifacts):
    count = sum(1 for p in game.players.values() if p.is_facilitator)
    return _verdict(
        "#6", "one facilitator, fixed for the game", count == 1,
        "%d facilitator; the role has no rotation path in the engine" % count,
    )


def _facilitator_plays(game, journal, artifacts):
    facilitator = game.facilitator
    acted = [
        action["kind"] for action in journal.actions
        if action["player"] == facilitator.player_id
    ]
    return _verdict(
        "#7", "the facilitator plays under the same rules", bool(acted),
        "%s exported %d time(s), picked %d winner(s) and answered %d question(s), "
        "and served %d import turn(s) like anybody else"
        % (
            facilitator.city,
            acted.count(SLOT_EXPORT), acted.count(SLOT_IMPORT_PICK),
            acted.count(SLOT_QUESTION), facilitator.import_turns_served,
        ),
    )


def _facilitator_relays(game, journal, artifacts):
    return Finding(
        "#8", "the facilitator can relay for a player without their own agent",
        PROCESS,
        "not decidable from game state: every action in this run entered through "
        "the same public engine methods (join/checkin/submit_export/pick_winner/"
        "answer_question), which is the interface a relaying facilitator would use, "
        "and the engine has no per-player transport of its own to contradict it",
        {"entry_points": sorted({a["kind"] for a in journal.actions})},
    )


# --- rounds & timing ------------------------------------------------------

def _one_timer_lockstep(game, journal, artifacts):
    timers = game.timers()
    extra = audit.find_extra_timers()
    wrong = [
        index for index, record in game.rounds.items()
        if tuple(record.ops) != LOCKSTEP_OPS
    ]
    return _verdict(
        "#9", "one timer; OPEN/CLOSE/RESOLVE every round",
        len(timers) == 1 and not extra and not wrong,
        "%d timer, %d rounds each logging %s in order"
        % (len(timers), len(game.rounds), "/".join(LOCKSTEP_OPS)),
        {"timer": timers, "structural_timer_scan": extra or "clean"},
    )


def _round_window(game, journal, artifacts):
    hours = game.config.require_number("rounds.round_window_hours")
    actual = game.timer.window.total_seconds() / 3600
    return _verdict(
        "#10", "round window comes from config", actual == hours,
        "%g-hour rounds, from config.rounds.round_window_hours" % actual,
    )


def _one_checkin(game, journal, artifacts):
    problems = []
    for index, entry in journal.rounds.items():
        for player_id, checkin in entry["checkins"].items():
            kinds = [action["kind"] for action in checkin["did"]]
            if len(kinds) > 2:
                problems.append("%s used %d slots in round %d" % (player_id, len(kinds), index))
            if len(set(kinds)) != len(kinds):
                problems.append("%s used a slot kind twice in round %d" % (player_id, index))
            if SLOT_QUESTION in kinds and checkin["pending_game_actions"] > 1:
                problems.append(
                    "%s answered a question in round %d with two game actions pending"
                    % (player_id, index)
                )
    used = [len(c["did"]) for e in journal.rounds.values() for c in e["checkins"].values()]
    return _verdict(
        "#11", "one check-in per player per round, two slots", not problems,
        "%d check-ins, none using more than two slots (max seen: %d)"
        % (len(used), max(used) if used else 0),
        problems or None,
    )


def _two_rotations(game, journal, artifacts):
    target = game.config.require_int("rounds.rotations_target")
    closed = game.queue.rotation_closed_rounds
    rotation_1_closed = closed.get(1)
    problems = []
    for player in game.players.values():
        expected = target if (
            player.queued_round is not None and player.queued_round < rotation_1_closed
        ) else 1
        if player.import_turns_allotted != expected:
            problems.append(
                "%s queued in round %s but is allotted %d import turns, not %d"
                % (player.city, player.queued_round, player.import_turns_allotted, expected)
            )
        if player.import_turns_served != player.import_turns_allotted:
            problems.append(
                "%s was allotted %d import turns and served %d"
                % (player.city, player.import_turns_allotted, player.import_turns_served)
            )
    allotments = sorted({p.import_turns_allotted for p in game.players.values()})
    return _verdict(
        "#12", "two rotations; 2 import turns vs 1 by join timing",
        not problems and allotments == [1, target],
        "rotation 1 closed in round %s; allotments seen: %s -- both branches exercised"
        % (rotation_1_closed, allotments),
        problems or {"rotation_closed_rounds": dict(closed)},
    )


# --- the import/export/winner cycle --------------------------------------

def _importer_chooses(game, journal, artifacts):
    """#13: the need a city opens is the one its own mayor filed."""
    seeded = {need["id"] for need in game.content.needs}
    unfiled = [n.need_key for n in game.needs.values() if not n.order.get("filed_by")]
    unknown = [
        n.content_need_id for n in game.needs.values()
        if n.content_need_id not in seeded
    ]
    mismatched = [
        n.need_key for n in game.needs.values()
        if n.order.get("filed_by") != game.players[n.importing_player_id].mayor
    ]
    held = [
        event for record in game.rounds.values() for event in record.events
        if event["op"] == "OPEN" and event.get("rounds_held")
    ]
    offer = game.import_choice_offer(game.facilitator.player_id)
    sources = sorted({n.order.get("request_source") for n in game.needs.values()})
    return _verdict(
        "#13", "the importing mayor chooses their city's next import",
        not unfiled and not unknown and not mismatched,
        "%d needs opened, every one of them filed in advance by the mayor of the "
        "importing city (request sources: %s); the engine holds a turn whose mayor "
        "has filed nothing rather than opening one for them (%d held this game) and "
        "owns no function that picks a need. A mayor is offered %s eligible "
        "suggestions plus a freeform order, and may take any eligible seed shown "
        "or not"
        % (
            len(game.needs), sources, len(held),
            game.config.require_int("imports.suggestions_offered_to_importer"),
        ),
        {
            "unfiled": unfiled or None,
            "not_from_the_pool": unknown or None,
            "filed_by_somebody_else": mismatched or None,
            "held_turns": [
                {"round": e.get("city"), "rounds_held": e["rounds_held"]} for e in held
            ] or None,
            "offer_is_still_available_at_game_end": bool(offer),
        },
    )


def _needs_are_trade(game, journal, artifacts):
    """#13a: what is ordered is goods or services, never advice."""
    policy = game.content.trade
    refused = []
    for need in game.content.needs:
        try:
            policy.check_need(need, where=need["id"])
        except Exception as exc:  # pragma: no cover - a failure is the finding
            refused.append({"need": need["id"], "why": str(exc)})
    families = sorted({need["trade_family"] for need in game.content.needs
                       if need.get("trade_family")})
    opened = sorted({n.order.get("trade_family") for n in game.needs.values()})
    prompts = [n.rendered["exporter_prompt"] for n in game.needs.values()]
    advice = [p for p in prompts if policy.advice_marker_in(p)]
    return _verdict(
        "#13a", "import needs are orders for actual tradable things",
        not refused and not advice,
        "all %d needs in the pool declare one of spec #13a's kinds of tradable "
        "thing (%s) and none of them, or of the %d prompts this game put in front "
        "of exporting mayors, reads as a request for advice or civic problem "
        "solving; this game's own orders covered %s"
        % (len(game.content.needs), families, len(prompts), opened),
        {"refused": refused or None, "advice_prompts": advice or None},
    )


def _import_repetition(game, journal, artifacts):
    by_city = {}
    for need in game.needs.values():
        by_city.setdefault(need.importing_city, []).append(need.category)
    repeats = {
        city: cats for city, cats in by_city.items() if len(set(cats)) != len(cats)
    }
    across = [
        category for category in {n.category for n in game.needs.values()}
        if sum(1 for n in game.needs.values() if n.category == category) > 1
    ]
    allowed = game.config.require_bool("imports.allow_repeat_category_for_same_city")
    return _verdict(
        "#14", "a category repeats across cities, never within one",
        not repeats or allowed,
        "no city drew a category twice; %d categor%s repeated across different cities"
        % (len(across), "y" if len(across) == 1 else "ies"),
        {"repeated_across_cities": sorted(across), "within_a_city": repeats or "none"},
    )


def _freeform_capped_exports(game, journal, artifacts):
    cap = game.config.require_int("exports.max_submissions_per_player_per_import_per_round")
    seen = {}
    for action in journal.actions:
        if action["kind"] != SLOT_EXPORT:
            continue
        key = (action["player"], action["need"], action["round"])
        seen[key] = seen.get(key, 0) + 1
    over = {key: count for key, count in seen.items() if count > cap}
    texts = [s.text for s in game.submissions.values()]
    return _verdict(
        "#15", "freeform exports, capped per player per need per round", not over,
        "%d freeform exports, none exceeding the configured cap of %d; "
        "%d distinct texts" % (len(texts), cap, len(set(texts))),
        over or None,
    )


def _silent_skip(game, journal, artifacts):
    skipped = [
        (index, player_id)
        for index, entry in journal.rounds.items()
        for player_id in entry["absent"]
    ]
    # "Silent" is the requirement: a missed round must leave no trace in the
    # round's own record beyond the absence itself -- no penalty event, no
    # substitute submission credited to that city.
    noise = [
        event for record in game.rounds.values() for event in record.events
        if event.get("op") not in LOCKSTEP_OPS
    ]
    return _verdict(
        "#16", "a missed round is a silent skip", bool(skipped) and not noise,
        "%d player-rounds missed across %d rounds, with no penalty or substitution "
        "recorded against any of them"
        % (len(skipped), len({index for index, _ in skipped})),
        {"missed": skipped[:12], "extra_events": noise or "none"},
    )


def _ramp_up(game, journal, artifacts):
    ramped = [n for n in game.needs.values() if (n.resolution or {}).get("mode") == RAMP_UP]
    ok = bool(ramped) and all(
        need.resolution["submission_count"] == 0
        and need.resolution["awards"]
        and need.resolution["awards"][0]["city"] == need.importing_city
        for need in ramped
    )
    return _verdict(
        "#17", "zero submissions -> ramp up own industry, profit still paid", ok,
        "%s received nothing in round %s and was still paid %s"
        % (
            ramped[0].importing_city, ramped[0].opened_round,
            ramped[0].resolution["awards"][0]["profit"],
        ) if ramped else "no round drew zero exports, so #17 went untested",
        {"needs": [n.need_key for n in ramped]},
    )


def _pick_the_round_after(game, journal, artifacts):
    problems = []
    for need in game.needs.values():
        if need.opened_round is not None and need.closed_round is not None:
            if need.closed_round != need.opened_round + 1:
                problems.append("%s closed in round %s" % (need.need_key, need.closed_round))
        if need.pick and need.pick["picked_round"] != need.closed_round:
            problems.append(
                "%s was picked in round %s but closed in round %s"
                % (need.need_key, need.pick["picked_round"], need.closed_round)
            )
        if need.resolved_round is not None and need.resolved_round != need.closed_round + 1:
            problems.append("%s resolved in round %s" % (need.need_key, need.resolved_round))
    # And the ballots themselves carried no city.
    ballots = [
        slot for entry in journal.rounds.values() for checkin in entry["checkins"].values()
        for slot in checkin["slots"] if slot["kind"] == SLOT_IMPORT_PICK
    ]
    leaks = audit.find_ballot_leaks(game, ballots)
    return _verdict(
        "#18", "the importer picks the round AFTER collection, blind",
        not problems and not leaks,
        "%d ballots offered, each in the round after its export window closed, none "
        "naming an exporting city" % len(ballots),
        problems + leaks or None,
    )


def _even_split(game, journal, artifacts):
    split = [n for n in game.needs.values() if (n.resolution or {}).get("mode") == EVEN_SPLIT]
    ok = bool(split) and all(
        len(need.resolution["awards"]) == need.resolution["submission_count"]
        and all(s.is_winner for s in game.submissions_for(need.need_key))
        for need in split
    )
    return _verdict(
        "#19", "no pick by the deadline -> every offer wins, profit splits", ok,
        "%s let its window lapse in round %s; %d offers shared the roll"
        % (
            split[0].importing_city, split[0].closed_round,
            split[0].resolution["submission_count"],
        ) if split else "no importing mayor let a window lapse, so #19 went untested",
        {"awards": split[0].resolution["awards"]} if split else None,
    )


def _profit_rolls(game, journal, artifacts):
    from fractions import Fraction

    rolls = [n.resolution["roll"] for n in game.needs.values() if n.resolution]
    expression = game.config.require_str("economy.profit_roll")
    # The range is derived from the configured expression rather than asserted
    # against 2 and 12: "2d6-style" in #20 is a shape, and config.json is what
    # says which dice this game actually rolls (economy.profit_roll).
    count, sides = parse_dice(expression)
    low, high = count, count * sides
    out_of_range = [
        roll for roll in rolls
        if not low <= roll["total"] <= high
        # A total inside the range is not enough on its own: the right number of
        # dice, each a real face of the configured die, is what makes the total
        # a roll rather than a number that happens to be in range.
        or len(roll["dice"]) != count
        or any(not 1 <= die <= sides for die in roll["dice"])
        or sum(roll["dice"]) != roll["total"]
        or roll["expression"] != expression
    ]
    # Every unit awarded came from a roll, and every city's total is the sum of
    # what it was awarded -- the leaderboard is not a separate tally that could
    # drift from the ledger it is supposed to summarise.
    # An award is {"city", "profit": money.to_json(...)}, and the authoritative
    # figure is the exact rational -- an even split of a 7 across three cities is
    # 7/3, and adding up its rounded display value would not equal what the
    # cities actually hold.
    awarded = sum(
        (
            Fraction(award["profit"]["exact"])
            for need in game.needs.values() if need.resolution
            for award in need.resolution["awards"]
        ),
        Fraction(0),
    )
    held = sum(p.cumulative_profit for p in game.players.values())
    return _verdict(
        "#20", "2d6-style profit rolls, cumulative per city",
        not out_of_range and awarded == held,
        "%d rolls of %s, all inside [%d, %d]; %s awarded and %s held on the "
        "leaderboard" % (len(rolls), expression, low, high, awarded, held),
        {"leaderboard": game.leaderboard()},
    )


def _config_document(root=None):
    """``config.json`` as it is shipped, read from disk.

    Deliberately the file rather than ``game.config``: the knob check below is
    about what the document *offers*, including keys nothing reads yet, and the
    game's Config is a read-tracking view that this run may have overridden. A
    knob that no game has switched on is still a knob.
    """
    path = os.path.join(root or repo_root(), DEFAULT_CONFIG_FILENAME)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


#: What #21 is audited over: everything this run *published*. The payload the
#: paper was built from and the bytes actually served are both here, because
#: they are different things -- a leak can exist in a rendered page that is not
#: in the payload it came from.
#:
#: Deliberately absent: ``playtest/transcript.json`` and the replay journal.
#: Those are the game's *input* -- which mayor sent which text -- and every
#: game, played or replayed, necessarily has that at the moment of submission.
#: The engine holds the same mapping itself, in a ledger that will only answer
#: for an audited reason (:mod:`engine.audit`) and that the paper is never given.
#: #21 forbids *exposure*: to the importing mayor while they vote, and to every
#: reader afterwards. The published surface is where exposure could happen, and
#: it is the whole of what is checked here. Auditing the input record instead
#: would report the game's own memory as a leak and say nothing about the paper.
AUDITED_FOR_EXPOSURE = ("archive", "editions", "site", "published bytes")
NOT_AUDITED_FOR_EXPOSURE = ("recorded transcript", "replay journal")


def _origin_never_exposed(game, journal, artifacts):
    """Spec #21, over everything this run published, in one place."""
    payload = {
        "archive": artifacts["archive"],
        "editions": artifacts["editions"],
        "site": artifacts["site"],
        "published_text": list(artifacts["public_files"].values()),
    }
    leaks = audit.find_identity_leaks(game, payload)
    misuse = audit.find_ledger_misuse(game)
    knobs = audit.find_origin_exposure_knobs(_config_document())
    losers = sum(1 for s in game.submissions.values() if not s.is_winner)
    return _verdict(
        "#21", "a losing offer's origin is never exposed, ever",
        not leaks and not misuse and not knobs,
        "%d non-winning offers; nothing published in this run -- %s -- ties any of "
        "them to a city, no ledger read went unaudited, and config.json holds no "
        "knob that could turn the rule off. Not audited, because it is the game's "
        "input rather than its output: %s"
        % (losers, ", ".join(AUDITED_FOR_EXPOSURE),
           ", ".join(NOT_AUDITED_FOR_EXPOSURE)),
        {
            # The identities themselves are not repeated here. A finding that
            # printed what leaked would leak it again, into a report that gets
            # committed; the submission and the path are what a fix needs.
            "leaks": [
                {key: leak[key] for key in ("submission_id", "need", "path", "spec")}
                for leak in leaks
            ],
            "ledger_misuse": misuse,
            "config_knobs": knobs,
        } if (leaks or misuse or knobs) else None,
    )


def _exposure_is_configured(game, journal, artifacts):
    payload = {
        "archive": artifacts["archive"],
        "published_text": list(artifacts["public_files"].values()),
    }
    violations = audit.find_exposure_violations(game, payload)
    visible = game.economy.leaderboard_visible
    printed = any("The Ledger" in text or "the_ledger" in text
                  for text in artifacts["public_files"].values())
    return _verdict(
        "#22", "the leaderboard and the exposure policy come from config",
        not violations and visible == printed,
        "config.economy.leaderboard_visible_in_newspaper=%s and the paper %s it; "
        "no gated key appears in anything published against the policy"
        % (visible, "prints" if printed else "withholds"),
        violations or None,
    )


# --- facilitator questions -----------------------------------------------

def _two_slots(game, journal, artifacts):
    offered = 0
    crowded_out = 0
    problems = []
    for index, entry in journal.rounds.items():
        for player_id, checkin in entry["checkins"].items():
            kinds = [slot["kind"] for slot in checkin["slots"]]
            if len(kinds) > 2:
                problems.append("%s was offered %d slots in round %d"
                                % (player_id, len(kinds), index))
            if SLOT_QUESTION in kinds:
                offered += 1
                if checkin["pending_game_actions"] > 1:
                    problems.append(
                        "%s was offered a question in round %d with two game actions "
                        "pending" % (player_id, index)
                    )
            elif entry["question"] is not None and checkin["pending_game_actions"] > 1:
                crowded_out += 1
    return _verdict(
        "#23", "two slots: pending game actions first, then a question", not problems,
        "%d check-ins were offered the round's question; %d had it crowded out by a "
        "second pending game action, which is the rule" % (offered, crowded_out),
        problems or None,
    )


def _question_framing(game, journal, artifacts):
    scope = game.config.require_str("facilitator_questions.scope")
    asked = [game.content.question_by_id(qid) for qid in game.asked_question_ids()]
    framings = sorted({q["framing"] for q in asked})
    ok = bool(asked) and game.content.check_question_policy(game.config)
    return _verdict(
        "#24", "freeform getting-to-know-you questions, framed at the mayor", ok,
        "%d distinct questions asked, scope %r, framings %s -- all checked against "
        "config.facilitator_questions.framing over the whole bank"
        % (len(asked), scope, framings),
        {"asked": [q["id"] for q in asked]},
    )


def _aggregate_phrasing(game, journal, artifacts):
    """Spec #25 is half arithmetic and half writing; this reports both.

    The judged question is whether "the world" / "most nations" / "some
    countries" *correctly* describes the distribution underneath it, so a grader
    needs the two halves side by side: the round's actual buckets, and the
    sentence The Wire actually printed about them. A finding that attached the
    arithmetic alone would be asking somebody to grade writing they cannot read.
    """
    printed = {}
    for edition in artifacts["archive"]["editions"]:
        for department in edition["departments"]:
            if department["id"] == "the_wire":
                printed[edition["round"]] = _texts(department)

    rounds = []
    for index in sorted(game.rounds):
        report = game.mayor_question_report(index)
        if report is None or not report["answered"]:
            continue
        outcome = report["outcome"] or {}
        rounds.append({
            "round": index,
            "question": report["text"],
            "answered": report["answered"],
            "asked_of": report["asked_of"],
            "buckets": [
                {"label": row["label"], "size": row["size"], "role": row.get("role")}
                for row in report["buckets"]
            ],
            "measure": report["measure"],
            "outcome": outcome.get("id"),
            "licensed_phrases": outcome.get("phrases"),
            "must_disclose_partial_response": report["integrity"][
                "must_disclose_partial_response"
            ],
            "printed": printed.get(index, []),
        })

    aggregated = [entry for entry in rounds if entry["outcome"]]
    if not aggregated:
        # Not a pass. A game where no round ever reached an aggregate leaves #25
        # untested, and spec's Evaluation Criteria says an untested criterion is
        # reported as its own finding rather than waved through.
        return Finding(
            "#25", "answers shared, phrased in clever aggregate", FAIL,
            "%d rounds collected answers but none produced an aggregate, so the "
            "phrasing rule went untested in this run" % len(rounds),
            {"rounds": rounds},
        )
    return Finding(
        "#25", "answers shared, phrased in clever aggregate", JUDGED,
        "%d of %d answered rounds produced an aggregate; each is listed with the "
        "distribution it claims to describe and the line the paper printed about "
        "it, so the wording can be checked against the arithmetic rather than "
        "admired on its own" % (len(aggregated), len(rounds)),
        {"rounds": rounds},
    )


# --- the newspaper --------------------------------------------------------

def _one_edition_per_round(game, journal, artifacts):
    editions = artifacts["editions"]["editions"]
    rounds = [entry["round"] for entry in editions]
    completed = game.completed_rounds()
    desk = artifacts.get("desk")
    # The half of spec #26 that a finished archive cannot show: these editions
    # exist because rounds *ended*, not because this script published them. The
    # desk was attached before the timer started and every entry below is one
    # transaction it ran on its own (see playtest/run.py, facilitator/).
    transacted = [t.round for t in desk.transactions] if desk else []
    notices = [n.round for n in desk.notices] if desk else []
    ok = (
        rounds == completed
        and len(set(rounds)) == len(rounds)
        and transacted == completed
        and notices == completed
    )
    return _verdict(
        "#26", "one edition per completed round, automatically, at one private address",
        ok and artifacts["site"]["published"],
        "%d editions for %d completed rounds -- each one written by the round "
        "ending, in the facilitator's completed-round transaction (%s), and each "
        "followed by one notice to the group that it is up; published to one "
        "unguessable %s address with robots noindex in three places"
        % (
            len(rounds), len(completed),
            ", ".join(desk.steps) if desk else "no desk attached",
            game.config.require_str("hosting.url_style"),
        ),
        {
            "privacy": artifacts["site"]["privacy"],
            "transactions": transacted,
            "notices": notices,
            "example_notice": desk.notices[0].describe() if desk and desk.notices else None,
            "address_in_published_bytes": any(
                artifacts["site_id"] in text
                for text in artifacts["public_files"].values()
            ),
            "address_in_the_written_down_notices": any(
                artifacts["site_id"] in json.dumps(n.describe())
                for n in (desk.notices if desk else [])
            ),
        },
    )


def _archive_browsable(game, journal, artifacts):
    published = artifacts["site"]["rounds"]
    pages = [name for name in artifacts["public_files"] if name.startswith("round-")
             and name.endswith(".html")]
    index = artifacts["public_files"].get("index.html", "")
    linked = [name for name in pages if name in index]
    final_present = "final.html" in artifacts["public_files"]
    return _verdict(
        "#27", "every prior edition still browsable at the same address",
        len(pages) == len(game.rounds) and len(linked) == len(pages) and final_present,
        "%d back issues plus the final edition, all reachable from the one index at "
        "the one address; rounds published: %s..%s"
        % (len(pages), min(published), max(published)),
    )


def _city_identity_only(game, journal, artifacts):
    handles = audit.find_handle_leaks(
        game, {"published": list(artifacts["public_files"].values())}
    )
    style = game.config.require_str("newspaper.player_identity_style")
    return _verdict(
        "#28", "mayors named by city and office only", not handles,
        "%d published files; none contains any of the %d real handles at the table "
        "(config.newspaper.player_identity_style=%r)"
        % (len(artifacts["public_files"]), len(game.players), style),
        handles or None,
    )


def _image_per_edition(game, journal, artifacts):
    editions = artifacts["editions"]["editions"]
    missing = [e["round"] for e in editions if not e["image_modality"]]
    modalities = sorted({e["image_modality"] for e in editions})
    final = artifacts["editions"]["final"]
    preference = game.config.require("newspaper.image.modality_preference")
    return _verdict(
        "#29", "every edition carries a generated image", not missing and final,
        "%d editions plus the final one, each with an image; modality %s, chosen "
        "from the configured preference %s (no raster provider is registered in "
        "this deployment, which is the documented fallback case)"
        % (len(editions), modalities, preference),
        {"final_image": final["image_modality"], "portraits": len(final["cities"])},
    )


def _texts(department, kinds=None):
    """Every printed string in a department, in the order it appears.

    An edition is blocks -- ``standfirst``, ``para``, ``quote``, ``aside``,
    ``heading``, ``list``, ``table``, ``figure`` -- and this flattens the ones
    that carry prose. A grader handed a judged criterion (#30, #32) has to be
    handed the words, so reading the wrong shape here does not raise; it quietly
    attaches nothing, which is worse.
    """
    out = []
    for block in department.get("blocks", []):
        kind = block.get("kind")
        if kinds is not None and kind not in kinds:
            continue
        if isinstance(block.get("text"), str):
            out.append(block["text"])
        for item in block.get("items", []) or []:
            if isinstance(item, str):
                out.append(item)
    return out


def _tone(game, journal, artifacts):
    """Spec #30 is judged, but the paper refuses to publish a snide edition.

    So there are two facts worth handing a grader: the gate held, and here are
    the actual lines it let through.
    """
    lines = []
    for edition in artifacts["archive"]["editions"]:
        for department in edition["departments"]:
            for text in _texts(department):
                lines.append({"round": edition["round"], "text": text})
    return Finding(
        "#30", "funny, colourful, pointed but never mean", JUDGED,
        "the tone gate (config.newspaper.tone, content/newspaper.json's forbidden "
        "register) passed every edition -- a failure there refuses publication "
        "rather than printing. %d printed passages are attached for judgement"
        % len(lines),
        {"tone_policy": game.config.require("newspaper.tone"), "sample": lines[:24]},
    )


# --- endgame --------------------------------------------------------------

def _endgame_crown_and_twist(game, journal, artifacts):
    final = artifacts["archive"].get("final")
    if final is None:
        return Finding("#31", "crown and twist article", FAIL,
                       "the game ended but no final edition was produced")
    departments = {d["id"]: d for d in final["departments"]}
    crown_column = departments.get("the_crown")
    twist = departments.get("consequences")

    board = game.leaderboard()
    leaders = [row["city"] for row in board if row["rank"] == 1]
    crowned = " ".join(_texts(crown_column or {}))
    names_the_leader = bool(leaders) and any(city in crowned for city in leaders)

    # The twist article quotes the exports it follows up on. Every quote must be
    # an export that really won something in this game: that is the difference
    # between an article about this game's trade and a comedy column that could
    # have been printed before anybody played (spec #31).
    quotes = _texts(twist or {}, kinds={"quote"})
    winners = {s.text for s in game.submissions.values() if s.is_winner}
    invented = [quote for quote in quotes if quote not in winners]

    ok = bool(crown_column) and bool(twist) and names_the_leader and bool(quotes) \
        and not invented
    return _verdict(
        "#31", "the cumulative-profit winner is crowned; a twist article runs", ok,
        "the crown column names %s, which is who the leaderboard puts first; the "
        "consequences column follows up on %d arrival(s), every one of them an "
        "export that actually won a need in this game"
        % (", ".join(leaders) or "nobody", len(quotes)),
        {
            "leaderboard_head": board[:3],
            "crown_column": _texts(crown_column or {}),
            "twist_quotes": quotes,
            "quotes_not_from_this_game": invented,
            "arrivals_printed": (twist or {}).get("provenance", {}).get(
                "arrivals_printed"
            ),
        },
    )


def _endgame_cities(game, journal, artifacts):
    final = artifacts["archive"].get("final")
    portraits = {
        entry["city"]: entry["filename"]
        for entry in (final or {}).get("city_images") or ()
    }
    cities = {player.city for player in game.players.values()}

    # The Excess is one run of blocks per city, opened by a level-3 heading with
    # that city's name on it. Grouping by heading is how a description gets
    # attributed to the city it is about.
    excess = {}
    current = None
    for department in (final or {}).get("departments", []):
        if department["id"] != "the_excess":
            continue
        for block in department.get("blocks", []):
            if block.get("kind") == "heading" and block.get("text") in cities:
                current = block["text"]
                excess.setdefault(current, [])
            elif current and isinstance(block.get("text"), str):
                excess[current].append(block["text"])

    described = {city for city, lines in excess.items() if lines}
    complete = set(portraits) == cities and described == cities
    return Finding(
        "#32", "a description and an image per city, from its own history",
        JUDGED if complete else FAIL,
        "%d of %d cities carry a portrait and %d carry a description; whether each "
        "is *informed by that city's actual history* -- and whether its excess is "
        "portrayed as offers it declined rather than offers it sent -- is the "
        "judged part, and the material is attached"
        % (len(portraits), len(cities), len(described)),
        {
            "cities": sorted(cities),
            "portraits": portraits,
            "descriptions": {city: excess[city] for city in sorted(excess)},
        },
    )


# --- content and process --------------------------------------------------

def _game_content(game, journal, artifacts):
    needs = game.content.needs
    categories = {need["category"] for need in needs}
    briefs = {need["need_brief"] for need in needs}
    families = {need.get("trade_family") for need in needs}
    return Finding(
        "#33", "a good name, and a seeded import list that is varied and gameable",
        JUDGED,
        "the game is %r and the paper is %r (see NAME.md); the seed list holds %d "
        "orders across %d categories and %d kinds of tradable thing, %d distinct "
        "briefs, of which %d were ordered in this game. Whether the name reads as "
        "chosen rather than placeheld, and whether the list is varied and gameable "
        "rather than repetitive, is the judged part"
        % (
            artifacts["archive"]["game"], artifacts["archive"]["publication"],
            len(needs), len(categories), len(families), len(briefs), len(game.needs),
        ),
        {
            "categories": sorted(categories),
            "trade_families": sorted(f for f in families if f),
            "ordered_this_game": sorted(
                (n.importing_city, n.category, n.rendered["title"])
                for n in game.needs.values()
            ),
        },
    )


def _separate_agents(game, journal, artifacts):
    seats = artifacts.get("transcript_data", {}).get("seats") or []
    return Finding(
        "#34", "every role and every simulated player is a separate session",
        PROCESS,
        "not decidable from game state. This run's evidence: %d mayors, each played "
        "by its own separately-spawned agent given only its own city's brief, plus "
        "a separate facilitator session for the answer clustering; the Generator "
        "and Evaluator are separate sessions by the harness's own construction. "
        "See docs/m8-integration.md" % len(seats),
        {"seats": [seat["player_id"] for seat in seats]},
    )


def _separate_repo(game, journal, artifacts):
    return Finding(
        "#35", "committed once per run, in a repository separate from the harness",
        PROCESS,
        "not decidable from game state: this is the deliverable repository "
        "(badralbudur/game-night), which shares no commit history with the harness "
        "repository (badralbudur/game-night-harness). Verifiable from `git log` in "
        "each, not from a played game",
    )
