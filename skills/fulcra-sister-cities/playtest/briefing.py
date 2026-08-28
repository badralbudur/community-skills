"""Turning a played game into one brief per mayor.

A mayor's agent cannot invent an export out of nothing: it has to know which
city asked for what, in which round, and what the mayor question was that day.
This module takes the journal :func:`playtest.replay.replay` produces and cuts
it into one pack per seat -- and cuts it *narrowly*, because a brief is a
player-facing document and spec #18 and #21 apply to it exactly as they apply to
a ballot.

What a brief contains, therefore:

* the mayor's own city, persona and engagement level;
* every round they are present for, and what their check-in offers them --
  their own import notices, the export prompts they may answer, the questions
  they may reply to;
* nothing whatsoever about anybody else's offers. Not the text, not the count,
  not who else showed up.

The pick round is deliberately *not* briefed here. An importing mayor's ballot
does not exist until the export window closes, and it cannot be assembled from
guesses about what the others will write. So picks are collected in a second
pass, from the real ballots of the real replayed game -- see
``docs/m8-integration.md``.
"""

from engine.game import SLOT_EXPORT, SLOT_QUESTION

from .table import SEATS


def briefing_packs(game, journal, seats=SEATS):
    """One brief per seat, keyed by player id."""
    return {seat.player_id: brief_for(game, journal, seat) for seat in seats}


def brief_for(game, journal, seat):
    player = game.players[seat.player_id]
    rounds = []
    for index in sorted(journal.rounds):
        entry = journal.rounds[index]
        checkin = entry["checkins"].get(seat.player_id)
        if checkin is None:
            continue
        asks = []
        for slot in checkin["slots"]:
            if slot["kind"] == SLOT_EXPORT:
                asks.append({
                    "asked_for": "export",
                    "importing_city": slot["importing_city"],
                    "importing_mayor": slot["importing_mayor"],
                    "the_notice": slot["need_brief"],
                    "what_they_want": slot["exporter_prompt"],
                })
            elif slot["kind"] == SLOT_QUESTION:
                asks.append({
                    "asked_for": "answer",
                    "question_id": slot["question_id"],
                    "question": slot["text"],
                    "framing": slot["framing"],
                    "answer_shape": slot.get("answer_shape"),
                })
        if asks:
            rounds.append({"round": index, "asks": asks})

    return {
        "player_id": seat.player_id,
        "city": player.city,
        "mayor": player.mayor,
        "requested_city": seat.requested_city,
        "reassigned": player.city != seat.requested_city,
        "persona": seat.persona,
        "engagement": seat.engagement,
        "engagement_note": seat.engagement_note,
        "joined_round": player.joined_round,
        "away_rounds": sorted(seat.away),
        "rounds": rounds,
        "privacy": (
            "You never learn what any other city offered, and nobody ever learns "
            "what you offered unless it wins (spec #18, #21)."
        ),
    }


def ballot_packs(game, journal, seats=SEATS):
    """One pack per seat of the blind ballots that mayor was actually shown.

    Assembled from the journal's own record of the check-in, which is what the
    engine handed the player at the time -- refs and export text, no cities. It
    is not re-derived from the needs, because re-deriving it is precisely where
    a city would get attached to a losing offer by accident.
    """
    packs = {}
    for seat in seats:
        ballots = []
        for index in sorted(journal.rounds):
            for slot in journal.slots_offered(index, seat.player_id, "import_pick"):
                need = game.needs[slot["need"]]
                ballots.append({
                    "round": index,
                    "need": slot["need"],
                    "your_notice": slot["need_brief"],
                    "you_asked_for": need.rendered["exporter_prompt"],
                    "offers": [
                        {"ballot_ref": entry["ballot_ref"], "offer": entry["export"]}
                        for entry in slot["ballot"]
                    ],
                })
        if ballots:
            packs[seat.player_id] = {
                "player_id": seat.player_id,
                "city": game.players[seat.player_id].city,
                "ballots": ballots,
            }
    return packs


def question_packs(game, journal):
    """Every round's question and the answers it drew, keyed by city (spec #28).

    The facilitator's material for spec #25's clustering, and the only brief in
    this module that is *supposed* to show one mayor what another said -- answers
    are shared by default and the whole point of an aggregate is that somebody
    looked at all of them at once. It is handed to the facilitator's own agent,
    never to a player's, and it carries cities rather than handles.
    """
    packs = []
    for index in sorted(journal.rounds):
        entry = journal.rounds[index]
        if entry["question"] is None:
            continue
        answers = game.answers_by_city(index)
        if not answers:
            continue
        packs.append({
            "round": index,
            "question_id": entry["question"]["id"],
            "question": entry["question"]["text"],
            "answers": answers,
        })
    return packs
