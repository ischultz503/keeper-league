"""The keeper rules engine.

Plain Python implementing docs/keeper_rules_v3.md sections 2-6. No request
objects, no rendering, no templates -- everything here is callable from a test,
a management command, the admin, or a view.

IMPORTANT: this module deliberately imports no models at the top level.
league.models imports base_cost() from here for RosterEntry.base_keeper_cost,
so a module-level `from .models import ...` would be a circular import. The
database-touching functions import what they need *inside* the function body --
a standard Django idiom for breaking exactly this cycle.
"""

from dataclasses import dataclass, field, replace

# --- Rule constants (docs/keeper_rules_v3.md) --------------------------------

MAX_KEEPERS_PER_TEAM = 3        # section 1
MAX_KEEPS_PER_PLAYER = 3        # section 2, "Maximum keeper length: 3 keeps"
LATE_ROUND_COST_FLOOR = 8       # section 2, round 9+/undrafted -> Round 8
EARLIEST_ROUND = 1              # section 2, cannot escalate past Round 1
PREMIUM_ROUNDS = (1, 2)         # section 4, at most one keeper costing R1-R2
CHEAP_COST_THRESHOLD = 8        # section 4, a 3-keeper set needs one R8-or-later


# --- Cost -------------------------------------------------------------------


def base_cost(draft_round):
    """Section 2. The cost a player carries from the draft that acquired him.

    Rounds 1-8 cost their own round; round 9+ and undrafted both cost Round 8.
    """
    if draft_round is None:
        return LATE_ROUND_COST_FLOOR
    return min(draft_round, LATE_ROUND_COST_FLOOR)


@dataclass(frozen=True)
class CostResult:
    """What a player costs this year, and why he might not be keepable."""

    base_round: int
    times_kept_before: int
    cost_round: int | None
    keepable: bool
    reason: str = ''

    def __str__(self):
        return f'Round {self.cost_round}' if self.keepable else self.reason


def current_cost(roster_entry, times_kept_before):
    """Section 2. This year's cost round for a player, after escalation.

    First keep costs the base; each repeat keep costs one round earlier. Two
    independent stop conditions: escalating past Round 1, and the 3-keep cap.
    (A Round 8 base hits the keep cap at 8/7/6 without ever reaching Round 1;
    a Round 2 base runs out of rounds after two keeps. Both must be checked.)

    `roster_entry` must be the entry from the draft that started the chain --
    see resolve_current_cost(), which works that out for you.
    """
    base = base_cost(roster_entry.draft_round)

    if times_kept_before >= MAX_KEEPS_PER_PLAYER:
        return CostResult(
            base, times_kept_before, None, False,
            f'Already kept {times_kept_before} times (maximum is {MAX_KEEPS_PER_PLAYER}).',
        )

    cost = base - times_kept_before
    if cost < EARLIEST_ROUND:
        return CostResult(
            base, times_kept_before, None, False,
            'Keeper cost would escalate past Round 1.',
        )

    return CostResult(base, times_kept_before, cost, True)


def times_kept_before(player, season):
    """How many *consecutive* seasons this player has been kept, up to `season`.

    Counts backwards from the prior season and stops at the first gap: a player
    who goes unkept returns to the draft pool, and being drafted again starts a
    fresh chain at a fresh base cost.

    Keyed on the player, not the team -- section 2, "keep history and escalated
    cost follow the player" across trades.
    """
    from .models import KeeperSelection

    kept_years = set(
        KeeperSelection.objects
        .filter(roster_entry__player=player, season__year__lt=season.year)
        .values_list('season__year', flat=True)
    )

    count = 0
    year = season.year - 1
    while year in kept_years:
        count += 1
        year -= 1
    return count


def chain_start_entry(player, season, keeps):
    """The RosterEntry whose draft_round sets the base cost for this keep chain.

    With no prior keeps that is simply last season's entry. After N keeps the
    player has not been drafted for N years, so the drafted entry sits N+1
    seasons back -- and the intervening entries have draft_round=None, which
    would wrongly read as a Round 8 base.
    """
    from .models import RosterEntry

    return (
        RosterEntry.objects
        .filter(player=player, season__year=season.year - keeps - 1)
        .select_related('season')
        .first()
    )


def resolve_current_cost(roster_entry, season):
    """Current-year cost for a player, resolving keep history from the database.

    This is the function callers normally want; current_cost() is the pure
    arithmetic underneath it. For more than a handful of players use
    current_costs() instead -- this one costs two queries each.
    """
    keeps = times_kept_before(roster_entry.player, season)
    base_entry = chain_start_entry(roster_entry.player, season, keeps) or roster_entry
    return current_cost(base_entry, keeps)


def current_costs(roster_entries, season):
    """Bulk resolve_current_cost: {roster_entry.pk: CostResult}.

    Same answers as calling resolve_current_cost() in a loop, but in a fixed
    number of queries rather than two per player. The draft board prices every
    eligible player on all ten rosters at once, which is ~160 players -- the
    loop version would be several hundred queries per page load.
    """
    from .models import KeeperSelection, RosterEntry

    entries = list(roster_entries)
    if not entries:
        return {}

    player_ids = {e.player_id for e in entries}

    # Query 1: every prior keep for these players, in one go.
    kept_years = {}
    rows = (
        KeeperSelection.objects
        .filter(roster_entry__player_id__in=player_ids, season__year__lt=season.year)
        .values_list('roster_entry__player_id', 'season__year')
    )
    for player_id, year in rows:
        kept_years.setdefault(player_id, set()).add(year)

    # Same consecutive-chain walk as times_kept_before(), done in memory.
    keeps = {}
    for player_id in player_ids:
        years = kept_years.get(player_id, ())
        count = 0
        year = season.year - 1
        while year in years:
            count += 1
            year -= 1
        keeps[player_id] = count

    # Query 2: the chain-start entries, but only for players with a keep chain.
    wanted = {pid: season.year - n - 1 for pid, n in keeps.items() if n}
    base_entries = {}
    if wanted:
        candidates = (
            RosterEntry.objects
            .filter(player_id__in=wanted.keys(), season__year__in=set(wanted.values()))
            .select_related('season')
        )
        for candidate in candidates:
            if wanted.get(candidate.player_id) == candidate.season.year:
                base_entries[candidate.player_id] = candidate

    return {
        entry.pk: current_cost(
            base_entries.get(entry.player_id, entry), keeps[entry.player_id]
        )
        for entry in entries
    }


# --- Draft order ------------------------------------------------------------


def snake_overall(slot, round_number, team_count):
    """Section 6. Overall pick number for a draft slot in a snake draft.

    Odd rounds run 1..N, even rounds run back N..1, so slot 3 of 10 picks
    3rd in round 1 and 8th in round 2.
    """
    if round_number % 2 == 1:
        position_in_round = slot
    else:
        position_in_round = team_count - slot + 1
    return (round_number - 1) * team_count + position_in_round


# --- Pick forfeiture --------------------------------------------------------


# Why a keeper burned the pick it burned -- surfaced in the UI so a manager can
# see the reasoning, not just the outcome.
VIA_BASE = 'base cost'
VIA_COLLISION = 'collision'
VIA_MISSING = 'missing pick'


@dataclass(frozen=True)
class BurnAssignment:
    """One keeper's cost paid with one specific draft pick."""

    cost_round: int
    pick: object          # a DraftPick
    via: str              # VIA_BASE / VIA_COLLISION / VIA_MISSING
    entry: object = None  # the RosterEntry this pays for, when the caller knows it

    @property
    def walked(self):
        """True when the rules forced this earlier than the cost round."""
        return self.via != VIA_BASE

    def __str__(self):
        note = f' (walked from Round {self.cost_round}, {self.via})' if self.walked else ''
        return f'Round {self.pick.round} pick{note}'


@dataclass
class BurnResult:
    assignments: list = field(default_factory=list)
    impossible: list = field(default_factory=list)   # cost rounds that can't be paid

    @property
    def ok(self):
        return not self.impossible

    @property
    def picks(self):
        return [a.pick for a in self.assignments]


def _choose_pick(candidates, team, chosen_picks, round_number):
    """Section 3. Which pick to burn when a team owns more than one in a round.

    The manager may choose; absent a choice we default to the team's own
    original pick and keep the acquired one. `chosen_picks` maps a round number
    to a DraftPick (or its pk). An unavailable choice falls back to the default.
    """
    # Own pick first, then by pk -- deterministic regardless of query order.
    candidates = sorted(candidates, key=lambda p: (p.original_team_id != team.pk, p.pk or 0))

    if chosen_picks:
        wanted = chosen_picks.get(round_number)
        if wanted is not None:
            wanted_pk = getattr(wanted, 'pk', wanted)
            for candidate in candidates:
                if candidate.pk == wanted_pk:
                    return candidate

    return candidates[0]


def resolve_burned_picks(team, season, keeper_costs, chosen_picks=None):
    """Section 3. Work out which picks a set of keeper costs forfeits.

    Rules applied, in order, per keeper:
      * burn an owned, un-forfeited pick in the cost round;
      * if none is left there, walk to the next-earlier owned pick
        (this single walk covers both the missing-pick rule and the
        same-cost-round collision rule -- a collision just means the round was
        consumed by an earlier keeper in this same pass);
      * if nothing is owned at or before the cost round, that keeper is
        impossible. It is reported, not raised.

    Costs are processed in ascending round order (most expensive first) so the
    outcome does not depend on the order the commissioner typed them in.
    """
    from django.db.models import Q

    from .models import DraftPick

    # A pick counts as available if it is un-forfeited, OR if the only thing
    # forfeiting it is one of this team's own keepers for this same season --
    # which is precisely the set we are re-deriving. Without that second branch,
    # adding a second keeper would see the first keeper's pick as gone and
    # wrongly report the set unpayable.
    owned = list(
        DraftPick.objects
        .filter(season=season, current_team=team)
        .filter(Q(forfeited=False) | Q(burned_by__team=team, burned_by__season=season))
        .select_related('original_team')
        .distinct()
    )

    by_round = {}
    for pick in owned:
        by_round.setdefault(pick.round, []).append(pick)

    result = BurnResult()
    used = set()

    for cost in sorted(keeper_costs):
        # Why the walk happened, decided at the cost round itself: owning picks
        # there that are already spoken for is a collision; owning none at all
        # is the missing-pick rule.
        at_cost_round = by_round.get(cost, [])
        via_if_walked = VIA_COLLISION if at_cost_round else VIA_MISSING

        for round_number in range(cost, EARLIEST_ROUND - 1, -1):
            available = [p for p in by_round.get(round_number, []) if p.pk not in used]
            if not available:
                continue
            pick = _choose_pick(available, team, chosen_picks, round_number)
            used.add(pick.pk)
            result.assignments.append(BurnAssignment(
                cost_round=cost,
                pick=pick,
                via=VIA_BASE if round_number == cost else via_if_walked,
            ))
            break
        else:
            result.impossible.append(cost)

    return result


# --- Whole-set validation ---------------------------------------------------


@dataclass
class ValidationResult:
    valid: bool = True
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    burned_picks: list = field(default_factory=list)     # BurnAssignment objects
    costs: dict = field(default_factory=dict)            # roster_entry.pk -> CostResult

    def add_error(self, message):
        self.errors.append(message)
        self.valid = False


def validate_keeper_set(team, season, roster_entries, chosen_picks=None):
    """Sections 1, 3, 4 and 5. Is this whole set of keepers legal for this team?

    Takes the *complete* proposed set, not one player at a time, because almost
    every rule here is set-dependent: the composition limits compare keepers to
    each other, and which pick a keeper burns depends on what the other keepers
    already burned.
    """
    entries = list(roster_entries)
    result = ValidationResult()

    # Section 1 -- at most three keepers.
    if len(entries) > MAX_KEEPERS_PER_TEAM:
        result.add_error(
            f'{len(entries)} keepers selected; the limit is {MAX_KEEPERS_PER_TEAM}.'
        )

    payable = []      # (entry, cost_round) for keepers that got this far

    for entry in entries:
        label = entry.player.name

        # Section 5 -- eligibility. None means "commissioner has not reviewed
        # this player yet" and is treated as not-yet-eligible, not as a pass.
        if entry.eligible is None:
            result.add_error(f'{label}: eligibility has not been reviewed yet.')
        elif entry.eligible is False:
            note = f' ({entry.eligibility_note})' if entry.eligibility_note else ''
            result.add_error(f'{label}: not keeper-eligible{note}.')

        # Section 2 -- cost and escalation.
        cost = resolve_current_cost(entry, season)
        result.costs[entry.pk] = cost
        if not cost.keepable:
            result.add_error(f'{label}: {cost.reason}')
        else:
            payable.append((entry, cost.cost_round))

        if entry.team_id != team.pk:
            result.warnings.append(
                f'{label} finished {entry.season.year} on {entry.team.name}, '
                f'not {team.name} -- confirm the offseason trade.'
            )

    payable_costs = [cost_round for _, cost_round in payable]

    # Section 4 -- roster composition, judged on *current-year* cost.
    premium = [c for c in payable_costs if c in PREMIUM_ROUNDS]
    if len(premium) > 1:
        result.add_error(
            f'{len(premium)} keepers cost Rounds 1-2; only one is allowed.'
        )

    if len(entries) == MAX_KEEPERS_PER_TEAM and payable_costs:
        if not any(c >= CHEAP_COST_THRESHOLD for c in payable_costs):
            result.add_error(
                f'A 3-keeper set needs at least one keeper costing '
                f'Round {CHEAP_COST_THRESHOLD} or later.'
            )

    # Section 3 -- can the team actually pay?
    burn = resolve_burned_picks(team, season, payable_costs, chosen_picks)
    result.burned_picks = _attribute_assignments(burn.assignments, payable)
    for cost_round in burn.impossible:
        result.add_error(
            f'No pick owned in Round {cost_round} or any earlier round to pay '
            f'that keeper.'
        )

    return result


def _attribute_assignments(assignments, payable):
    """Tag each burn with the roster entry it pays for.

    resolve_burned_picks() works in bare cost rounds so it stays easy to test,
    so the entries are matched back here. Two keepers at the same cost round are
    genuinely interchangeable -- either attribution is correct -- so a greedy
    match on cost round is enough.
    """
    unmatched = list(payable)
    attributed = []

    for assignment in assignments:
        for pair in unmatched:
            if pair[1] == assignment.cost_round:
                attributed.append(replace(assignment, entry=pair[0]))
                unmatched.remove(pair)
                break
        else:
            attributed.append(assignment)

    return attributed


# --- Applying a validated set ----------------------------------------------


def recompute_team_selections(team, season, chosen_picks=None):
    """Re-derive cost_round, burned picks and forfeit flags for one team.

    Called after any KeeperSelection is added, changed or removed. It recomputes
    the *whole* team every time rather than patching one row, because burned
    picks are set-dependent -- adding a second Round 8 keeper changes which pick
    the first one burns. Returns the ValidationResult.
    """
    from .models import DraftPick, KeeperSelection

    selections = list(
        KeeperSelection.objects
        .filter(team=team, season=season)
        .select_related('roster_entry__player', 'roster_entry__team', 'roster_entry__season')
    )

    # Release every pick this team has burned this season before re-deriving.
    # Clearing by owner rather than by current selection matters: when a keeper
    # is deleted, the row linking it to its pick is already gone, and only a
    # blanket release hands that slot back.
    DraftPick.objects.filter(season=season, current_team=team, forfeited=True).update(forfeited=False)
    KeeperSelection.objects.filter(season=season, team=team).update(burned_pick=None)

    result = validate_keeper_set(
        team, season, [s.roster_entry for s in selections], chosen_picks
    )

    for selection in selections:
        cost = result.costs.get(selection.roster_entry_id)
        selection.cost_round = cost.cost_round if cost and cost.keepable else None

    # validate_keeper_set already tagged each burn with the entry it pays for.
    by_entry = {s.roster_entry_id: s for s in selections}
    for assignment in result.burned_picks:
        if assignment.entry is None:
            continue
        selection = by_entry.get(assignment.entry.pk)
        if selection is not None:
            selection.burned_pick = assignment.pick

    for selection in selections:
        selection.save(update_fields=['cost_round', 'burned_pick'])

    burned_pks = [a.pick.pk for a in result.burned_picks]
    DraftPick.objects.filter(pk__in=burned_pks).update(forfeited=True)

    return result
