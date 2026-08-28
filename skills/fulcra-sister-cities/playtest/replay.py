"""Playing a recorded game through the real engine, and writing down what it asked.

One function does the work. :func:`replay` seats the table, starts the one round
timer, and then walks rounds until the game ends -- registering mid-game
arrivals, offering every present mayor their check-in, and passing whatever the
check-in offers to a :class:`~playtest.transcript.Decisions` for an answer.

It touches no engine internals. Every action goes through the same public
methods a facilitator's agent would use (``join``, ``checkin``,
``submit_export``, ``pick_winner``, ``answer_question``,
``record_answer_buckets``) and the game only ever moves by advancing the clock
and letting :meth:`~engine.GameEngine.tick` notice -- which is the point of an
integration pass. A driver that reached past the front door would prove the
engine's internals work and nothing about whether the game does.

The journal
-----------
:class:`Journal` records every check-in exactly as it was offered. That serves
two masters. It is the **briefing** the mayors' agents are written from -- a
mayor can only write an export if they know what was asked for -- and it is the
**evidence** :mod:`playtest.conformance` checks spec #11, #16, #18 and #23
against afterwards. Both want the same thing: a faithful record of what the
game put in front of each player, and what they did about it.
"""

from engine import Config, Content, GameEngine
from engine.clock import ManualClock
from engine.game import (
    ENDED,
    RUNNING,
    SLOT_EXPORT,
    SLOT_IMPORT_PICK,
    SLOT_QUESTION,
)

from .table import SEATS, SEED, START
from .transcript import StandIns

#: A game of seven mayors over two rotations runs to the high teens. The cap is
#: a runaway guard, not a length: a replay that hits it has a bug, and
#: :func:`replay` says so rather than returning a half-played game.
ROUND_LIMIT = 60


class Journal:
    """What the game asked, and what each mayor did about it."""

    def __init__(self):
        self.joins = []
        self.rounds = {}
        self.actions = []

    def round(self, index):
        return self.rounds.setdefault(
            index,
            {"round": index, "question": None, "checkins": {}, "absent": [],
             "unregistered": [], "clustered": None},
        )

    def join(self, round_index, seat, record):
        self.joins.append(dict(record, round=round_index, engagement=seat.engagement))

    def checkin(self, round_index, seat, payload):
        self.round(round_index)["checkins"][seat.player_id] = {
            "city": payload["city"],
            "engagement": seat.engagement,
            "slots": [slot for slot in payload["slots"] if slot],
            "pending_game_actions": payload["pending_game_actions"],
            "deadline": payload["deadline"],
            "did": [],
        }

    def absent(self, round_index, seat, registered):
        entry = self.round(round_index)
        (entry["absent"] if registered else entry["unregistered"]).append(seat.player_id)

    def acted(self, round_index, player_id, kind, detail):
        self.round(round_index)["checkins"][player_id]["did"].append(
            dict(detail, kind=kind)
        )
        self.actions.append(dict(detail, kind=kind, round=round_index, player=player_id))

    def question(self, round_index, question):
        self.round(round_index)["question"] = question

    def clustered(self, round_index, buckets):
        self.round(round_index)["clustered"] = buckets

    def slots_offered(self, round_index, player_id, kind):
        entry = self.rounds.get(round_index, {}).get("checkins", {}).get(player_id)
        return [slot for slot in (entry or {}).get("slots", []) if slot["kind"] == kind]

    def to_dict(self):
        return {
            "joins": self.joins,
            "rounds": {index: self.rounds[index] for index in sorted(self.rounds)},
            "actions": self.actions,
        }


def replay(decisions=None, config=None, content=None, seats=SEATS, seed=SEED,
           start=START, limit=ROUND_LIMIT):
    """Play the table's game to its end. Returns ``(game, journal)``.

    With no ``decisions``, stand-ins play it: everybody present acts, nobody
    says anything interesting, and the result is the round-by-round schedule the
    real mayors are briefed from. With a
    :class:`~playtest.transcript.Transcript`, the real game replays.
    """
    decisions = decisions if decisions is not None else StandIns()
    config = config if config is not None else Config.load()
    content = content if content is not None else Content.load(config)
    game = GameEngine(
        config=config, content=content, clock=ManualClock(start), rng_seed=seed
    )
    journal = Journal()

    for seat in seats:
        if seat.joins_round == 0:
            journal.join(0, seat, _seat(game, seat))
    game.start()

    played = 0
    while game.phase == RUNNING and played < limit:
        index = game.current_round
        for seat in seats:
            if seat.joins_round == index:
                journal.join(index, seat, _seat(game, seat))

        record = game.rounds[index]
        if record.question_id is not None:
            journal.question(index, dict(content.question_by_id(record.question_id)))

        for seat in seats:
            if seat.player_id not in game.players:
                journal.absent(index, seat, registered=False)
            elif not seat.is_present(index):
                # Spec #16: no penalty, no substitution, no announcement. The
                # journal records it because the *conformance* pass has to be
                # able to tell a silent skip from a bug.
                journal.absent(index, seat, registered=True)
            else:
                _check_in(game, seat, index, decisions, journal)

        _cluster(game, index, decisions, journal)
        game.clock.advance(game.timer.window)
        game.tick()
        played += 1

    if game.phase != ENDED:
        raise RuntimeError(
            "the game was still %s after %d rounds; the table's seating plan and the "
            "rotation target no longer agree" % (game.phase, played)
        )
    return game, journal


def _seat(game, seat):
    """Seat one mayor, letting the engine resolve a duplicate pick (spec #2)."""
    return game.join(
        seat.player_id, seat.handle, seat.requested_city, seat.is_facilitator
    )


def _check_in(game, seat, index, decisions, journal):
    """One mayor's one check-in for this round (spec #11, #23).

    The slots are read once, up front, and then acted on. That is not an
    optimisation -- it is the check-in. Spec #23's two slots are fixed when the
    round opens, and a driver that re-asked after every action would be playing
    a different game from the one the engine describes (see
    ``GameEngine.checkin``'s note on what "pending" means).
    """
    payload = game.checkin(seat.player_id)
    journal.checkin(index, seat, payload)

    for slot in payload["slots"]:
        if slot is None:
            continue
        if slot["kind"] == SLOT_IMPORT_PICK:
            _pick(game, seat, index, slot, decisions, journal)
        elif slot["kind"] == SLOT_EXPORT:
            _export(game, seat, index, slot, decisions, journal)
        elif slot["kind"] == SLOT_QUESTION:
            _answer(game, seat, index, slot, decisions, journal)


def _pick(game, seat, index, slot, decisions, journal):
    pick = decisions.pick_for(seat.player_id, slot["need"], slot["ballot"])
    if pick is None:
        # Spec #19's path: present, offered a ballot, declined to use it.
        return
    applied = game.pick_winner(seat.player_id, pick["ballot_ref"], slot["need"])
    journal.acted(index, seat.player_id, SLOT_IMPORT_PICK, {
        "need": slot["need"],
        # The ref, never a city: a journal is written down and spec #21 outlives
        # the round. Which ref won is public the moment the need resolves.
        "ballot_ref": applied["ballot_ref"],
        "because": pick.get("because"),
    })


def _export(game, seat, index, slot, decisions, journal):
    text = decisions.export_for(seat.player_id, index, slot["need"])
    if not text:
        return
    submission = game.submit_export(seat.player_id, text, slot["need"])
    journal.acted(index, seat.player_id, SLOT_EXPORT, {
        "need": slot["need"],
        "importing_city": slot["importing_city"],
        "submission": submission.submission_id,
    })


def _answer(game, seat, index, slot, decisions, journal):
    answer = decisions.answer_for(seat.player_id, index, slot["question_id"])
    if not answer:
        # A mayor who skips leaves the denominator rather than counting as a
        # null answer -- see the question slot's own note.
        return
    game.answer_question(seat.player_id, answer)
    journal.acted(index, seat.player_id, SLOT_QUESTION, {
        "question_id": slot["question_id"],
        "answer": answer,
    })


def _cluster(game, index, decisions, journal):
    """Apply the facilitator's grouping of this round's answers (spec #25).

    The engine will not group freeform answers itself and this driver will not
    either. It asks the transcript, which holds a grouping made by the
    facilitator's own agent, and passes it through the engine's validation --
    which refuses a clustering that drops a respondent or invents one.
    """
    record = game.rounds[index]
    if record.question_id is None or not record.answers:
        return
    answers = game.answers_by_city(index)
    buckets = decisions.clustering_for(index, answers)
    if not buckets or len(buckets) != len(answers):
        return
    journal.clustered(index, game.record_answer_buckets(index, buckets))
