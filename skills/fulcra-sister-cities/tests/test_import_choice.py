"""Spec #13, #13a: the importing mayor orders, and orders actual goods.

Two rules from the user decision of 2026-08-31, and they are separate rules:

* **#13 -- agency.** A city's import need is the one its mayor filed, chosen
  from a slate of eligible seeds or written freehand. The engine draws nothing,
  and a turn nobody filed for is held and then lost rather than filled.
* **#13a -- trade.** What is filed is an order for food, materials, equipment,
  living things, cultural works or a specialist service. Not a request for
  advice, and not a civic problem handed over for solving.

The first is about who decides; the second is about what may be decided. A game
can satisfy either one and fail the other, so they are tested apart.
"""

import json
import os
import unittest

from harness import (
    FACILITATOR, FOUNDERS, LATECOMER, advance, everyone_exports, file_orders,
    make_config, new_game, play_out,
)
from engine import Content, GameEngine
from engine.clock import utc
from engine.content import normalize_city
from engine.errors import (
    CheckInExhausted, ImportChoiceRejected, RuleViolation, TradeRefused,
)
from engine.game import SLOT_IMPORT_CHOICE, SLOT_QUESTION
from engine.trade import TradePolicy

FREEFORM = {
    "category": "energy_and_utilities",
    "trade_family": "materials",
    "title": "Ninety kilometres of insulated pipe",
    "need_brief": "{city} has more hot water under it than pipe to put it in. "
                  "Wanted: pre-insulated pipe in six-metre lengths, valves, "
                  "lagging, and welders who have worked in a trench in February.",
    "exporter_prompt": "Ship {city} the pipe, the fittings or the welders.",
}


def unstarted(**overrides):
    """A seated table that has filed nothing yet."""
    config = make_config(**overrides)
    game = GameEngine.for_test(
        utc(2026, 9, 1, 12), rng_seed=5, config=config, content=Content.load(config)
    )
    game.register_player(*FACILITATOR, is_facilitator=True)
    for founder in FOUNDERS:
        game.register_player(*founder)
    return game


# -- #13: the mayor decides -------------------------------------------------

class ImporterAgencyTest(unittest.TestCase):
    def test_the_offer_is_a_slate_of_eligible_seeds_plus_a_freeform_door(self):
        game = unstarted()
        offer = game.import_choice_offer("p1")
        self.assertEqual(
            len(offer["suggestions"]),
            game.config.require_int("imports.suggestions_offered_to_importer"),
        )
        self.assertEqual(offer["turn"], 1)
        self.assertEqual(offer["of"], game.config.require_int("rounds.rotations_target"))
        for suggestion in offer["suggestions"]:
            self.assertIn(suggestion["need_id"], {n["id"] for n in game.content.needs})
            self.assertNotIn("{city}", suggestion["need_brief"])
            self.assertIn(FACILITATOR[2], suggestion["need_brief"])
        # Spec #13 asks for suggestions *and* a freeform request, so the offer
        # carries what a mayor needs to write one.
        self.assertTrue(offer["freeform"]["families"])
        self.assertTrue(offer["freeform"]["note_to_mayor"])

    def test_the_slate_is_a_choice_rather_than_four_shades_of_one_thing(self):
        game = unstarted()
        offer = game.import_choice_offer("p1")
        categories = [suggestion["category"] for suggestion in offer["suggestions"]]
        self.assertEqual(len(set(categories)), len(categories))

    def test_the_need_that_opens_is_the_one_the_mayor_filed(self):
        game = new_game()
        play_out(game)
        self.assertTrue(game.needs)
        for need in game.needs.values():
            self.assertEqual(need.order["filed_by"], game.players[
                need.importing_player_id].mayor)
            self.assertIn(need.order["request_source"], ("seed", "freeform"))

    def test_a_mayor_may_order_an_eligible_seed_that_was_not_on_the_slate(self):
        """The slate suggests; it does not restrict (spec #13)."""
        game = unstarted()
        offer = game.import_choice_offer("p1")
        shown = {suggestion["need_id"] for suggestion in offer["suggestions"]}
        elsewhere = next(
            need["id"] for need in game.content.needs if need["id"] not in shown
        )
        filed = game.choose_import("p1", need_id=elsewhere)
        self.assertEqual(filed["need_id"], elsewhere)
        game.choose_import("p1", need_id=next(iter(shown)))
        for founder in FOUNDERS:
            file_orders(game, [founder[0]])
        game.start()
        self.assertEqual(game.collecting_need().content_need_id, elsewhere)

    def test_a_freeform_order_becomes_the_city_s_next_import_and_joins_the_pool(self):
        game = unstarted()
        before = len(game.content.needs)
        filed = game.choose_import("p1", request=FREEFORM)
        self.assertEqual(filed["request_source"], "freeform")
        self.assertEqual(len(game.content.needs), before + 1)
        game.choose_import("p1", need_id=game.import_choice_offer("p1")
                           ["suggestions"][0]["need_id"])
        for founder in FOUNDERS:
            file_orders(game, [founder[0]])
        game.start()
        need = game.collecting_need()
        self.assertEqual(need.order["request_source"], "freeform")
        self.assertIn("insulated pipe", need.rendered["need_brief"])
        self.assertIn(FACILITATOR[2], need.rendered["need_brief"])

    def test_filing_a_seed_and_a_freeform_request_at_once_is_refused(self):
        game = unstarted()
        with self.assertRaises(ImportChoiceRejected):
            game.choose_import("p1", need_id="need-infrastructure-01", request=FREEFORM)
        with self.assertRaises(ImportChoiceRejected):
            game.choose_import("p1")

    def test_a_mayor_cannot_file_more_orders_than_they_have_turns(self):
        game = unstarted()
        file_orders(game, ["p1"])
        self.assertEqual(game.unfiled_import_turns("p1"), 0)
        with self.assertRaises(ImportChoiceRejected):
            game.choose_import("p1", request=FREEFORM)

    def test_the_game_will_not_start_until_the_facilitator_has_ordered(self):
        """Spec #4 gives them round 1; spec #13 says round 1 is theirs to fill."""
        game = unstarted()
        file_orders(game, ["p2", "p3"])
        with self.assertRaises(ImportChoiceRejected):
            game.start()
        file_orders(game, ["p1"])
        game.start()
        self.assertEqual(game.collecting_need().importing_city, FACILITATOR[2])


class NoUnchosenNeedTest(unittest.TestCase):
    """The strong form of #13: there is no way to receive an unordered need."""

    def test_the_engine_owns_no_function_that_picks_a_need_for_a_city(self):
        content = Content.load(make_config())
        self.assertFalse(hasattr(content, "draw_need"))
        self.assertTrue(hasattr(content, "suggest_needs"))

    def test_a_round_whose_importer_filed_nothing_opens_nothing(self):
        game = unstarted()
        file_orders(game, ["p1"])          # only the facilitator orders
        game.start()
        everyone_exports(game)             # p2 and p3 are queued by exporting
        advance(game, orders=False)        # round 2 is p2's turn, unfiled
        opened = [e for e in game.rounds[2].events if e["op"] == "OPEN"][0]
        self.assertIsNone(opened["need"])
        self.assertEqual(opened["city"], FOUNDERS[0][2])
        self.assertEqual(opened["rounds_held"], 1)
        self.assertEqual(game.queue.waiting_on, "p2")
        self.assertEqual(len(game.needs), 1)

    def test_a_held_turn_is_passed_over_once_the_grace_runs_out(self):
        game = unstarted(imports__unchosen_turn_grace_rounds=1)
        file_orders(game, ["p1", "p3"])
        game.start()
        everyone_exports(game)
        advance(game, orders=False)        # round 2: held (1 of 1)
        advance(game, orders=False)        # round 3: grace spent -> passed over
        opened = [e for e in game.rounds[3].events if e["op"] == "OPEN"][0]
        self.assertEqual(opened["forfeited"], [FOUNDERS[0][2]])
        self.assertEqual(game.players["p2"].import_turns_forfeited, 1)
        # The turn is lost and nothing else is: no penalty, no substitution, and
        # no need opened in this city's name that its mayor did not order.
        self.assertEqual(game.players["p2"].import_turns_served, 0)
        self.assertEqual(
            [n for n in game.needs.values() if n.importing_player_id == "p2"], []
        )
        # The queue moved on to the next city that had filed something.
        self.assertEqual(game.needs["in-002"].importing_city, FOUNDERS[1][2])

    def test_a_passed_over_mayor_still_gets_their_next_rotation(self):
        game = unstarted(imports__unchosen_turn_grace_rounds=0)
        file_orders(game, ["p1", "p3"])
        game.start()
        everyone_exports(game)
        advance(game, orders=False)        # p2's first turn is lost at once
        self.assertEqual(game.players["p2"].import_turns_forfeited, 1)
        file_orders(game, ["p2"])
        play_out(game)
        served = [n.importing_city for n in game.needs.values()]
        self.assertIn(FOUNDERS[0][2], served)


class ChoiceInTheCheckInTest(unittest.TestCase):
    """Spec #11, #23: filing is a game action and lives in the two-slot budget."""

    def test_a_mayor_with_a_turn_coming_is_asked_for_the_order(self):
        game = unstarted()
        file_orders(game, ["p1"])
        game.start()
        everyone_exports(game)
        # p2's turn is next round, which is inside the configured lookahead.
        slots = [slot for slot in game.checkin("p2")["slots"] if slot]
        kinds = [slot["kind"] for slot in slots]
        self.assertIn(SLOT_IMPORT_CHOICE, kinds)
        self.assertLessEqual(len(slots), 2)
        choice = next(slot for slot in slots if slot["kind"] == SLOT_IMPORT_CHOICE)
        self.assertTrue(choice["suggestions"])
        self.assertEqual(choice["opens_in_rounds"], 1)

    def test_a_mayor_whose_turn_is_far_off_is_not_pestered(self):
        game = new_game(founders=[("p%d" % n, "@m%d" % n, city) for n, city in
                                  enumerate(("Valparaíso", "Hobart", "Tromsø", "Osaka"), 2)])
        # Everybody has already filed (new_game does), so nobody is asked at all.
        for player_id in game.players:
            kinds = [slot["kind"] for slot in game.checkin(player_id)["slots"] if slot]
            self.assertNotIn(SLOT_IMPORT_CHOICE, kinds)

    def test_filing_when_asked_uses_the_slot_and_only_once(self):
        # A lookahead wide enough that *both* of this mayor's turns are being
        # asked about, so the second order in one round is the case the slot
        # budget exists for (spec #11: at most one check-in per round).
        game = unstarted(imports__choice_offered_rounds_ahead=8)
        file_orders(game, ["p1"])
        game.start()
        everyone_exports(game)
        offer = game.import_choice_offer("p2")
        game.choose_import("p2", need_id=offer["suggestions"][0]["need_id"])
        self.assertEqual(game.checkin_used("p2").get(SLOT_IMPORT_CHOICE), 1)
        self.assertEqual(game.unfiled_import_turns("p2"), 1)
        with self.assertRaises(CheckInExhausted):
            game.choose_import("p2", need_id=offer["suggestions"][1]["need_id"])
        # ... and the round after, they are asked again and may file it.
        advance(game, orders=False)
        game.choose_import("p2", need_id=offer["suggestions"][1]["need_id"])
        self.assertEqual(game.unfiled_import_turns("p2"), 0)

    def test_filing_before_being_asked_costs_no_slot(self):
        """A mayor who orders at the table has not taken a turn at the round."""
        game = new_game()
        self.assertEqual(game.checkin_used("p2"), {})
        kinds = [slot["kind"] for slot in game.checkin("p2")["slots"] if slot]
        self.assertIn(SLOT_QUESTION, kinds)

    def test_the_check_in_still_offers_at_most_two_slots(self):
        game = unstarted()
        file_orders(game, ["p1"])
        game.start()
        while game.phase == "running":
            for player_id in sorted(game.players):
                self.assertLessEqual(
                    len([slot for slot in game.checkin(player_id)["slots"] if slot]), 2
                )
            everyone_exports(game)
            advance(game)


class RepetitionStillHoldsTest(unittest.TestCase):
    """Spec #14 binds the mayor's choice exactly as it bound the old draw."""

    def test_a_city_cannot_order_a_category_it_has_already_imported(self):
        game = new_game()
        play_out(game)
        by_city = {}
        for need in game.needs.values():
            by_city.setdefault(need.importing_city, []).append(need.category)
        for city, categories in by_city.items():
            self.assertEqual(len(categories), len(set(categories)), city)

    def test_an_order_in_a_category_this_city_already_has_is_refused(self):
        game = new_game()
        need = game.collecting_need()
        with self.assertRaises(ImportChoiceRejected):
            game.choose_import(
                "p1",
                request=dict(FREEFORM, category=need.category, title="Something else"),
            )

    def test_a_seed_another_city_has_already_filed_is_not_offered_or_accepted(self):
        game = unstarted()
        taken = game.import_choice_offer("p1")["suggestions"][0]["need_id"]
        game.choose_import("p1", need_id=taken)
        offer = game.import_choice_offer("p2")
        self.assertNotIn(taken, {s["need_id"] for s in offer["suggestions"]})
        with self.assertRaises(ImportChoiceRejected):
            game.choose_import("p2", need_id=taken)

    def test_no_need_is_opened_twice_in_one_game(self):
        game = play_out(new_game())
        used = [need.content_need_id for need in game.needs.values()]
        self.assertEqual(len(used), len(set(used)))


# -- #13a: what may be ordered ---------------------------------------------

class TradePolicyTest(unittest.TestCase):
    def setUp(self):
        self.content = Content.load(make_config())
        self.policy = self.content.trade

    def test_every_seeded_need_is_an_order_for_goods_or_services(self):
        for need in self.content.needs:
            self.policy.check_need(need, where=need["id"])
            self.assertIn(need["trade_family"], self.policy.families)
            self.assertIsNone(
                self.policy.advice_marker_in(
                    " ".join([need["title"], need["need_brief"], need["exporter_prompt"]])
                ),
                need["id"],
            )

    def test_the_seeds_cover_every_kind_of_tradable_thing_spec_13a_names(self):
        families = {need["trade_family"] for need in self.content.needs}
        self.assertEqual(families, set(self.policy.families))

    def test_the_seed_list_is_varied_rather_than_forty_eight_of_one_thing(self):
        """#33's gameability, as far as a machine can check it."""
        titles = [need["title"] for need in self.content.needs]
        briefs = [need["need_brief"] for need in self.content.needs]
        self.assertEqual(len(set(titles)), len(titles))
        self.assertEqual(len(set(briefs)), len(briefs))
        self.assertGreaterEqual(len(self.content.categories), 8)
        for category in self.content.categories:
            self.assertGreaterEqual(
                len([n for n in self.content.needs if n["category"] == category]), 2
            )

    def test_an_order_for_advice_is_refused_and_says_which_words_did_it(self):
        with self.assertRaises(TradeRefused) as caught:
            self.policy.check_need(
                {
                    "id": "need-test-01",
                    "category": "civic_ritual",
                    "trade_family": "cultural_works",
                    "title": "A better ceremony",
                    "need_brief": "{city} has a ribbon and no occasion.",
                    "exporter_prompt": "What should {city} do about the ribbon?",
                }
            )
        self.assertEqual(caught.exception.phrase, "what should")
        self.assertIn("#13a", str(caught.exception))

    def test_a_prompt_that_asks_for_no_consignment_is_refused(self):
        with self.assertRaises(TradeRefused):
            self.policy.check_need(
                {
                    "id": "need-test-02",
                    "category": "civic_ritual",
                    "trade_family": "materials",
                    "title": "Ribbon",
                    "need_brief": "{city} is buying four hundred metres of ribbon.",
                    "exporter_prompt": "{city} awaits your thinking on ribbon.",
                }
            )

    def test_a_need_with_no_trade_family_is_refused(self):
        with self.assertRaises(TradeRefused):
            self.policy.check_need(
                {
                    "id": "need-test-03",
                    "category": "civic_ritual",
                    "title": "Ribbon",
                    "need_brief": "{city} is buying ribbon.",
                    "exporter_prompt": "Ship {city} ribbon.",
                }
            )

    def test_an_order_that_merely_mentions_fixing_or_explaining_is_fine(self):
        """The check is for requests for advice, not for the words themselves."""
        self.policy.check_need(
            {
                "id": "need-test-04",
                "category": "infrastructure",
                "trade_family": "materials",
                "title": "Brackets, and the crew who fix them in place",
                "need_brief": "{city} is buying brackets, and somewhere in the "
                              "archive is the minute that explains the pipes.",
                "exporter_prompt": "Ship {city} the brackets and the crew who fix "
                                   "them in place.",
            }
        )

    def test_a_city_named_in_place_of_the_placeholder_is_still_caught(self):
        self.assertEqual(
            self.policy.advice_marker_in("Please tell Reykjavík what it ought to buy"),
            "tell {city} what",
        )

    def test_a_content_file_with_no_trade_policy_will_not_load(self):
        from engine.errors import ContentError

        with self.assertRaises(ContentError):
            TradePolicy(None)


class FreeformTradeTest(unittest.TestCase):
    def test_a_freeform_request_for_advice_is_refused(self):
        game = unstarted()
        with self.assertRaises(TradeRefused):
            game.choose_import(
                "p1",
                request=dict(
                    FREEFORM,
                    title="A plan for the pipes",
                    need_brief="{city} would like ideas for its heating network.",
                    exporter_prompt="Send {city} your best thinking.",
                ),
            )
        self.assertEqual(game.import_programme_for("p1"), [])

    def test_a_freeform_request_missing_its_family_is_refused(self):
        game = unstarted()
        with self.assertRaises(TradeRefused):
            game.choose_import("p1", request={k: v for k, v in FREEFORM.items()
                                              if k != "trade_family"})

    def test_a_freeform_request_gets_the_content_file_s_defaults(self):
        game = unstarted()
        bare = {k: v for k, v in FREEFORM.items() if k != "exporter_prompt"}
        game.choose_import("p1", request=bare)
        filed = game.players["p1"].import_programme[0]["need"]
        defaults = game.content.trade.freeform["default_exporter_prompt"]
        self.assertEqual(filed["exporter_prompt"], defaults)
        self.assertTrue(filed["excess_flavor"])
        self.assertEqual(filed["requested_by_city"], FACILITATOR[2])


class TradeReachesThePlayersTest(unittest.TestCase):
    """The point of #13a is what the mayors are actually asked for."""

    def test_the_export_slot_asks_for_a_consignment(self):
        game = new_game()
        slot = next(
            slot for slot in game.checkin("p2")["slots"]
            if slot and slot["kind"] == "export"
        )
        policy = game.content.trade
        self.assertIsNone(policy.advice_marker_in(slot["exporter_prompt"]))
        self.assertIsNone(policy.advice_marker_in(slot["need_brief"]))
        self.assertTrue(policy._verb_re.search(slot["exporter_prompt"]))

    def test_the_public_record_of_a_need_says_who_filed_it_and_what_kind(self):
        from engine import views

        game = new_game()
        need = game.collecting_need()
        briefing = views.need_briefing(game, need)
        self.assertEqual(briefing["filed_by"], game.players["p1"].mayor)
        self.assertEqual(briefing["request_source"], "seed")
        self.assertIn(briefing["trade_family"], game.content.trade.families)


if __name__ == "__main__":
    unittest.main()
