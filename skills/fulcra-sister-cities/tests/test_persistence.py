"""Tests for the facilitator-private durable engine snapshot."""

import os
import tempfile
import unittest

from engine.persistence import SnapshotError, SnapshotStore
from harness import new_game


class PersistenceTests(unittest.TestCase):
    def test_round_trip_preserves_private_engine_and_public_checkin(self):
        game = new_game()
        game.submit_export("p2", "a wind-powered canning line")
        expected = game.checkin("p1")
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(os.path.join(directory, "game.snapshot"))
            with store.locked():
                store.save(game)
            with store.locked():
                restored = store.load()
        self.assertEqual(restored.current_round, game.current_round)
        self.assertEqual(restored.checkin("p1"), expected)
        # The ledger is deliberately private but must survive a facilitator
        # restart so cap enforcement/blind voting cannot be bypassed.
        self.assertEqual(restored.ledger.all_submission_ids(), game.ledger.all_submission_ids())

    def test_save_and_load_require_a_single_writer_lock(self):
        game = new_game()
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(os.path.join(directory, "game.snapshot"))
            with self.assertRaises(SnapshotError):
                store.save(game)
            with self.assertRaises(SnapshotError):
                store.load()


if __name__ == "__main__":
    unittest.main()
