"""The Sister Cities round-flow engine.

Implements spec #4, #5, #9-#12, #14-#19, #21 and #23-#25. Newspaper prose,
generated images and the *wording* of the mayor-question items belong to later
milestones; where this engine would need them it emits a clearly marked stub
instead (see ``resolution["newspaper"]``).

The lockstep (spec #9)
----------------------
The game has one timer. Every round performs exactly three operations, in this
order, and nothing else:

    OPEN     a new import need for the next city in the queue
    CLOSE    the export window of the need opened in the previous round
    RESOLVE  the winner of the need opened two rounds ago

which gives each need this life:

    round r      OPEN     -> collecting   (its export window *is* round r)
    round r + 1  CLOSE    -> picking      (the importer's own full window, #18)
    round r + 2  RESOLVE  -> resolved     (pick applied, or a fallback fires)

Exports for a need are collected during the round it opens; the importing mayor
picks during the following round, which is what spec #18 means by "the round
AFTER exports were collected". Because the last need still needs its collecting
and picking rounds, the final two rounds of a game OPEN nothing -- they close and
resolve the tail. Those two drain rounds are logged with the same three ops (with
``need: null`` on OPEN), so the lockstep invariant holds for every round of the
game without exception.
"""

import random
from datetime import timedelta

from . import aggregate, ballot
from .aggregate import Ladder
from .clock import ManualClock, RoundTimer, SystemClock, ensure_aware
from .config import Config
from .content import Content, normalize_city
from .economy import Economy
from .errors import (
    CheckInExhausted,
    ConfigError,
    DuplicateCity,
    PhaseError,
    PickRejected,
    RosterError,
    RuleViolation,
    SubmissionRejected,
)
from .join import CityRegistrar, join_player
from .rotation import CityQueue
from .state import (
    COLLECTING,
    EVEN_SPLIT,
    PICKING,
    RAMP_UP,
    READ_AWARD,
    RESOLVED,
    WINNER_PICK,
    ExporterLedger,
    ImportNeed,
    Player,
    RoundRecord,
    Submission,
)

_UNSET = object()

LOBBY = "lobby"
RUNNING = "running"
ENDED = "ended"

OP_OPEN = "OPEN"
OP_CLOSE = "CLOSE"
OP_RESOLVE = "RESOLVE"
#: The three lockstep operations, in the order spec #9 lists them. Every round
#: logs exactly this sequence.
LOCKSTEP_OPS = (OP_OPEN, OP_CLOSE, OP_RESOLVE)

SLOT_IMPORT_PICK = "import_pick"
SLOT_EXPORT = "export"
SLOT_QUESTION = "mayor_question"


class GameEngine:
    """One game of Sister Cities."""

    def __init__(self, config=None, content=None, clock=None, rng_seed=_UNSET):
        self.config = config if config is not None else Config.load()
        self.content = content if content is not None else Content.load(self.config)
        # Built here, before a round exists, so a malformed dice expression or
        # split mode is a startup error rather than a round-3 crash (spec #20).
        self.economy = Economy(self.config)
        # Same reason: the phrasing ladder spec #25's aggregate is selected from
        # is content, and a malformed one must not surface as a broken newspaper
        # item three rounds in. Building it here also means every game validates
        # its question bank, however its Content was constructed.
        self.content.check_question_policy(self.config)
        self.phrasing_ladder = Ladder.from_config(self.config, self.content)
        # And again: an unknown duplicate-pick resolution mode is a startup
        # error, not something the fifth player to join finds out about.
        self.registrar = CityRegistrar(self.config, self.content)
        self.clock = clock if clock is not None else SystemClock()
        self._seed = (
            self.config.require_nullable_int("engine.rng_seed")
            if rng_seed is _UNSET
            else rng_seed
        )

        self.players = {}
        self.needs = {}
        self.submissions = {}
        self.ledger = ExporterLedger()
        self.rounds = {}

        self.phase = LOBBY
        self.current_round = 0
        self.ended_round = None
        self.timer = None
        self.queue = CityQueue(self.config.require_int("rounds.rotations_target"))

        self._city_keys = {}
        self._checkin_used = {}
        self._asked_question_ids = []
        self._need_counter = 0
        self._submission_counter = 0

    # -- construction helpers ---------------------------------------------

    @classmethod
    def for_test(cls, start_at, rng_seed=1, config=None, content=None):
        """A game on a hand-advanced clock. Used by the test suite."""
        config = config if config is not None else Config.load()
        content = content if content is not None else Content.load(config)
        return cls(config=config, content=content, clock=ManualClock(start_at), rng_seed=rng_seed)

    def _rng(self, purpose, key=""):
        """A stream per (purpose, key), so adding a draw elsewhere cannot shift
        an unrelated one -- a game replayed from the same seed stays identical."""
        if self._seed is None:
            return random.Random()
        return random.Random("%s|%s|%s" % (self._seed, purpose, key))

    # -- roster -----------------------------------------------------------

    def register_player(self, player_id, handle, city, is_facilitator=False):
        """Seat a player. Legal in the lobby and mid-game alike (spec #3)."""
        if self.phase == ENDED:
            raise RosterError("the game is over")
        if player_id in self.players:
            raise RosterError("player %r is already registered" % player_id)

        max_players = self.config.require_int("players.max_players")
        if len(self.players) >= max_players:
            raise RosterError(
                "this game is full at %d players (config.players.max_players)" % max_players
            )

        key = normalize_city(city)
        if self.config.require_bool("cities.enforce_unique_city_names") and key in self._city_keys:
            # Refused, never silently allowed to collide (spec #2). Resolving the
            # collision is the join/city-assignment milestone's job; the
            # candidates it needs travel with the error.
            raise DuplicateCity(city, key, self._city_keys[key], self.content.nearby_names(city))

        if is_facilitator:
            if self.facilitator is not None:
                raise RosterError(
                    "the facilitator is fixed for the whole game and does not rotate (spec #6)"
                )
            if self.phase != LOBBY:
                raise RosterError(
                    "the facilitator must be seated before the game starts -- their city holds "
                    "queue position 1 so round 1 has an import need to open (spec #4)"
                )

        player = Player(player_id, handle, city, is_facilitator, joined_round=self.current_round)
        self.players[player_id] = player
        self._city_keys[key] = player_id

        if is_facilitator:
            # Spec #4: position 1, and queued on arrival rather than on first
            # export -- that exemption is the whole point of the rule.
            self.queue.seat_facilitator(player_id)
            player.queued_round = self.current_round
            player.import_turns_allotted = self.queue.allotment_for_new_entrant()
        return player

    def join(self, player_id, handle, city, is_facilitator=False, rng=None):
        """Seat a player and resolve a duplicate city pick (spec #2).

        The door a facilitator's agent uses. :meth:`register_player` is the
        lower level: it refuses a collision and hands back the candidates, which
        is the right behaviour for something that must never seat two mayors on
        one city by accident, and the wrong behaviour for the only caller a
        joining player ever sees. See :mod:`engine.join`.
        """
        return join_player(self, player_id, handle, city, is_facilitator, rng=rng)

    def city_suggestions(self, rng=None):
        """Cities to offer a joining player, minus the ones already taken (#2)."""
        return {
            "cities": self.registrar.suggestions(self.claimed_cities(), rng=rng),
            "note": self.registrar.suggestion_note(),
            "spec": "#2",
        }

    def claimed_cities(self):
        """Every city this game currently holds, as the mayors spell them."""
        return [player.city for player in self.players.values()]

    @property
    def facilitator(self):
        for player in self.players.values():
            if player.is_facilitator:
                return player
        return None

    def player_for_city(self, city):
        player_id = self._city_keys.get(normalize_city(city))
        return self.players.get(player_id) if player_id else None

    def _player(self, player_id):
        try:
            return self.players[player_id]
        except KeyError:
            raise RosterError("unknown player %r" % player_id)

    # -- lifecycle --------------------------------------------------------

    def start(self, at=None):
        """Begin round 1."""
        if self.phase != LOBBY:
            raise PhaseError("this game has already started")
        min_players = self.config.require_int("players.min_players")
        if len(self.players) < min_players:
            raise RosterError(
                "need at least %d players to start (config.players.min_players), have %d"
                % (min_players, len(self.players))
            )
        if self.facilitator is None:
            raise RosterError(
                "a facilitator must be seated before the game starts (spec #4, #6)"
            )
        epoch = ensure_aware(at if at is not None else self.clock.now())
        window = timedelta(hours=self.config.require_number("rounds.round_window_hours"))
        self.timer = RoundTimer(epoch, window)
        self.phase = RUNNING
        return self._begin_round(1)

    def timers(self):
        """Every timer in the game. Spec #9 allows exactly one; this proves it."""
        return {"round": self.timer.describe()} if self.timer is not None else {}

    def advance_round(self):
        """Run the lockstep once. The only way rounds move."""
        if self.phase != RUNNING:
            raise PhaseError("cannot advance a game that is %s" % self.phase)
        return self._begin_round(self.current_round + 1)

    def tick(self, now=None):
        """Advance as many rounds as the one round timer says have elapsed."""
        if self.phase != RUNNING:
            return []
        now = ensure_aware(now if now is not None else self.clock.now())
        advanced = []
        while self.phase == RUNNING and self.timer.round_index_at(now) > self.current_round:
            advanced.append(self._begin_round(self.current_round + 1))
        return advanced

    def _begin_round(self, index):
        # The previous round is over the instant this one starts, so this is where
        # its closing standing is fixed -- after every check-in it was going to
        # get, including a mayor who joined part-way through it.
        self._close_standings(index - 1)
        record = RoundRecord(index, self.timer.round_start(index), self.timer.round_end(index))
        self.rounds[index] = record
        self.current_round = index

        # Spec #9's three operations, in spec #9's order. Nothing else belongs
        # in this block -- the lockstep tests read `record.ops` literally.
        self._op_open(record)
        self._op_close(record)
        self._op_resolve(record)

        self._select_question(record)
        if self._game_is_over():
            self.phase = ENDED
            self.ended_round = index
            # No round follows this one, so nothing else will close its standing.
            self._close_standings(index)
        return record

    def _close_standings(self, index):
        """Freeze one round's cumulative leaderboard, once (see RoundRecord).

        An edition is a historical document: the paper for round 3 must go on
        saying what round 3's table was, whatever happens afterwards (spec #26,
        #27). Without this, an archive of twelve editions prints the final table
        twelve times.
        """
        record = self.rounds.get(index)
        if record is not None and record.standings is None:
            record.standings = self.leaderboard()

    def _game_is_over(self):
        if not self.needs:
            return False
        return self.queue.exhausted and all(
            need.status == RESOLVED for need in self.needs.values()
        )

    # -- lockstep operations ----------------------------------------------

    def _op_open(self, record):
        """One new import need opens (spec #9)."""
        importer_id = self.queue.next_importer(self.players, record.index)
        if importer_id is None:
            record.log(
                OP_OPEN,
                need=None,
                reason="no import turns remain; draining the last needs",
            )
            return None

        player = self.players[importer_id]
        need_doc = self.content.draw_need(
            self._rng("need", "%s|%d" % (player.city, record.index)),
            player.city,
            used_need_ids={n.content_need_id for n in self.needs.values()},
            categories_used_by_city={
                n.category for n in self.needs.values() if n.importing_city == player.city
            },
            categories_used_anywhere={n.category for n in self.needs.values()},
            allow_repeat_for_same_city=self.config.require_bool(
                "imports.allow_repeat_category_for_same_city"
            ),
            allow_repeat_across_cities=self.config.require_bool(
                "imports.allow_repeat_category_across_cities"
            ),
            allow_need_reuse=self.config.require_bool("imports.reuse_same_need_within_game"),
        )

        self._need_counter += 1
        need = ImportNeed(
            need_key="in-%03d" % self._need_counter,
            content_need_id=need_doc["id"],
            category=need_doc["category"],
            importing_player_id=importer_id,
            importing_city=player.city,
            rendered=self.content.render_need(need_doc, player.city),
            opened_round=record.index,
            rotation=self.queue.rotation,
        )
        self.needs[need.need_key] = need
        player.import_turns_served += 1
        record.log(
            OP_OPEN,
            need=need.need_key,
            city=player.city,
            category=need.category,
            rotation=need.rotation,
        )
        return need

    def _op_close(self, record):
        """One export-collection window closes (spec #9)."""
        need = self._need_opened_in(record.index - 1)
        if need is None or need.status != COLLECTING:
            record.log(OP_CLOSE, need=None)
            return None
        submissions = self.submissions_for(need.need_key)
        # Refs are assigned here, at close, and in shuffled order -- so nothing
        # about ballot position can be read back as submission order (spec #18).
        ballot.assign_refs(self._rng("ballot", need.need_key), submissions)
        need.status = PICKING
        need.closed_round = record.index
        record.log(
            OP_CLOSE, need=need.need_key, submissions=len(submissions), city=need.importing_city
        )
        return need

    def _op_resolve(self, record):
        """One earlier round's winner gets picked (spec #9)."""
        need = self._need_opened_in(record.index - 2)
        if need is None or need.status != PICKING:
            record.log(OP_RESOLVE, need=None)
            return None
        resolution = self._resolve(need, record.index)
        record.log(
            OP_RESOLVE,
            need=need.need_key,
            mode=resolution["mode"],
            city=need.importing_city,
        )
        return need

    def _resolve(self, need, round_index):
        submissions = self.submissions_for(need.need_key)
        economy = self.economy
        rng = self._rng("profit", need.need_key)

        if not submissions:
            # Spec #17: nobody exported, so the importing city ramps up its own
            # industry and the importing mayor still takes the rolled profit.
            roll = economy.roll(rng)
            awards = economy.whole(need.importing_city, roll)
            mode = RAMP_UP
            winners = []
        elif need.pick is not None:
            # Spec #18/#20: the importer chose; the winning city takes the roll.
            roll = economy.roll(rng)
            winner = ballot.resolve_ref(submissions, need.pick["ballot_ref"])
            winner.is_winner = True
            winners = [winner]
            awards = economy.whole(
                self.ledger.city_for(winner.submission_id, READ_AWARD), roll
            )
            mode = WINNER_PICK
        else:
            # Spec #19: the picking window lapsed, so every submission wins and
            # the profit is split evenly among their cities.
            roll = economy.roll(rng)
            for submission in submissions:
                submission.is_winner = True
            winners = list(submissions)
            # Among their *cities*, not among their submissions. The two differ
            # only when config raises the #15 cap above one submission per
            # player -- and then splitting per submission would pay a city that
            # submitted twice a double share, making export spam profitable.
            # That is the incentive the cap exists to remove, so the split is
            # per distinct city, in first-submission order.
            cities = []
            for submission in winners:
                city = self.ledger.city_for(submission.submission_id, READ_AWARD)
                if city not in cities:
                    cities.append(city)
            awards = economy.split(cities, roll)
            mode = EVEN_SPLIT

        for city, amount in awards:
            earner = self.player_for_city(city)
            if earner is None:  # pragma: no cover - cities are registered players
                raise RuleViolation("cannot credit profit to unknown city %r" % city)
            economy.credit(earner, amount)

        need.status = RESOLVED
        need.resolved_round = round_index
        need.resolution = {
            "mode": mode,
            "roll": roll.to_dict(),
            "awards": economy.render_awards(awards),
            "submission_count": len(submissions),
            "winning_ballot_refs": [s.ballot_ref for s in winners],
            "spec": {
                WINNER_PICK: "#18, #20",
                RAMP_UP: "#17",
                EVEN_SPLIT: "#19",
            }[mode],
            # Prose, headline and image belong to the :mod:`newspaper` package;
            # the engine states the fact and the framing it needs, and stops.
            "newspaper": {
                "framing_hint": {
                    WINNER_PICK: "winner_chosen_by_importing_mayor",
                    RAMP_UP: "import_city_ramped_up_its_own_industry",
                    EVEN_SPLIT: "no_pick_by_deadline_every_submission_wins",
                }[mode],
                "written_by": "newspaper.departments.arrivals",
            },
        }
        return need.resolution

    # -- the round's mayor question ----------------------------------------

    def _select_question(self, record):
        """Draw this round's getting-to-know-you question (spec #23, #24).

        One question per round, asked of every mayor who checks in -- not a
        different question each. That is what makes spec #25's aggregate phrasing
        ("the world", "some countries") mean anything: it can only describe a
        distribution if everyone was asked the same thing.

        This is not a fourth lockstep operation and carries no timer of its own;
        it is bookkeeping attached to the round the check-ins belong to.
        """
        if not self.config.require_bool("facilitator_questions.enabled"):
            return None
        cadence = self.config.require_int("facilitator_questions.ask_every_n_rounds")
        if cadence < 1:
            raise ConfigError(
                "facilitator_questions.ask_every_n_rounds must be at least 1 (use "
                "enabled: false to stop asking), got %d" % cadence
            )
        if (record.index - 1) % cadence != 0:
            return None
        question = self.content.draw_question(
            self._rng("question", str(record.index)), self._asked_question_ids
        )
        if question is None:
            # The bank ran dry. Silence is the right failure here: a repeated
            # question would corrupt the aggregate it feeds.
            return None
        record.question_id = question["id"]
        self._asked_question_ids.append(question["id"])
        return question

    def asked_question_ids(self):
        return list(self._asked_question_ids)

    def _round_record(self, round_index):
        try:
            return self.rounds[round_index]
        except KeyError:
            raise RuleViolation("round %r has not happened" % (round_index,))

    def answers_by_city(self, round_index):
        """A round's answers keyed by city -- never by handle (spec #28).

        The city is the identity the newspaper and the aggregate both use; the
        player id and handle exist only so the facilitator's agent can route a
        check-in, and neither leaves the engine through this door.
        """
        record = self._round_record(round_index)
        return {
            self.players[player_id].city: answer
            for player_id, answer in record.answers.items()
        }

    def mayors_asked(self, round_index):
        """How many mayors this round's question was put to.

        Every mayor seated by that round. Deliberately the wider count: it is the
        denominator for the integrity rule that says an aggregate over some of
        the mayors must admit as much ("of the nine who replied..."), so counting
        a mayor whose two game actions crowded the question out errs toward
        disclosure rather than away from it.
        """
        record = self._round_record(round_index)
        return sum(1 for p in self.players.values() if p.joined_round <= record.index)

    def record_answer_buckets(self, round_index, buckets_by_city, source="facilitator"):
        """Cluster a round's answers, as ``{city: bucket label}`` (spec #25).

        The engine cannot do this itself and does not pretend to: deciding that
        "the fish counter" and "the market" are the same answer is a judgement.
        What it *can* do is refuse a clustering that would corrupt the aggregate
        -- one that drops a respondent or invents one -- and then do the
        arithmetic exactly, which is what
        :meth:`mayor_question_report` returns.

        Re-clustering is allowed while the game runs; whether an already
        published edition may be revised is the newspaper's rule (see the
        questions file's asking_rules on late answers), not this method's.
        """
        record = self._round_record(round_index)
        if record.question_id is None:
            raise RuleViolation("no mayor question was asked in round %d" % round_index)
        answers = self.answers_by_city(round_index)
        if not answers:
            raise RuleViolation(
                "nobody answered round %d's question, so there is nothing to cluster"
                % round_index
            )
        record.answer_buckets = aggregate.validate_bucketing(
            answers, self._resolve_bucket_cities(buckets_by_city)
        )
        record.bucket_source = source
        return dict(record.answer_buckets)

    def _resolve_bucket_cities(self, buckets_by_city):
        """Key a supplied clustering by the city names the game actually holds.

        "Reykjavik" and "Reykjavík" are the same city everywhere else in this
        engine (see :func:`engine.content.normalize_city`), so a clustering that
        spells one of them differently is accepted rather than reported as a
        mayor who both failed to answer and answered twice. Two spellings of the
        *same* city are refused, though -- collapsing them silently would drop
        one of the labels and change the distribution.
        """
        if not isinstance(buckets_by_city, dict):
            return buckets_by_city  # validate_bucketing owns this complaint
        resolved = {}
        for city, label in buckets_by_city.items():
            key = city
            if isinstance(city, str) and city.strip():
                player = self.player_for_city(city)
                if player is not None:
                    key = player.city
            if key in resolved:
                raise RuleViolation(
                    "%r and another key both name %s; a city gets one bucket" % (city, key)
                )
            resolved[key] = label
        return resolved

    def mayor_question_report(self, round_index):
        """One round's question, its answers, and what they add up to (spec #25).

        The facilitator's own view: always complete, whatever
        ``facilitator_questions.answers_shared_in_newspaper`` says. The gated,
        newspaper-facing version is
        :func:`engine.views.newspaper_mayor_question`, and that one function is
        where the exposure decision is taken.

        Returns ``None`` when the round asked no question at all.
        """
        record = self._round_record(round_index)
        if record.question_id is None:
            return None
        return aggregate.summarize(
            self.phrasing_ladder,
            round_index,
            self.content.question_by_id(record.question_id),
            self.answers_by_city(round_index),
            record.answer_buckets,
            self.mayors_asked(round_index),
            bucket_source=record.bucket_source,
        )

    # -- lookups ----------------------------------------------------------

    def _need_opened_in(self, round_index):
        if round_index < 1:
            return None
        for need in self.needs.values():
            if need.opened_round == round_index:
                return need
        return None

    def collecting_need(self):
        """The need whose export window is open right now (at most one, #9)."""
        for need in self.needs.values():
            if need.status == COLLECTING:
                return need
        return None

    def picking_need_for(self, player_id):
        """The need this player must pick a winner for this round, if any."""
        for need in self.needs.values():
            if need.status == PICKING and need.importing_player_id == player_id:
                return need
        return None

    def submissions_for(self, need_key):
        return [s for s in self.submissions.values() if s.need_id == need_key]

    # -- check-in ---------------------------------------------------------

    def checkin(self, player_id):
        """This player's one check-in for the current round (spec #11, #23).

        Two slots. Slot 1 is a pending game action if one exists. Slot 2 is a
        second pending game action if one exists, and otherwise a
        getting-to-know-you question for the mayor -- which is exactly spec #23's
        "if a second game action isn't pending, a question fills that slot".

        "Pending" means pending *for the round*, not "still undone right now".
        The distinction matters: if it meant the latter, a mayor who submitted
        their export first would then be offered a question, and answering it
        would eat the slot their still-outstanding winner pick needed. The set of
        slots a round offers is fixed when the round opens; ``slots`` below just
        omits the ones already filled.
        """
        player = self._player(player_id)
        if self.phase != RUNNING:
            raise PhaseError("no check-ins while the game is %s" % self.phase)
        used = self._checkin_used.get((player_id, self.current_round), {})
        deadline = self.rounds[self.current_round].ends_at

        # Which game actions this round asks of this player, regardless of what
        # they have already done. Pick first: its window closes this round and
        # letting it lapse triggers spec #19's even split.
        applicable = []
        pick_need = self.picking_need_for(player_id)
        if pick_need is not None and self.submissions_for(pick_need.need_key):
            applicable.append((SLOT_IMPORT_PICK, pick_need))
        open_need = self.collecting_need()
        if open_need is not None and self._export_slot_applies(player, open_need):
            applicable.append((SLOT_EXPORT, open_need))

        outstanding = [
            self._game_action_slot(kind, need, deadline)
            for kind, need in applicable
            if used.get(kind, 0) < self._slot_allowance(kind)
        ]
        slots = [
            outstanding[0] if outstanding else None,
            outstanding[1] if len(outstanding) > 1 else None,
        ]

        gate = self.config.require_bool(
            "facilitator_questions.fill_second_slot_only_if_no_second_game_action_pending"
        )
        second_game_action_pending = len(applicable) > 1
        if not (gate and second_game_action_pending):
            question_slot = self._question_slot(player_id, used, deadline)
            if question_slot is not None:
                if slots[1] is None:
                    slots[1] = question_slot
                else:
                    # Only reachable with the gate switched off in config, which
                    # is what switching it off means.
                    slots.append(question_slot)

        return {
            "round": self.current_round,
            "player_id": player_id,
            "city": player.city,
            "mayor": player.mayor,
            "queued": player.is_queued,
            "deadline": deadline.isoformat(),
            "slots": slots,
            "pending_game_actions": len(applicable),
            "already_used": dict(used),
        }

    def _slot_allowance(self, kind):
        """How many times a slot kind may be used in one round.

        One for a pick and one for a question; for exports it is the configured
        per-need cap, so raising ``max_submissions_per_player_per_import_per_round``
        actually raises it rather than being silently overruled by a
        one-use-per-slot assumption baked into the check-in.
        """
        if kind == SLOT_EXPORT:
            return self.config.require_int(
                "exports.max_submissions_per_player_per_import_per_round"
            )
        return 1

    def _game_action_slot(self, kind, need, deadline):
        if kind == SLOT_IMPORT_PICK:
            return {
                "kind": SLOT_IMPORT_PICK,
                "need": need.need_key,
                "need_brief": need.rendered["need_brief"],
                "ballot": ballot.build(self.submissions_for(need.need_key)),
                "deadline": deadline.isoformat(),
                "note": "Pick by ballot ref. Which city sent which export is not on "
                        "this ballot and will not be revealed for the ones you "
                        "don't pick (spec #18, #21).",
            }
        return {
            "kind": SLOT_EXPORT,
            "need": need.need_key,
            "importing_city": need.importing_city,
            "importing_mayor": self.players[need.importing_player_id].mayor,
            "need_brief": need.rendered["need_brief"],
            "exporter_prompt": need.rendered["exporter_prompt"],
            "deadline": deadline.isoformat(),
        }

    def _export_slot_applies(self, player, need):
        """Whether this round asks this player for an export at all.

        Deliberately independent of the per-round submission cap: the cap says
        whether the slot is *already filled*, not whether it exists.
        """
        if need.importing_player_id == player.player_id:
            return self.config.require_bool("exports.importer_may_export_to_own_need")
        return True

    def _question_slot(self, player_id, used, deadline):
        record = self.rounds[self.current_round]
        if record.question_id is None or used.get(SLOT_QUESTION, 0):
            return None
        if player_id in record.answers:
            return None
        cap = self.config.require_int("facilitator_questions.max_per_player_per_round")
        if cap < 1:
            return None
        if cap > 1:
            raise ConfigError(
                "facilitator_questions.max_per_player_per_round is %d, but spec #23 gives "
                "a mayor a two-slot check-in of which at most one slot can be a question; "
                "use 0 to suppress questions or 1 to ask one" % cap
            )
        question = self.content.question_by_id(record.question_id)
        return {
            "kind": SLOT_QUESTION,
            "question_id": question["id"],
            "text": question["text"],
            # Present on every question in the bank, and checked against
            # config.facilitator_questions.framing at load rather than defaulted
            # here (spec #24).
            "framing": question["framing"],
            "answer_shape": question.get("answer_shape"),
            # The same round deadline the game-action slots carry: a question is
            # part of the one check-in, not a phase with a clock of its own (#9).
            "deadline": deadline.isoformat(),
            "optional": True,
            "note": "Answering is optional; a mayor who skips leaves the "
                    "denominator rather than counting as a null answer. What the "
                    "answers add up to is decided in engine.aggregate; "
                    "newspaper.wire writes the sentence from that.",
        }

    def _guard_checkin(self, player_id, kind):
        """One check-in per round, one use per slot kind (spec #11).

        There is no separate "at most two actions" counter, and there must not
        be: the two-slot budget is already enforced by what ``checkin`` offers --
        there are only two game-action kinds, each usable once, and the question
        is offered only when a second game action is not pending. A numeric
        counter on top of that would double-count and could block a legitimate
        pending pick.
        """
        used = self._checkin_used.setdefault((player_id, self.current_round), {})
        allowance = self._slot_allowance(kind)
        if used.get(kind, 0) >= allowance:
            raise CheckInExhausted(
                "%s already used their %s slot %d time(s) in round %d (spec #11: each "
                "player checks in and acts at most once per round)"
                % (player_id, kind, allowance, self.current_round)
            )
        return used

    def _mark_checkin(self, player_id, kind):
        used = self._checkin_used.setdefault((player_id, self.current_round), {})
        used[kind] = used.get(kind, 0) + 1

    def checkin_used(self, player_id, round_index=None):
        """Slot kinds this player has already used this round (with counts)."""
        round_index = round_index if round_index is not None else self.current_round
        return dict(self._checkin_used.get((player_id, round_index), {}))

    # -- player actions ---------------------------------------------------

    def submit_export(self, player_id, text, need_key=None):
        """Submit a freeform export (spec #15).

        Accepting a player's *first* export is also what puts them in the city
        order queue (spec #5) -- exports are allowed before being queued, and
        are the way in.
        """
        if self.phase != RUNNING:
            raise PhaseError("no exports while the game is %s" % self.phase)
        player = self._player(player_id)

        need = self.collecting_need() if need_key is None else self.needs.get(need_key)
        if need is None:
            raise SubmissionRejected("no import need is collecting exports right now")
        if need.status != COLLECTING:
            raise SubmissionRejected(
                "the export window for %s closed in round %s; it is %s now"
                % (need.need_key, need.closed_round, need.status)
            )
        if need.importing_player_id == player_id and not self.config.require_bool(
            "exports.importer_may_export_to_own_need"
        ):
            raise SubmissionRejected(
                "%s opened this import need; a mayor does not export to themselves"
                % player.city
            )
        if not isinstance(text, str) or not text.strip():
            raise SubmissionRejected("an export is freeform text and must say something (spec #15)")

        cap = self.config.require_int("exports.max_submissions_per_player_per_import_per_round")
        mine = self.ledger.submissions_by(
            player_id, need.need_key, self.submissions_for(need.need_key)
        )
        if len([s for s in mine if s.submitted_round == self.current_round]) >= cap:
            raise SubmissionRejected(
                "%s already submitted %d export(s) for %s this round (cap is "
                "config.exports.max_submissions_per_player_per_import_per_round=%d)"
                % (player.city, len(mine), need.need_key, cap)
            )

        self._guard_checkin(player_id, SLOT_EXPORT)

        self._submission_counter += 1
        submission = Submission(
            submission_id="ex-%04d" % self._submission_counter,
            need_id=need.need_key,
            text=text.strip(),
            submitted_round=self.current_round,
        )
        self.submissions[submission.submission_id] = submission
        # The only place the exporter's identity is written down.
        self.ledger.record(submission.submission_id, player_id, player.city)

        if not player.is_queued:
            self.queue.append(player_id)
            player.queued_round = self.current_round
            player.import_turns_allotted = self.queue.allotment_for_new_entrant()

        self._mark_checkin(player_id, SLOT_EXPORT)
        return submission

    def pick_winner(self, player_id, ballot_ref, need_key=None):
        """The importing mayor picks a winner by ballot ref (spec #18).

        There is no overload that takes a city: the importer cannot name an
        exporter because the API gives them no way to.
        """
        if self.phase != RUNNING:
            raise PhaseError("no winner picks while the game is %s" % self.phase)
        self._player(player_id)
        need = self.picking_need_for(player_id) if need_key is None else self.needs.get(need_key)
        if need is None:
            raise PickRejected("you have no import need awaiting a winner this round")
        if need.importing_player_id != player_id:
            raise PickRejected(
                "only the importing mayor of %s picks that winner (spec #18)"
                % need.importing_city
            )
        if need.status != PICKING:
            raise PickRejected(
                "%s is %s, not awaiting a pick" % (need.need_key, need.status)
            )
        if need.closed_round != self.current_round:
            raise PickRejected(
                "the picking window for %s was round %s; it is round %d"
                % (need.need_key, need.closed_round, self.current_round)
            )
        if need.pick is not None:
            raise PickRejected("a winner for %s has already been picked" % need.need_key)

        submissions = self.submissions_for(need.need_key)
        if not submissions:
            raise PickRejected(
                "nothing was submitted for %s, so there is nothing to pick; %s ramps up "
                "its own industry instead (spec #17)" % (need.need_key, need.importing_city)
            )
        submission = ballot.resolve_ref(submissions, ballot_ref)

        self._guard_checkin(player_id, SLOT_IMPORT_PICK)
        need.pick = {
            "ballot_ref": ballot_ref,
            "submission_id": submission.submission_id,
            "picked_round": self.current_round,
        }
        self._mark_checkin(player_id, SLOT_IMPORT_PICK)
        return need.pick

    def answer_question(self, player_id, answer):
        """Record a mayor's answer. Phrasing and aggregation are M5/M6's job."""
        if self.phase != RUNNING:
            raise PhaseError("no answers while the game is %s" % self.phase)
        self._player(player_id)
        record = self.rounds[self.current_round]
        if record.question_id is None:
            raise RuleViolation(
                "no mayor question was asked in round %d (config.facilitator_questions)"
                % self.current_round
            )
        if player_id in record.answers:
            raise CheckInExhausted(
                "%s already answered round %d's question (spec #11)"
                % (player_id, self.current_round)
            )
        offered = self.checkin(player_id)["slots"]
        if not any(slot and slot["kind"] == SLOT_QUESTION for slot in offered):
            raise RuleViolation(
                "round %d offered %s no question slot -- a second game action was "
                "pending (spec #23)" % (self.current_round, player_id)
            )
        if not isinstance(answer, str) or not answer.strip():
            raise RuleViolation("an answer must say something, or be skipped entirely")
        self._guard_checkin(player_id, SLOT_QUESTION)
        record.answers[player_id] = answer.strip()
        self._mark_checkin(player_id, SLOT_QUESTION)
        return record.answers[player_id]

    def suggest_import_need(self, player_id, need):
        """Add a player-suggested import need to the pool (spec #13)."""
        self._player(player_id)
        if not self.config.require_bool("content.allow_player_suggested_import_needs"):
            raise RuleViolation(
                "player-suggested import needs are disabled "
                "(config.content.allow_player_suggested_import_needs)"
            )
        return self.content.add_player_need(need)

    # -- reporting --------------------------------------------------------

    def leaderboard(self):
        """Cumulative per-city profit (spec #20).

        Whether the *newspaper* shows this is a separate, config-driven exposure
        decision (spec #22) taken in one place: ``views.newspaper_leaderboard``.
        This method is the facilitator's own view and is always populated --
        gating it here too would mean a hidden leaderboard also stops the engine
        from being able to crown a winner at the end (#31).
        """
        return self.economy.leaderboard(self.players.values())

    def describe(self):
        return {
            "phase": self.phase,
            "current_round": self.current_round,
            "ended_round": self.ended_round,
            "timers": self.timers(),
            "queue": self.queue.describe(),
            "players": {
                pid: {
                    "city": p.city,
                    "is_facilitator": p.is_facilitator,
                    "joined_round": p.joined_round,
                    "queued_round": p.queued_round,
                    "queue_position": self.queue.position(pid),
                    "import_turns_allotted": p.import_turns_allotted,
                    "import_turns_served": p.import_turns_served,
                }
                for pid, p in self.players.items()
            },
            "needs": {
                key: {
                    "city": need.importing_city,
                    "category": need.category,
                    "opened_round": need.opened_round,
                    "closed_round": need.closed_round,
                    "resolved_round": need.resolved_round,
                    "rotation": need.rotation,
                    "status": need.status,
                    "resolution_mode": (need.resolution or {}).get("mode"),
                }
                for key, need in self.needs.items()
            },
            "rounds": {
                index: {"ops": record.ops, "events": record.events,
                        "question_id": record.question_id,
                        "answer_count": len(record.answers),
                        "answers_clustered": record.answer_buckets is not None}
                for index, record in self.rounds.items()
            },
        }
