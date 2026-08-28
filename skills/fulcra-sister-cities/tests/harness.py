"""Shared fixtures for the engine tests.

Every game here runs on a :class:`~engine.clock.ManualClock` with a fixed RNG
seed, so a test never waits and never flakes. Games are advanced by moving the
clock forward one round window and letting :meth:`GameEngine.tick` notice --
i.e. through the one round timer, the same way a real game moves, rather than by
poking round numbers directly.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import Config, Content, GameEngine  # noqa: E402
from engine.clock import utc  # noqa: E402

START = utc(2026, 9, 1, 12, 0)

#: Facilitator first, then three more mayors. Cities are real gazetteer entries
#: so the duplicate-collision path has neighbours to offer.
FACILITATOR = ("p1", "@ada", "Reykjavík")
FOUNDERS = [("p2", "@bo", "Valparaíso"), ("p3", "@cy", "Hobart")]
LATECOMER = ("p4", "@di", "Tromsø")


def make_config(**overrides):
    """config.json, optionally with keys overridden (``a__b`` means ``a.b``)."""
    config = Config.load()
    return config.overridden(**overrides) if overrides else config


def question_doc(questions, config=None):
    """Wrap a hand-made question bank in a document a game will accept.

    Scope, per-question framing and the aggregate-phrasing ladder are policy the
    engine validates before a game starts (spec #24, #25). A test that only
    cares about, say, import repetition still needs a bank that satisfies it, so
    it borrows the shipped file's scope and ladder rather than restating them --
    which also means such a test cannot drift out of sync with the real content.
    """
    real = Content.load(config or make_config()).question_doc
    return {
        "set_id": real["set_id"],
        "scope": real["scope"],
        "asking_rules": real.get("asking_rules"),
        "aggregate_phrasing": real["aggregate_phrasing"],
        "questions": [
            dict(question, framing=question.get("framing", "to_the_mayor"))
            for question in questions
        ],
    }


def new_game(founders=None, seed=1, config=None, start=True, **overrides):
    config = config if config is not None else make_config(**overrides)
    content = Content.load(config)
    game = GameEngine.for_test(START, rng_seed=seed, config=config, content=content)
    game.register_player(*FACILITATOR, is_facilitator=True)
    for player in (FOUNDERS if founders is None else founders):
        game.register_player(*player)
    if start:
        game.start()
    return game


def advance(game, rounds=1):
    """Move the one round timer forward and let the engine catch up."""
    for _ in range(rounds):
        game.clock.advance(game.timer.window)
        game.tick()
    return game.current_round


def everyone_exports(game, text_prefix="export", exclude=()):
    """Every eligible mayor submits an export for the currently open need."""
    need = game.collecting_need()
    if need is None:
        return []
    submitted = []
    for player_id in sorted(game.players):
        if player_id in exclude or player_id == need.importing_player_id:
            continue
        if "export" in game.checkin_used(player_id):
            continue  # already used their export slot this round
        submitted.append(
            game.submit_export(player_id, "%s from %s" % (text_prefix, player_id))
        )
    return submitted


def pick_first(game, player_id):
    """The importing mayor picks whatever is at the top of their blind ballot."""
    need = game.picking_need_for(player_id)
    if need is None:
        return None
    entries = game.checkin(player_id)["slots"]
    for slot in entries:
        if slot and slot["kind"] == "import_pick":
            return game.pick_winner(player_id, slot["ballot"][0]["ballot_ref"])
    return None


def play_out(game, limit=40):
    """Run a full, cooperative game: everyone exports, every importer picks."""
    rounds = 0
    while game.phase == "running" and rounds < limit:
        for player_id in sorted(game.players):
            pick_first(game, player_id)
        everyone_exports(game, "round%d" % game.current_round)
        advance(game)
        rounds += 1
    return game
