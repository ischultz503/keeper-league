"""Draft simulation: fill the empty half of the board from ADP.

Pure Python, like keeper_engine. No request objects, no sessions, no rendering
-- everything here is callable from a test with hand-built fixtures. The only
Django things that reach this module are model *instances* passed in as
arguments, and only their plain attributes are read.

The job: given the draft slots, the pick inventory, which cells are already
spoken for by keepers, and a pool of players with ADP, work out who would go
where if the rest of the draft ran chalk. It is a projection, not a
prediction -- the board renders these fills in muted colours for exactly that
reason.

Determinism is a hard requirement, not a nicety. A manager compares scenarios
by simulating, changing one keeper, and simulating again; if the untouched
parts of the board reshuffled between runs the comparison would be worthless.
So every ordering here is total -- no set iteration decides anything.
"""

from dataclasses import dataclass

from .keeper_engine import snake_overall

# --- Rules of the simulation ------------------------------------------------
# These are house-league heuristics about how a draft *tends* to go, not rules
# from docs/keeper_rules_v3.md. The rules doc has nothing to say about who a
# team would draft -- it only governs keepers. Keeping the two sets of
# constants in separate modules keeps that distinction visible.

# Through this round a team takes at most one QB and one TE. Nobody starts two
# of either, and a projection that hands a team three quarterbacks in the first
# eight rounds reads as broken rather than as a projection.
STARTER_ONLY_THROUGH_ROUND = 8

# Kickers and defenses are never drafted early and almost always go at the very
# end, so they are confined to the last two rounds of the draft.
LATE_ONLY_POSITIONS = ('K', 'DEF')
LATE_ONLY_FINAL_ROUNDS = 2

# Positions nobody rosters two of while starters are still on the board.
SINGLE_STARTER_POSITIONS = ('QB', 'TE')


# --- Results ----------------------------------------------------------------

# Why a cell holds what it holds. The board paints on this: real declarations
# and locked calls are loud, projections are muted, burned cells are dead.
SOURCE_KEEPER = 'keeper'          # a declared keeper (post-reveal)
SOURCE_PREDICTION = 'prediction'  # this user's locked call on a rival
SOURCE_SANDBOX = 'sandbox'        # the user's own unticked-at-any-moment plan
SOURCE_SIM = 'sim'                # projected from ADP by this module
SOURCE_BURNED = 'burned'          # forfeited to pay for a keeper; nobody picks
SOURCE_EMPTY = 'empty'            # pool ran dry, or every candidate was capped


@dataclass(frozen=True)
class SimPick:
    """One cell of the simulated board.

    team_id is the team that would *make* the pick, i.e. the pick's current
    owner. That is not always the column the cell sits in: a traded pick keeps
    the original team's slot (rules section 7), so Marcus's Round 4 sits in
    Marcus's column but Isaac drafts there and the player lands on Isaac's
    roster for the positional caps below.
    """

    pick_id: int
    round: int
    team_id: int
    overall: int
    player_id: int | None
    source: str

    @property
    def filled(self):
        return self.player_id is not None


# --- The simulation ---------------------------------------------------------


def sort_pool(pool):
    """The draft pool in the order a chalk draft would take it.

    Best ADP first; players with no ADP sort last (we know nothing about them,
    so they are the leftovers). The `pk` tie-break is what makes the whole
    simulation reproducible -- ADP ties are common near the end of the list,
    and without a second key the order would depend on however the database
    happened to return the rows.
    """
    return sorted(pool, key=lambda p: (p.adp is None, p.adp if p.adp is not None else 0.0, p.pk))


def _allowed(position, round_number, taken_positions, final_rounds_start):
    """Would a team plausibly take this position here?

    `taken_positions` is what that team already holds -- kept, predicted,
    sandboxed, or filled earlier in this same simulation.
    """
    if position in LATE_ONLY_POSITIONS:
        return round_number >= final_rounds_start

    if round_number <= STARTER_ONLY_THROUGH_ROUND and position in SINGLE_STARTER_POSITIONS:
        return position not in taken_positions

    return True


def simulate_draft(*, slots, picks, pool, burned_pick_ids=(), taken_player_ids=(),
                   roster_positions=None, rounds=None):
    """Project the whole draft, returning one SimPick per cell in pick order.

    Arguments (all plain data, so a test can build them by hand):
      slots               DraftSlot rows for the season -- gives each team its
                          column, and their count gives the snake its width.
      picks               DraftPick rows for the season.
      pool                Players available to be drafted. Anything with
                          .pk, .position and .adp will do.
      burned_pick_ids     Pick ids forfeited to pay for a keeper, a locked
                          prediction, or the user's sandbox selection. This is
                          the *only* thing that kills a cell -- `DraftPick.
                          forfeited` is deliberately ignored, because before the
                          reveal that flag records declarations the league is
                          not allowed to see yet (rules section 1). The caller
                          decides what counts as burned; see views.simulate.
      taken_player_ids    Players who must never be projected -- everyone kept,
                          predicted or sandboxed anywhere in the league.
      roster_positions    {team_id: iterable of position strings} a team already
                          holds, so the positional caps count keepers.
      rounds              How many rounds the draft runs. Defaults to the
                          deepest round in `picks`.

    Returns every cell, burned and empty included, so a caller can paint the
    whole board from one list rather than reconciling two.
    """
    slot_by_team = {slot.team_id: slot.slot for slot in slots}
    team_count = len(slot_by_team)
    if not team_count:
        return []

    if rounds is None:
        rounds = max((pick.round for pick in picks), default=0)

    # "The last two rounds" is measured against the full draft, which is why
    # `rounds` is the whole draft and not just the rows on screen. Simulating
    # only the visible eight rounds of a thirteen-round draft would otherwise
    # start handing out kickers in Round 7.
    final_rounds_start = rounds - LATE_ONLY_FINAL_ROUNDS + 1

    burned = set(burned_pick_ids)

    held = {team_id: set(positions) for team_id, positions in (roster_positions or {}).items()}

    available = [p for p in sort_pool(pool) if p.pk not in set(taken_player_ids)]
    taken = set()
    # Everything before this index is already drafted, so later picks skip it
    # instead of rescanning the whole consumed prefix.
    start = 0

    ordered = []
    for pick in picks:
        slot = slot_by_team.get(pick.original_team_id)
        if slot is None or pick.round > rounds:
            # A pick whose column is not on the board cannot be placed; leaving
            # it out beats guessing a position for it.
            continue
        ordered.append((snake_overall(slot, pick.round, team_count), pick))
    ordered.sort(key=lambda item: (item[0], item[1].pk))

    results = []
    for overall, pick in ordered:
        team_id = pick.current_team_id

        if pick.pk in burned:
            # The slot is dead, not deleted. Nobody picks here and nobody else
            # moves up -- a forfeited pick is skipped, and the draft carries on
            # around it (rules section 3).
            results.append(SimPick(pick.pk, pick.round, team_id, overall, None, SOURCE_BURNED))
            continue

        while start < len(available) and available[start].pk in taken:
            start += 1

        taken_positions = held.setdefault(team_id, set())
        chosen = None
        for index in range(start, len(available)):
            candidate = available[index]
            if candidate.pk in taken:
                continue
            if _allowed(candidate.position, pick.round, taken_positions, final_rounds_start):
                chosen = candidate
                break

        if chosen is None:
            results.append(SimPick(pick.pk, pick.round, team_id, overall, None, SOURCE_EMPTY))
            continue

        taken.add(chosen.pk)
        taken_positions.add(chosen.position)
        results.append(SimPick(pick.pk, pick.round, team_id, overall, chosen.pk, SOURCE_SIM))

    return results
