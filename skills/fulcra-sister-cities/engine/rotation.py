"""The city order queue and the two-rotation walk (spec #4, #5, #12).

Three rules govern the queue, and they interlock:

* **#4 -- facilitator first.** The facilitator's city always occupies position 1,
  so round 1 has an import need to open and nobody sits through a dead round.
* **#5 -- earn your place.** Every *other* player is appended to the queue only
  once their first export is accepted. They may export before that (and that is
  exactly how they get queued); they are not assigned an import need until they
  are in the queue.
* **#12 -- two rotations.** Players queued before rotation 1 closed get 2 import
  turns; players queued after it closed get 1.

#4 and #5 are what make each other work: the facilitator opens the first need,
everyone else answers it, and answering it is what puts them in the queue. The
queue therefore starts with exactly one entry and grows during rotation 1.

On reading #12: its prose says "players who join during/after rotation 1 get
only 1 import turn", but under #5 *every* non-facilitator player necessarily
joins the queue during rotation 1 -- which would give the whole table 1 turn and
make "players present from rotation 1 get 2" unreachable. The spec's own
Evaluation Criteria disambiguate it: "rotation-count assignment (2 imports vs 1)
matches whether they joined before or after rotation 1 **closed**". That is the
reading implemented here. See ``docs/m2-engine.md``.
"""


class CityQueue:
    """FIFO city order with a rotation-aware cursor."""

    def __init__(self, rotations_target):
        if rotations_target < 1:
            raise ValueError("rotations_target must be >= 1, got %r" % (rotations_target,))
        self.rotations_target = rotations_target
        self.order = []                 # player ids, queue position = index + 1
        self.rotation = 1
        self._cursor = 0                # next index to consider in this rotation
        self.rotation_closed_rounds = {}  # rotation number -> round it closed in
        self.exhausted = False          # every allotted import turn has been served

    # -- membership -------------------------------------------------------

    def seat_facilitator(self, player_id):
        """Put the facilitator at position 1 (spec #4)."""
        if self.order:
            raise ValueError(
                "the facilitator must be seated before anyone else so their city "
                "holds position 1 (spec #4); queue already holds %r" % (self.order,)
            )
        self.order.append(player_id)
        return 1

    def append(self, player_id):
        """Append a player who has just had their first export accepted (spec #5)."""
        if player_id in self.order:
            raise ValueError("player %r is already in the city order queue" % player_id)
        if not self.order:
            raise ValueError(
                "the facilitator holds position 1 (spec #4); seat them before appending"
            )
        self.order.append(player_id)
        return len(self.order)

    def position(self, player_id):
        return self.order.index(player_id) + 1 if player_id in self.order else None

    def allotment_for_new_entrant(self):
        """Import turns a player joining the queue *right now* is owed (spec #12).

        Rotation 1 still open -> the full ``rotations_target``. Otherwise a
        single turn, in the rotation they arrived in.
        """
        return self.rotations_target if self.rotation == 1 else 1

    # -- the walk ---------------------------------------------------------

    def next_importer(self, players, current_round):
        """The player whose import need opens now, or ``None`` if the game is out.

        Walks the queue in order within the current rotation; when the cursor
        runs off the end, that rotation closes and the next begins. A player is
        due a turn while they have served fewer than their allotment, and the
        single pass per rotation is what limits them to one turn per rotation.
        """
        while not self.exhausted:
            while self._cursor < len(self.order):
                player_id = self.order[self._cursor]
                self._cursor += 1
                player = players[player_id]
                if player.import_turns_served < player.import_turns_allotted:
                    return player_id
            # Cursor ran off the end: this rotation is over.
            self.rotation_closed_rounds.setdefault(self.rotation, current_round)
            if self.rotation >= self.rotations_target:
                self.exhausted = True
                return None
            self.rotation += 1
            self._cursor = 0
        return None

    @property
    def rotation_1_closed(self):
        return 1 in self.rotation_closed_rounds

    def describe(self):
        return {
            "order": list(self.order),
            "rotation": self.rotation,
            "rotations_target": self.rotations_target,
            "rotation_closed_rounds": dict(self.rotation_closed_rounds),
            "exhausted": self.exhausted,
        }
