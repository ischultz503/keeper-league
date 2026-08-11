import json
from pathlib import Path

import markdown
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from . import draft_sim
from . import keeper_engine as engine
from . import names
from . import poll as poll_logic
from .forms import DraftPollAlternativeForm, FeedbackForm
from .models import (
    DraftPick,
    DraftPoll,
    DraftPollAlternative,
    DraftPollOption,
    DraftPollVote,
    DraftSlot,
    KeeperPrediction,
    KeeperSelection,
    Player,
    RosterEntry,
    Season,
    Team,
)

RULES_PATH = Path(settings.BASE_DIR) / 'docs' / 'keeper_rules_v3.md'

# Keeper costs never exceed Round 8 (rules section 2), so rounds 1-8 hold every
# cell a keeper can burn. The later rounds only matter for trades and the full
# draft, and are behind the "all rounds" toggle.
DEFAULT_VISIBLE_ROUNDS = 8


def latest_roster_season():
    """The most recent season we have roster data for (2025 today).

    Deliberately not "the newest Season row": 2026 exists as a Season because it
    has a draft order and picks, but no rosters have been played yet.
    """
    return Season.objects.filter(roster_entries__isnull=False).distinct().first()


def keeper_season():
    """The season being drafted for.

    Primarily "the newest season that has a draft order", since that is what
    defines a draft. Falls back to the season after the latest roster so keeper
    costs still resolve before the order has been entered.
    """
    with_draft_order = (
        Season.objects.filter(draft_slots__isnull=False).distinct().first()
    )
    if with_draft_order is not None:
        return with_draft_order

    roster_season = latest_roster_season()
    if roster_season is None:
        return None
    return Season.objects.filter(year=roster_season.year + 1).first()


# Rules section 6, "Transition rule -- 2026 draft only": the 2026 order is
# Yahoo's final 2025 standings reversed. That equivalence is what lets this app
# show 2025 standings without storing them -- and it holds for this one draft
# only. From 2027 the consolation bracket picks its own slots, so the order
# stops being a reversal of anything and the derivation below must not be
# trusted. It returns nothing for other years rather than a plausible lie.
TRANSITION_DRAFT_YEAR = 2026


def final_standings(roster_season):
    """{team_id: finishing position} for a completed season, or {} if unknown.

    Derived from the following season's draft slots: slot 1 belongs to the team
    that finished last, so a ten-team league maps slot 1 to 10th and slot 10 to
    the champion. See TRANSITION_DRAFT_YEAR for why this is sound for 2025 and
    must not be extended past it.
    """
    if roster_season is None or roster_season.year + 1 != TRANSITION_DRAFT_YEAR:
        return {}

    slots = list(
        DraftSlot.objects.filter(season__year=TRANSITION_DRAFT_YEAR).values_list('team_id', 'slot')
    )
    if not slots:
        return {}

    team_count = len(slots)
    return {team_id: team_count - slot + 1 for team_id, slot in slots}


@login_required
def league_overview(request):
    """Final standings for the last completed season, linking to each roster."""
    season = latest_roster_season()
    standings = final_standings(season)

    # Teams with a known finish first, in finishing order; anything unranked
    # falls to the bottom in the model's own alphabetical-by-owner order rather
    # than disappearing.
    teams = sorted(
        Team.objects.all(),
        key=lambda team: (standings.get(team.pk) is None, standings.get(team.pk) or 0),
    )
    for team in teams:
        team.rank = standings.get(team.pk)

    return render(
        request,
        'league/league_overview.html',
        {'teams': teams, 'season': season, 'ranked': bool(standings)},
    )


@login_required
def team_detail(request, pk=None):
    """One team's roster, its keeper costs, and a picker for the other nine.

    Reached three ways, all landing here: `/teams/3/` is a permalink,
    `/teams/?team=3` is what the dropdown submits (a plain GET form, so the
    picker needs no JavaScript), and bare `/teams/` is your own team, which is
    where the "My Team" tab points.

    This page absorbed the old My Keepers page. They listed the same roster,
    and one of them showed the cost that actually applies -- so the escalated
    cost, the keep count and eligibility live here now, for every team. None of
    it is private: it all derives from last season's public draft.
    """
    if pk is None and request.GET.get('team'):
        try:
            pk = int(request.GET['team'])
        except ValueError:
            pk = None                     # a hand-edited query, not a crash

    if pk is None:
        team = _own_team(request)
        if team is None:
            # A commissioner account with no team of its own has nothing to
            # default to; the standings are a sensible landing spot.
            return redirect('league_overview')
    else:
        team = get_object_or_404(Team, pk=pk)

    roster_season = latest_roster_season()
    season = keeper_season()

    # select_related follows the player FK in the same SQL query, so rendering
    # 16 rows costs 1 query instead of 17. Sort order (draft round ascending,
    # undrafted last, then name) comes from RosterEntry.Meta.ordering.
    entries = list(
        team.roster_entries.filter(season=roster_season).select_related('player', 'season')
    )

    # The engine does the thinking; the view only assembles rows. current_costs
    # prices the whole roster in a fixed number of queries rather than two per
    # player -- see the note on it in keeper_engine.
    costs = engine.current_costs(entries, season) if season else {}
    team_count = Team.objects.count()

    rows = [
        {
            'entry': entry,
            'cost': costs.get(entry.pk),
            'keeps': costs[entry.pk].times_kept_before if entry.pk in costs else 0,
            'pick': draft_pick_label(entry, team_count),
        }
        for entry in entries
    ]

    return render(
        request,
        'league/team_detail.html',
        {
            'team': team,
            'season': roster_season,
            'keeper_season': season,
            'rows': rows,
            'teams': Team.objects.all(),
            'is_own': _own_team(request) == team,
        },
    )


@login_required
def my_team(request):
    """Send the logged-in manager to their own team page."""
    team = _own_team(request)
    if team is None:
        # Superusers and any unlinked account land on the league page instead.
        return redirect('league_overview')
    return redirect('team_detail', pk=team.pk)


@login_required
def eligibility(request):
    """Every player who cannot be kept this year, best first.

    "Cannot be kept" is two states, not one: explicitly ineligible, and not yet
    reviewed. They are listed together because the engine treats them the same
    way -- validate_keeper_set refuses a keeper whose eligibility is None just
    as firmly as one marked False (rules section 5). Splitting them into
    separate pages would suggest a difference that does not exist at the
    declaration deadline.

    Sorted by ADP so the names that hurt appear first; a player nobody drafts
    being ineligible is not news.
    """
    season = latest_roster_season()

    entries = (
        RosterEntry.objects
        .filter(season=season)
        .exclude(eligible=True)
        .select_related('player', 'team')
    )

    # Sorted in Python rather than SQL: "no ADP sorts last" is the same rule
    # draft_sim uses, and doing it here keeps one definition of it.
    ordered = sorted(
        entries,
        key=lambda entry: (
            entry.player.adp is None,
            entry.player.adp if entry.player.adp is not None else 0.0,
            entry.player.name,
        ),
    )

    team_count = Team.objects.count()
    rows = [
        {'entry': entry, 'pick': draft_pick_label(entry, team_count)}
        for entry in ordered
    ]

    return render(
        request,
        'league/eligibility.html',
        {
            'season': season,
            'rows': rows,
            'blocked_count': sum(1 for entry in ordered if entry.eligible is False),
            'pending_count': sum(1 for entry in ordered if entry.eligible is None),
        },
    )


def draft_pick_label(entry, team_count):
    """"R5, Pick 7" for a roster entry, or "Undrafted".

    Presentation, not rules: the arithmetic lives in
    keeper_engine.position_from_overall, and this only decides how to say it.
    Falls back to the overall number when the two disagree, so bad source data
    shows up as an odd label rather than being quietly smoothed over.
    """
    if entry.draft_round is None:
        return 'Undrafted'

    position = engine.position_from_overall(
        entry.overall_pick, entry.draft_round, team_count
    )
    if position is None:
        if entry.overall_pick is None:
            return f'R{entry.draft_round}'
        return f'R{entry.draft_round}, #{entry.overall_pick} overall'

    return f'R{entry.draft_round}, Pick {position}'


def _grid_names():
    """{player id: the name to print in a board cell} -- see league.names.

    Priced over the WHOLE player table rather than just the cells the server is
    about to render, for one reason: the simulator paints players into this same
    grid from the browser, and a burned cell keeps its candidate list underneath
    the burn tag. Two surfaces, one board -- and a player who read "J. Chase" in
    one cell and "Ja'Marr Chase" in the next would look like two people.
    Scoring the whole table once is a superset of anything the board can show,
    so every cell agrees. It is one query of a few hundred narrow rows.
    """
    return names.short_names(Player.objects.only('id', 'name', 'position'))


def _own_team(request):
    """The logged-in manager's team, or None.

    Every keeper view derives the team from the session this way. A team id is
    never accepted from the client -- that is the whole access-control boundary
    for pre-reveal keeper data.
    """
    try:
        return request.user.team
    except Team.DoesNotExist:
        return None


@login_required
def board(request):
    """The draft board: rounds down the side, teams across in draft-slot order."""
    season = keeper_season()
    roster_season = latest_roster_season()

    if season is None:
        return render(request, 'league/board.html', {'season': None})

    slots = list(
        DraftSlot.objects.filter(season=season).select_related('team').order_by('slot')
    )
    picks = list(
        DraftPick.objects
        .filter(season=season)
        .select_related('original_team', 'current_team')
    )

    # Index picks so cell lookup is a dict hit rather than a query per cell.
    pick_map = {(p.round, p.original_team_id): p for p in picks}
    rounds = sorted({p.round for p in picks})

    revealed = season.keepers_revealed
    own_team = _own_team(request)

    # Post-reveal: what each burned cell actually paid for.
    kept_by_pick = {}
    kept_entry_ids = set()
    if revealed:
        for selection in KeeperSelection.objects.filter(season=season).select_related(
            'roster_entry__player', 'team', 'burned_pick'
        ):
            kept_entry_ids.add(selection.roster_entry_id)
            if selection.burned_pick_id:
                kept_by_pick[selection.burned_pick_id] = selection

    # Pre-reveal: who *could* be kept, placed in the cell that keeping them
    # would actually burn. For most players that is their cost round; for a
    # player whose team no longer owns that pick, the missing-pick rule shifts
    # it earlier (rules section 3), and placing them at cost round would point
    # at a slot the team cannot spend.
    #
    # Only the SOLO placement is shown. A second keeper's collision depends on
    # a set nobody outside that team knows, so it is not projected here.
    # Private predictions about other teams, run through the engine per team so
    # a rival's predicted SET resolves properly -- two guesses at the same cost
    # round collide and the second walks earlier, exactly as a real set would.
    #
    # Once keepers are revealed the real declarations own the board and guesses
    # stop being placed on it -- two competing truths in one grid would be
    # unreadable. What survives is the scorecard: which calls came off.
    if revealed:
        predicted_by_pick, team_warnings = {}, {}
        predicted_entry_ids = set(
            KeeperPrediction.objects
            .filter(user=request.user, season=season)
            .values_list('roster_entry_id', flat=True)
        )
    else:
        predicted_by_pick, predicted_entry_ids, team_warnings = _place_predictions(
            request.user, season
        )

    candidates = {}
    if not revealed and roster_season is not None:
        burn_targets = _solo_burn_targets_by_team(season, picks, slots)

        entries = list(
            RosterEntry.objects
            .filter(season=roster_season)
            .exclude(eligible=False)
            .select_related('player', 'team')
        )
        costs = engine.current_costs(entries, season)

        for entry in entries:
            cost = costs[entry.pk]
            if not cost.keepable:
                continue

            # A locked prediction is shown in its own cell, not offered again
            # as one possibility among many.
            if entry.pk in predicted_entry_ids:
                continue

            target = burn_targets.get(entry.team_id, {}).get(cost.cost_round)
            if target is None:
                # The team owns nothing at or before the cost round, so there
                # is no cell where this keep could be paid for.
                continue

            candidates.setdefault((target.round, entry.team_id), []).append({
                'entry': entry,
                'cost_round': cost.cost_round,
                'placed_round': target.round,
                'walked': target.round != cost.cost_round,
            })

    # Rounds 1-8 by default: that is where every keeper cost lands (rules
    # section 2 caps the base cost at Round 8), so it is the whole planning
    # surface, and eight rows of ten columns fits a screen without scrolling.
    show_all_rounds = request.GET.get('rounds') == 'all'
    visible_rounds = rounds if show_all_rounds else rounds[:DEFAULT_VISIBLE_ROUNDS]

    # Hang the warnings on the slot objects the header already loops over.
    # Django templates cannot look a dict up by a variable key, and this beats
    # inventing a template filter for one lookup.
    for slot in slots:
        slot.warnings = team_warnings.get(slot.team_id, [])

    team_count = len(slots)
    rows = []
    for round_number in visible_rounds:
        cells = []
        for slot in slots:
            pick = pick_map.get((round_number, slot.team_id))
            if pick is None:
                cells.append(None)
                continue

            cell_candidates = sorted(
                candidates.get((round_number, slot.team_id), []),
                key=lambda c: (c['cost_round'], c['entry'].player.name),
            )
            position_in_round = engine.snake_position_in_round(
                slot.slot, round_number, team_count
            )
            is_own = own_team is not None and slot.team_id == own_team.pk
            keeper = kept_by_pick.get(pick.pk)
            cells.append({
                'pick': pick,
                'team': slot.team,
                'is_own': is_own,
                'traded_to': pick.current_team if pick.is_traded else None,
                'keeper': keeper,
                # "You called it": this user predicted the player who really
                # was kept here. Costs nothing -- both sets are already loaded.
                'called': keeper is not None and keeper.roster_entry_id in predicted_entry_ids,
                'prediction': predicted_by_pick.get(pick.pk),
                # Own-team cells are never clickable: that plan stays in page
                # state, never in the database.
                'predictable': not is_own and not revealed,
                'candidates': cell_candidates[:3],
                'extra_candidates': cell_candidates[3:],
                # "3.4" -- round 3, fourth pick of that round.
                'label': f'{round_number}.{position_in_round}',
            })

        rows.append({
            'round': round_number,
            # Snake direction, rules section 6: odd rounds run left to right.
            'forward': round_number % 2 == 1,
            'cells': cells,
        })

    return render(request, 'league/board.html', {
        'season': season,
        'roster_season': roster_season,
        'slots': slots,
        'rows': rows,
        'revealed': revealed,
        'own_team': own_team,
        # Grid cells only. Every list with room to spare -- the sandbox, the
        # popover, the mock-draft chooser -- prints full names.
        'short_names': _grid_names(),
        'sandbox_players': _sandbox_players(own_team, roster_season, season),
        'show_all_rounds': show_all_rounds,
        'total_rounds': len(rounds),
        'visible_round_count': len(visible_rounds),
        # What the collapsed view shows. Distinct from visible_round_count,
        # which is however many rounds are on screen *right now* -- the link
        # back has to name where it goes, not where it already is.
        'default_round_count': min(DEFAULT_VISIBLE_ROUNDS, len(rounds)),
        'team_warnings': team_warnings,
        'prediction_count': len(predicted_entry_ids),
        # Post-reveal scorecard: how many of this user's calls were right.
        'called_count': len(predicted_entry_ids & kept_entry_ids),
        'keeper_count': len(kept_entry_ids),
    })


def _place_predictions(user, season):
    """Where this user's locked predictions land, and which sets are illegal.

    Returns (by_pick_id, entry_ids, warnings_by_team). Every query is filtered
    to `user` -- predictions are private, and nothing aggregates across users.
    """
    predictions = (
        KeeperPrediction.objects
        .filter(user=user, season=season)
        .select_related('roster_entry__player', 'roster_entry__team', 'roster_entry__season')
    )

    by_team = {}
    for prediction in predictions:
        by_team.setdefault(prediction.roster_entry.team, []).append(prediction.roster_entry)

    by_pick, entry_ids, warnings = {}, set(), {}

    for team, entries in by_team.items():
        entry_ids.update(entry.pk for entry in entries)

        # The whole predicted set at once, so collisions and missing picks
        # resolve against each other rather than one guess at a time.
        result = engine.validate_keeper_set(team, season, entries)

        for assignment in result.burned_picks:
            if assignment.entry is not None:
                by_pick[assignment.pick.pk] = {
                    'entry': assignment.entry,
                    'cost_round': assignment.cost_round,
                    'via': assignment.via,
                }

        # An illegal guess is still a guess -- flag the column, do not refuse
        # the lock.
        if not result.valid:
            warnings[team.pk] = result.errors

    return by_pick, entry_ids, warnings


def _solo_burn_targets_by_team(season, picks, slots):
    """{team_id: {cost_round: DraftPick}} for every team, from picks already in hand.

    The board has already fetched the season's picks, so grouping them here
    keeps the whole placement pass query-free.
    """
    # Forfeitures are deliberately ignored here, for two reasons. Semantically,
    # these cells answer "if this were your only keeper", and a solo keeper is
    # unaffected by other declarations. Practically, the commissioner enters all
    # ten teams' declarations before flipping the reveal switch -- if a burned
    # pick shifted a team's candidates during that window, the board would leak
    # that they had declared, which rules section 1 forbids.
    owned = {}
    for pick in picks:
        owned.setdefault(pick.current_team_id, []).append(pick)

    return {
        slot.team_id: engine.solo_burn_targets(
            slot.team, season, owned.get(slot.team_id, [])
        )
        for slot in slots
    }


def _sandbox_players(team, roster_season, season):
    """The logged-in manager's eligible players, priced for the sandbox list."""
    if team is None or roster_season is None or season is None:
        return []

    entries = list(
        team.roster_entries
        .filter(season=roster_season, eligible=True)
        .select_related('player')
    )
    costs = engine.current_costs(entries, season)
    # Same solo placement the board uses, so the sidebar and the grid agree.
    targets = engine.solo_burn_targets(team, season)

    players = []
    for entry in entries:
        cost = costs[entry.pk]
        if not cost.keepable:
            continue

        target = targets.get(cost.cost_round)
        players.append({
            'entry': entry,
            'cost': cost,
            'keeps': cost.times_kept_before,
            'burn_round': target.round if target else None,
            'walked': target is not None and target.round != cost.cost_round,
        })

    players.sort(key=lambda p: (p['cost'].cost_round, p['entry'].player.name))
    return players


@login_required
@require_POST
def keeper_preview(request):
    """Score a hypothetical keeper set for the logged-in manager's own team.

    Read-only by design: this endpoint never writes. Declarations reach the
    commissioner by text (rules section 1), so there is nothing to save here and
    nothing in the database for anyone to leak before the reveal.
    """
    team = _own_team(request)
    if team is None:
        return JsonResponse({'error': 'No team is linked to your login.'}, status=403)

    # Validate the request itself before worrying about league state, so a
    # malformed body always answers 400 rather than depending on the data.
    try:
        payload = json.loads(request.body or '{}')
        entry_ids = [int(value) for value in payload.get('entry_ids', [])]
    except (ValueError, TypeError, AttributeError):
        return JsonResponse({'error': 'Malformed request.'}, status=400)

    if len(entry_ids) > engine.MAX_KEEPERS_PER_TEAM:
        return JsonResponse(
            {'error': f'Pick at most {engine.MAX_KEEPERS_PER_TEAM} keepers.'}, status=400
        )

    season = keeper_season()
    roster_season = latest_roster_season()
    if season is None or roster_season is None:
        return JsonResponse({'error': 'No draft season is set up yet.'}, status=409)

    # Scope the lookup to this manager's own roster. Filtering rather than
    # trusting the ids is what stops someone POSTing another team's entry ids to
    # read their keeper costs before the reveal.
    entries = list(
        RosterEntry.objects
        .filter(pk__in=entry_ids, team=team, season=roster_season)
        .select_related('player', 'team', 'season')
    )
    if len(entries) != len(set(entry_ids)):
        return JsonResponse({'error': 'Those players are not on your roster.'}, status=403)

    result = engine.validate_keeper_set(team, season, entries)
    short = _grid_names()

    return JsonResponse({
        'valid': result.valid,
        'errors': result.errors,
        'warnings': result.warnings,
        'burned': [
            {
                'pick_id': assignment.pick.pk,
                'round': assignment.pick.round,
                'cost_round': assignment.cost_round,
                'via': assignment.via,
                # entry_id lets the board tie a burned cell back to the exact
                # checkbox that caused it, for the colour-matched swatches.
                'entry_id': assignment.entry.pk if assignment.entry else None,
                # Full name for the cell's tooltip, short one for the tag drawn
                # in the cell -- same split as _fill_json.
                'player': assignment.entry.player.name if assignment.entry else '',
                'short': (
                    short.get(assignment.entry.player_id, assignment.entry.player.name)
                    if assignment.entry else ''
                ),
            }
            for assignment in result.burned_picks
        ],
    })


@login_required
@require_POST
def simulate(request):
    """Project the rest of the draft from ADP, for this user only.

    POST-only, and the sandbox selection travels in the body. That is a
    deliberate choice, not a style preference: the sandbox IS the manager's real
    keeper plan, and the whole design keeps it out of the database (rules
    section 1). A GET like ?sandbox=12,45 would write that same plan into
    browser history, the Referer header of every outbound link, and gunicorn's
    access log in plaintext -- strictly worse than the database row we went out
    of our way not to write, because nobody thinks to guard a log file.

    Like keeper_preview, this view never writes anything.
    """
    team = _own_team(request)

    try:
        payload = json.loads(request.body or '{}')
        entry_ids = [int(value) for value in payload.get('entry_ids', [])]
        # {pick_id: player_id} of the picks the user has made so far in a mock
        # draft. Sent whole on every step: there is no server-side cursor, so
        # this dict IS the state, and replaying it is what "resume" means.
        manual_picks = {
            int(key): int(value) for key, value in (payload.get('manual_picks') or {}).items()
        }
        mock = bool(payload.get('mock'))
    except (ValueError, TypeError, AttributeError):
        return JsonResponse({'error': 'Malformed request.'}, status=400)

    if len(entry_ids) > engine.MAX_KEEPERS_PER_TEAM:
        return JsonResponse(
            {'error': f'Pick at most {engine.MAX_KEEPERS_PER_TEAM} keepers.'}, status=400
        )

    # A commissioner account with no team of its own can still simulate -- it
    # just has no sandbox to contribute, and no picks to make.
    if team is None and (entry_ids or mock or manual_picks):
        return JsonResponse({'error': 'No team is linked to your login.'}, status=403)

    season = keeper_season()
    roster_season = latest_roster_season()
    if season is None or roster_season is None:
        return JsonResponse({'error': 'No draft season is set up yet.'}, status=409)

    entries = []
    if entry_ids:
        entries = list(
            RosterEntry.objects
            .filter(pk__in=entry_ids, team=team, season=roster_season)
            .select_related('player', 'team', 'season')
        )
        # Same rule as keeper_preview: the ids are filtered to this manager's
        # own roster, and a short count is refused rather than quietly dropped.
        if len(entries) != len(set(entry_ids)):
            return JsonResponse({'error': 'Those players are not on your roster.'}, status=403)

    burned_pick_ids, taken_player_ids, roster_positions = _keeper_state(
        request.user, team, season, entries
    )

    slots = list(DraftSlot.objects.filter(season=season).select_related('team'))
    picks = list(DraftPick.objects.filter(season=season))
    pool = list(Player.objects.exclude(pk__in=taken_player_ids))

    # The team whose picks pause the replay is derived from the session, never
    # sent by the client -- the same rule as everywhere else on this board. You
    # can only mock-draft for yourself.
    try:
        run = draft_sim.run_sim(
            slots=slots,
            picks=picks,
            pool=pool,
            burned_pick_ids=burned_pick_ids,
            roster_positions=roster_positions,
            user_team_id=team.pk if (mock and team is not None) else None,
            manual_picks=manual_picks if mock else None,
            stop_at_next_user_pick=mock,
        )
    except draft_sim.SimError as problem:
        # An impossible choice: a player someone else already took, or a cell
        # that is not this manager's. Refused rather than silently dropped.
        return JsonResponse({'error': str(problem)}, status=400)

    players = {player.pk: player for player in pool}
    short = _grid_names()

    response = {
        'fills': [
            _fill_json(cell, players[cell.player_id], short)
            for cell in run.cells if cell.player_id is not None
        ],
        'burned': sorted(burned_pick_ids),
        'done': run.done,
        'your_pick': None,
    }

    if run.paused_at is not None:
        pending = run.paused_at
        response['your_pick'] = {
            'pick_id': pending.pick_id,
            'round': pending.round,
            'overall': pending.overall,
            # "3.4" -- the same label the cell carries on the board. It follows
            # the pick's ORIGINAL team, not mine: a pick acquired by trade keeps
            # the other manager's slot and column (rules section 7), so labelling
            # it from my own slot would name a cell somewhere else entirely.
            'label': _pick_label(pending.pick_id, picks, slots),
            'suggestions': [_player_json(player) for player in pending.suggestions],
        }
        # The whole remaining pool goes with the step, once, rather than a
        # second endpoint answering search-as-you-type. Two reasons: it is
        # small (a few hundred rows, ~15KB of JSON), and a search endpoint
        # would have to replay the entire draft again just to know what is
        # still available. Filtering a list the client already holds is instant
        # and adds no pick logic to the browser -- the caps that build
        # `suggestions` still come from draft_sim.
        available = run.available[:MAX_POOL_PAYLOAD]
        response['available'] = [_player_json(player) for player in available]
        response['available_count'] = len(run.available)

    return JsonResponse(response)


# A bound on the step payload. The real pool is ~350 players, so this never
# bites today; it exists so a future full-NFL import cannot turn one step into
# a megabyte of JSON.
MAX_POOL_PAYLOAD = 500


def _pick_label(pick_id, picks, slots):
    """The "3.4" label for a pick, from rows already in memory."""
    pick = next((p for p in picks if p.pk == pick_id), None)
    slot = next((s.slot for s in slots if pick and s.team_id == pick.original_team_id), None)
    if pick is None or slot is None:
        return ''
    position = engine.snake_position_in_round(slot, pick.round, len(slots))
    return f'{pick.round}.{position}'


def _player_json(player):
    # Full name: this feeds the mock-draft chooser and its search box, both of
    # which have room, and neither of which should make you squint at an
    # initial before spending a pick.
    return {
        'id': player.pk,
        'name': player.name,
        'position': player.position,
        'nfl_team': player.nfl_team,
        'adp': player.adp,
    }


def _fill_json(cell, player, short):
    # `player` is the full name (titles, announcements); `short` is what goes in
    # the grid cell. Both are computed here rather than in board.js -- the
    # abbreviation rules and the collision check live in league.names, and the
    # browser has no business owning a second copy of them.
    return {
        'pick_id': cell.pick_id,
        'player': player.name,
        'short': short.get(player.pk, player.name),
        'position': player.position,
        'nfl_team': player.nfl_team,
        'source': cell.source,
    }


def _keeper_state(user, team, season, sandbox_entries):
    """Everything the simulator needs to know about who is already spoken for.

    Returns (burned pick ids, player ids that must not be drafted, and the
    positions each team already holds). Three sources feed it, and which ones
    apply depends on the reveal:

      * the user's own sandbox selection -- page state, never stored;
      * the user's private predictions about rivals -- theirs alone;
      * real declarations, but ONLY once keepers are revealed.

    That last exclusion is the important one. DraftPick.forfeited flips as soon
    as the commissioner enters a declaration, days before the reveal. Feeding
    those forfeitures to the simulator would show every manager exactly which
    slots had already been paid for, which is the leak rules section 1 exists to
    prevent -- the same reason the board's candidate placement ignores
    forfeitures (see _solo_burn_targets_by_team).
    """
    burned, taken, positions = set(), set(), {}

    def record(team_id, entry):
        taken.add(entry.player_id)
        positions.setdefault(team_id, set()).add(entry.player.position)

    if season.keepers_revealed:
        selections = (
            KeeperSelection.objects
            .filter(season=season)
            .select_related('roster_entry__player')
        )
        for selection in selections:
            record(selection.team_id, selection.roster_entry)
            if selection.burned_pick_id:
                burned.add(selection.burned_pick_id)
    else:
        # Burns come from the same engine pass the board uses, so a cell the
        # board shows as locked is a cell the simulator treats as gone.
        predicted_by_pick, _, _ = _place_predictions(user, season)
        burned.update(predicted_by_pick)

        # The players are read separately rather than off those assignments: a
        # prediction the engine could not pay for burns nothing, but the player
        # is still predicted kept and must not turn up in someone's projection.
        # Filtered to this user; nothing here crosses accounts.
        predictions = (
            KeeperPrediction.objects
            .filter(user=user, season=season)
            .select_related('roster_entry__player')
        )
        for prediction in predictions:
            record(prediction.roster_entry.team_id, prediction.roster_entry)

    if sandbox_entries and team is not None:
        result = engine.validate_keeper_set(team, season, sandbox_entries)
        for assignment in result.burned_picks:
            burned.add(assignment.pick.pk)
        for entry in sandbox_entries:
            record(team.pk, entry)

    return burned, taken, positions


@login_required
@require_POST
def toggle_prediction(request):
    """Lock or unlock a private prediction about another team's keeper.

    A plain form POST rather than a JSON call: locking one player can move
    where that team's OTHER predictions land (a second keeper at the same cost
    round collides and walks earlier, rules section 3). Re-rendering server-side
    keeps the engine the only thing that decides placement, instead of
    reimplementing it in the browser.
    """
    lock_id = request.POST.get('lock')
    unlock_id = request.POST.get('unlock')
    season = keeper_season()
    roster_season = latest_roster_season()

    if season is None or roster_season is None:
        return redirect('board')

    own_team = _own_team(request)

    if lock_id or unlock_id:
        entry = (
            RosterEntry.objects
            .filter(pk=lock_id or unlock_id, season=roster_season)
            .select_related('team')
            .first()
        )

        # Predictions are about rivals. Refusing own-team locks here, not just
        # in the template, is what actually keeps a manager's real keeper plan
        # out of the database.
        if entry is not None and not (own_team and entry.team_id == own_team.pk):
            if lock_id:
                KeeperPrediction.objects.get_or_create(
                    user=request.user, season=season, roster_entry=entry
                )
            else:
                KeeperPrediction.objects.filter(
                    user=request.user, season=season, roster_entry=entry
                ).delete()

    # Never redirect somewhere off-site just because a form said so.
    target = request.POST.get('next') or ''
    if not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        target = reverse('board')

    return redirect(target)


@login_required
@require_POST
def reset_board(request):
    """Clear every lock this user has placed on the current season's board.

    Deletes only `request.user`'s predictions -- the filter is the whole access
    control, exactly as it is on the read side. Nobody can reset anyone else's
    board, and there is no id in the request to tamper with.

    The sandbox ticks are cleared by board.js at the same moment, because they
    live in the browser and never reach the server at all.
    """
    season = keeper_season()
    if season is not None:
        KeeperPrediction.objects.filter(user=request.user, season=season).delete()

    target = request.POST.get('next') or ''
    if not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        target = reverse('board')

    return redirect(target)


@login_required
def rules(request):
    """Render the rules doc as HTML, read fresh from disk on every request.

    Reading at request time (rather than at import) means editing
    docs/keeper_rules_v3.md updates the page immediately -- no restart, no
    deploy, no second copy of the rules to drift out of sync.
    """
    try:
        source = RULES_PATH.read_text(encoding='utf-8')
    except OSError:
        return render(request, 'league/rules.html', {'rules_html': None})

    # mark_safe is only appropriate because this file is repo-controlled and
    # authored by the commissioner -- never do this with user-submitted text.
    html = markdown.markdown(source, extensions=['tables', 'sane_lists'])

    return render(request, 'league/rules.html', {'rules_html': mark_safe(html)})


@login_required
def feedback(request):
    """The suggestion box: submit a note about the site, and see your own back.

    One view for both GET and POST, which is the conventional Django form view:
    a POST that validates saves and REDIRECTS (rather than rendering), so a
    refresh afterwards cannot submit the note twice. That is the Post/Redirect/
    Get pattern, and the redirect is what makes the browser's reload harmless.

    An invalid POST falls through to the same render as GET, with `form` now
    carrying the errors -- so the template needs no idea which one it is.
    """
    if request.method == 'POST':
        form = FeedbackForm(request.POST)
        if form.is_valid():
            note = form.save(commit=False)
            # commit=False gives us the unsaved instance so the fields the form
            # deliberately does not expose can be set from the request itself.
            note.user = request.user
            note.page = (request.POST.get('page') or '')[:200]
            note.save()
            messages.success(
                request, 'Thanks — your note is in. The commissioner sees these.'
            )
            return redirect('feedback')
    else:
        form = FeedbackForm()

    return render(
        request,
        'league/feedback.html',
        {
            'form': form,
            # Only ever your own. Other managers' suggestions are theirs, and a
            # shared list would turn a suggestion box into a comment section.
            'mine': request.user.feedback.all(),
            # The page they were on when they clicked the tab, so the hidden
            # field below has something to carry. HTTP_REFERER is unreliable in
            # general; here it is a hint on a note, and wrong is survivable.
            'from_page': request.META.get('HTTP_REFERER', ''),
        },
    )


# --- The draft-time poll ----------------------------------------------------
#
# A Doodle-style grid: candidate times across the top, the ten teams down the
# side. EVERY ANSWER IS PUBLIC -- each team sees every other team's answer, and
# the page says so out loud. That is the deliberate opposite of the keeper
# board, where predictions are private to one user (see KeeperPrediction), and
# it is right here for the same reason it is wrong there: scheduling only works
# if you can see who else can make it.
#
# Votes are keyed to Team, never to User. The team comes from the session via
# _own_team(); no view below ever accepts a team id from the client.


def _current_poll():
    """The poll for the season being drafted, or None.

    OneToOneField(Season) means there can only ever be one, which is why this
    can return a single object with no "which poll?" to answer.
    """
    season = keeper_season()
    if season is None:
        return None
    return (
        DraftPoll.objects
        .filter(season=season)
        .select_related('season', 'chosen_option')
        .first()
    )


def _poll_stamp(poll):
    """The poll's change stamp, in two aggregate queries.

    This is what makes polling cheap. Ten browsers asking every fifteen seconds
    is forty requests a minute; each one costs two aggregates over tables with
    tens of rows in them, and answers "nothing changed" without serialising the
    grid at all.
    """
    votes = DraftPollVote.objects.filter(option__poll=poll).aggregate(
        latest=Max('updated'), rows=Count('pk')
    )
    alternatives = poll.alternatives.aggregate(latest=Max('updated'), rows=Count('pk'))

    return poll_logic.stamp(
        [votes['latest'], alternatives['latest']],
        [votes['rows'], alternatives['rows']],
    )


def _poll_state(poll, teams=None):
    """Everything the grid draws, as plain JSON-safe values.

    Shared by the vote endpoint's reply and the polling endpoint, and by the
    page for its column totals, so none of them can disagree about what the
    grid says.
    """
    options = list(poll.options.all())
    option_ids = [option.pk for option in options]
    votes = list(DraftPollVote.objects.filter(option__poll=poll))

    tallies = poll_logic.tallies_for(option_ids, votes)
    best = poll_logic.best_option_ids(tallies)
    answers = poll_logic.answers_by_option(votes)

    if teams is None:
        teams = list(Team.objects.all())

    return {
        'closed': poll.closed,
        'stamp': _poll_stamp(poll),
        'options': [
            {
                'id': option.pk,
                'label': poll_logic.format_slot(option.starts_at),
                'note': option.label,
                'yes': tallies[option.pk]['yes'],
                'available': tallies[option.pk]['available'],
                'no': tallies[option.pk]['no'],
                'best': option.pk in best,
            }
            for option in options
        ],
        # {"<option id>": {"<team id>": "yes"}}. JSON object keys are always
        # strings, so the ids are stringified here rather than surprising the
        # browser with a number that arrives as "12".
        'answers': {
            str(option_id): {str(team_id): answer for team_id, answer in row.items()}
            for option_id, row in answers.items()
        },
        'teams': [{'id': team.pk, 'owner': team.owner_name} for team in teams],
        'alternatives': [
            {'team': note.team.owner_name, 'text': note.text}
            for note in poll.alternatives.select_related('team').all()
        ],
    }


@login_required
def draft_poll(request):
    """The draft-time poll: a public grid of who can make which candidate time.

    Public BY DESIGN. Unlike KeeperPrediction -- private to one user, and read
    back filtered by request.user -- every answer here is visible to every
    manager, and the page says so in as many words. A scheduling grid you
    cannot see the other rows of is just a form.

    The GET renders the whole grid server-side, so the page is complete before
    any JavaScript runs. The POST is the "when else?" note, which is a plain
    form and a redirect (Post/Redirect/Get): it is a paragraph someone writes
    once, not a control they toggle, so the JSON machinery the vote cells use
    would buy it nothing.
    """
    poll = _current_poll()
    if poll is None:
        return render(request, 'league/draft_poll.html', {'poll': None})

    own_team = _own_team(request)
    teams = list(Team.objects.all())
    mine = (
        DraftPollAlternative.objects.filter(poll=poll, team=own_team).first()
        if own_team else None
    )

    if request.method == 'POST':
        # Both refusals are enforced HERE, not merely by hiding the form. A
        # hidden control is a suggestion; this is the part that is load-bearing.
        if own_team is None:
            return HttpResponseForbidden('No team is linked to your login.')
        if poll.closed:
            return HttpResponseForbidden('This poll is closed.')

        # instance=mine is what makes this replace rather than append: with an
        # existing row bound, ModelForm.save() issues an UPDATE. Someone
        # revising "weeknights after 8" should not leave the commissioner a
        # trail of contradictions to reconcile.
        form = DraftPollAlternativeForm(request.POST, instance=mine)
        if form.is_valid():
            note = form.save(commit=False)
            note.poll = poll
            note.team = own_team
            note.save()
            messages.success(request, 'Noted -- the commissioner sees these.')
            return redirect('draft_poll')
    else:
        form = DraftPollAlternativeForm(instance=mine)

    state = _poll_state(poll, teams)
    options = list(poll.options.all())
    answers = poll_logic.answers_by_option(
        DraftPollVote.objects.filter(option__poll=poll)
    )

    # Rows are assembled here rather than in the template because a Django
    # template cannot look a dict up by a variable key -- {{ answers[o.id] }}
    # is not something it can say.
    labels = dict(DraftPollVote.Answer.choices)
    rows = []
    for team in teams:
        cells = []
        for option in options:
            answer = answers.get(option.pk, {}).get(team.pk, '')
            cells.append({
                'option': option,
                'answer': answer,
                'glyph': poll_logic.glyph(answer),
                # Spelled out for screen readers, since the glyph is decorative.
                'label': labels.get(answer, 'no answer yet'),
            })
        rows.append({
            'team': team,
            'is_own': own_team is not None and team.pk == own_team.pk,
            'cells': cells,
        })

    columns = [
        {
            'option': option,
            'label': poll_logic.format_slot(option.starts_at),
            'yes': column['yes'],
            'available': column['available'],
            'best': column['best'],
        }
        for option, column in zip(options, state['options'])
    ]

    return render(request, 'league/draft_poll.html', {
        'poll': poll,
        'own_team': own_team,
        'columns': columns,
        'rows': rows,
        'form': form,
        'alternatives': poll.alternatives.select_related('team').all(),
        'stamp': state['stamp'],
        'chosen_label': (
            poll_logic.format_slot(poll.chosen_option.starts_at)
            if poll.chosen_option else ''
        ),
        # The three answers come from the model's own TextChoices, so the
        # buttons and the database cannot drift apart.
        'answer_choices': [
            {'value': value, 'label': label, 'glyph': poll_logic.glyph(value)}
            for value, label in DraftPollVote.Answer.choices
        ],
        # Handed to draft_poll.js through a json_script tag rather than
        # duplicated in the JavaScript, so there is one definition of what an
        # answer looks like. Also what the read-only cells are drawn from.
        'answer_glyphs': poll_logic.GLYPHS,
        'answer_labels': dict(DraftPollVote.Answer.choices),
    })


@login_required
@require_POST
def draft_poll_vote(request):
    """Record (or clear) the logged-in manager's own team's answer.

    The team is derived from the session and never read from the request body.
    The option id is checked to belong to THIS season's poll, so a stale or
    hand-edited id cannot write into another poll's grid.

    Re-sending the answer already stored clears it back to unanswered, which is
    what makes a mis-click fixable. "Unanswered" is the absence of a row, so
    clearing means deleting one -- there is no fourth answer value.
    """
    team = _own_team(request)
    if team is None:
        return JsonResponse({'error': 'No team is linked to your login.'}, status=403)

    try:
        payload = json.loads(request.body or '{}')
        option_id = int(payload['option_id'])
        answer = str(payload['answer'])
    except (ValueError, TypeError, KeyError, AttributeError):
        return JsonResponse({'error': 'Malformed request.'}, status=400)

    if answer not in DraftPollVote.Answer.values:
        return JsonResponse({'error': 'That is not one of the answers.'}, status=400)

    poll = _current_poll()
    if poll is None:
        return JsonResponse({'error': 'There is no draft poll yet.'}, status=409)
    if poll.closed:
        return JsonResponse({'error': 'This poll is closed.'}, status=403)

    # Scoping the lookup to this poll IS the check: an id from another poll or
    # another season simply is not found.
    option = DraftPollOption.objects.filter(pk=option_id, poll=poll).first()
    if option is None:
        return JsonResponse({'error': 'That is not a time on this poll.'}, status=404)

    existing = DraftPollVote.objects.filter(option=option, team=team).first()
    if existing is not None and existing.answer == answer:
        existing.delete()
        mine = ''
    else:
        # update_or_create is one UPDATE or one INSERT, and it works with the
        # (option, team) unique constraint rather than racing against it.
        DraftPollVote.objects.update_or_create(
            option=option, team=team, defaults={'answer': answer}
        )
        mine = answer

    state = _poll_state(poll)
    return JsonResponse({
        'option_id': option.pk,
        'answer': mine,
        'options': state['options'],
        'stamp': state['stamp'],
    })


@login_required
def draft_poll_state(request):
    """Has anything moved since the stamp the client holds?

    The common answer is no, and answering it costs two aggregates and no
    serialisation at all. Only when the stamp has actually moved does this
    build the grid. See _poll_stamp for why the stamp counts rows as well as
    timestamps.
    """
    poll = _current_poll()
    if poll is None:
        return JsonResponse({'error': 'There is no draft poll yet.'}, status=409)

    current = _poll_stamp(poll)
    if request.GET.get('since') == current:
        return JsonResponse({'changed': False, 'stamp': current})

    state = _poll_state(poll)
    state['changed'] = True
    return JsonResponse(state)
