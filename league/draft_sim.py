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
SOURCE_MANUAL = 'manual'          # the user chose this one themselves (mock draft)
SOURCE_BURNED = 'burned'          # forfeited to pay for a keeper; nobody picks
SOURCE_EMPTY = 'empty'            # pool ran dry, or every candidate was capped

# How many cap-respecting names the pause offers by default.
DEFAULT_SUGGESTIONS = 5


class SimError(ValueError):
    """A manual pick that cannot be honoured -- taken player, wrong slot.

    Raised rather than silently ignored: a mock draft that quietly drops a
    choice would show a board that never happened.
    """


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


@dataclass(frozen=True)
class PendingPick:
    """The user's pick the replay stopped at, and what they could take.

    `suggestions` are the cap-respecting names -- what a sensible manager would
    consider here. They are only a default: the caps shape the suggestion list
    and nothing else. A manager drafting their own team may take a second
    quarterback in Round 2 if they want to.
    """

    pick_id: int
    round: int
    team_id: int
    overall: int
    suggestions: list


@dataclass(frozen=True)
class SimRun:
    """The outcome of one replay.

    Nothing here is stored anywhere. Every step re-runs the whole draft from
    the same inputs, which is only viable because the ordering is total: the
    same manual picks always replay to the same board, so "resume" is just
    "run again with one more choice". That is what makes undo a one-line
    client change rather than a server-side history.
    """

    cells: list                  # SimPick, in pick order, for everything decided
    paused_at: PendingPick = None
    available: list = None       # pool still undrafted at the pause, ADP order

    @property
    def done(self):
        return self.paused_at is None


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


def ordered_picks(slots, picks, rounds=None):
    """Every pick in true draft order: (overall, pick) pairs, earliest first.

    Shared by the replay and by the caller that needs to know which cells are
    the user's, in order, without re-deriving the snake in a second place.
    """
    slot_by_team = {slot.team_id: slot.slot for slot in slots}
    team_count = len(slot_by_team)
    if not team_count:
        return []

    if rounds is None:
        rounds = max((pick.round for pick in picks), default=0)

    ordered = []
    for pick in picks:
        slot = slot_by_team.get(pick.original_team_id)
        if slot is None or pick.round > rounds:
            # A pick whose column is not on the board cannot be placed; leaving
            # it out beats guessing a position for it.
            continue
        ordered.append((snake_overall(slot, pick.round, team_count), pick))

    ordered.sort(key=lambda item: (item[0], item[1].pk))
    return ordered


def run_sim(*, slots, picks, pool, burned_pick_ids=(), taken_player_ids=(),
            roster_positions=None, rounds=None, user_team_id=None,
            manual_picks=None, stop_at_next_user_pick=False,
            suggestion_count=DEFAULT_SUGGESTIONS):
    """Replay the draft, injecting the user's own picks, and return a SimRun.

    Two modes, one code path:

      * `stop_at_next_user_pick=False` -- the full-auto projection. Every cell
        including the user's is filled from ADP.
      * `stop_at_next_user_pick=True` -- mock draft. Other teams still draft
        chalk, but the replay halts the moment it reaches one of
        `user_team_id`'s picks that has no entry in `manual_picks`, and reports
        what is on the board so far plus what could be taken there.

    There is no session and no stored cursor: "resume" means replaying from the
    start with one more manual pick. That is only sound because the ordering is
    total, so the same inputs always rebuild the identical board -- and it is
    what makes undo trivial, since dropping a choice and replaying is the whole
    operation.

    Arguments beyond simulate_draft's:
      user_team_id    whose picks pause the replay. None disables mock mode.
      manual_picks    {pick_id: player_id} already chosen by the user.
      suggestion_count how many cap-respecting names to offer at the pause.

    Raises SimError if a manual pick names an unknown or already-taken player,
    or a cell that is not this user's to fill.
    """
    manual = {int(k): int(v) for k, v in (manual_picks or {}).items()}

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
    by_id = {p.pk: p for p in available}
    taken = set()
    # Everything before this index is already drafted, so later picks skip it
    # instead of rescanning the whole consumed prefix.
    start = 0

    def best_available(round_number, taken_positions, limit=1):
        """The next `limit` players this team would plausibly take here."""
        found = []
        for index in range(start, len(available)):
            candidate = available[index]
            if candidate.pk in taken:
                continue
            if _allowed(candidate.position, round_number, taken_positions, final_rounds_start):
                found.append(candidate)
                if len(found) == limit:
                    break
        return found

    ordered = ordered_picks(slots, picks, rounds)

    # Validate the manual picks against the board BEFORE replaying, not as a
    # postscript: the replay returns early at the pause, so anything checked
    # after the loop would go unchecked in exactly the mode that needs it. A
    # choice for someone else's cell, or for a cell burned to pay a keeper, is
    # refused rather than quietly dropped -- silently ignoring it would leave
    # the client believing in a pick no board will ever show.
    if manual:
        mine = {
            pick.pk for _, pick in ordered
            if pick.current_team_id == user_team_id and pick.pk not in burned
        }
        stranded = sorted(set(manual) - mine)
        if stranded:
            raise SimError(f'Pick {stranded[0]} is not yours to fill.')

    cells = []
    for overall, pick in ordered:
        team_id = pick.current_team_id

        if pick.pk in burned:
            # The slot is dead, not deleted. Nobody picks here and nobody else
            # moves up -- a forfeited pick is skipped, and the draft carries on
            # around it (rules section 3). A burned cell is therefore never
            # offered to the user either: there is no pick to make.
            cells.append(SimPick(pick.pk, pick.round, team_id, overall, None, SOURCE_BURNED))
            continue

        while start < len(available) and available[start].pk in taken:
            start += 1

        taken_positions = held.setdefault(team_id, set())
        is_mine = user_team_id is not None and team_id == user_team_id
        chosen = None
        source = SOURCE_SIM

        if is_mine and pick.pk in manual:
            player_id = manual[pick.pk]
            chosen = by_id.get(player_id)
            if chosen is None:
                raise SimError(f'Player {player_id} is not in the draft pool.')
            if chosen.pk in taken:
                raise SimError(f'{getattr(chosen, "name", player_id)} has already been drafted.')
            source = SOURCE_MANUAL

        elif is_mine and stop_at_next_user_pick:
            # Stop here and hand back the board so far. Everything after this
            # cell is undecided by definition -- it depends on who is taken now.
            return SimRun(
                cells=cells,
                paused_at=PendingPick(
                    pick_id=pick.pk,
                    round=pick.round,
                    team_id=team_id,
                    overall=overall,
                    suggestions=best_available(pick.round, taken_positions, suggestion_count),
                ),
                available=[p for p in available if p.pk not in taken],
            )

        else:
            found = best_available(pick.round, taken_positions)
            chosen = found[0] if found else None

        if chosen is None:
            cells.append(SimPick(pick.pk, pick.round, team_id, overall, None, SOURCE_EMPTY))
            continue

        taken.add(chosen.pk)
        taken_positions.add(chosen.position)
        cells.append(SimPick(pick.pk, pick.round, team_id, overall, chosen.pk, source))

    return SimRun(cells=cells, available=[p for p in available if p.pk not in taken])


def simulate_draft(*, slots, picks, pool, burned_pick_ids=(), taken_player_ids=(),
                   roster_positions=None, rounds=None):
    """Project the whole draft, returning one SimPick per cell in pick order.

    The full-auto case, and a thin wrapper over run_sim() so there is exactly
    one implementation of draft order, the caps and the ADP walk.

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
    return run_sim(
        slots=slots,
        picks=picks,
        pool=pool,
        burned_pick_ids=burned_pick_ids,
        taken_player_ids=taken_player_ids,
        roster_positions=roster_positions,
        rounds=rounds,
    ).cells
