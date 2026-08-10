import json
from pathlib import Path

import markdown
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from . import keeper_engine as engine
from .keeper_engine import resolve_current_cost, times_kept_before
from .models import DraftPick, DraftSlot, KeeperSelection, RosterEntry, Season, Team

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


@login_required
def league_overview(request):
    """Landing page: every team in the league, linking to its roster."""
    teams = Team.objects.all()
    return render(
        request,
        'league/league_overview.html',
        {'teams': teams, 'season': latest_roster_season()},
    )


@login_required
def team_detail(request, pk):
    """One team's full roster for the latest season, with base keeper costs."""
    team = get_object_or_404(Team, pk=pk)
    season = latest_roster_season()

    # select_related follows the player FK in the same SQL query, so rendering
    # 16 rows costs 1 query instead of 17. Sort order (draft round ascending,
    # undrafted last, then name) comes from RosterEntry.Meta.ordering.
    entries = team.roster_entries.filter(season=season).select_related('player')

    return render(
        request,
        'league/team_detail.html',
        {'team': team, 'season': season, 'entries': entries},
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
def my_keepers(request):
    """Read-only keeper planning for the logged-in manager.

    Declarations are NOT made here -- managers text the commissioner (rules
    section 1). This page exists so they know what each player would cost
    before they do.
    """
    team = _own_team(request)
    if team is None:
        return redirect('league_overview')

    roster_season = latest_roster_season()
    season = keeper_season()

    entries = (
        team.roster_entries
        .filter(season=roster_season)
        .select_related('player', 'season')
    )

    # The engine does the thinking; the view only assembles rows for the
    # template. No keeper rules live in this file or in the template.
    rows = []
    for entry in entries:
        rows.append({
            'entry': entry,
            'cost': resolve_current_cost(entry, season) if season else None,
            'keeps': times_kept_before(entry.player, season) if season else 0,
        })

    return render(
        request,
        'league/my_keepers.html',
        {
            'team': team,
            'roster_season': roster_season,
            'season': season,
            'rows': rows,
        },
    )


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
    if revealed:
        for selection in KeeperSelection.objects.filter(season=season).select_related(
            'roster_entry__player', 'team', 'burned_pick'
        ):
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
            cells.append({
                'pick': pick,
                'team': slot.team,
                'is_own': own_team is not None and slot.team_id == own_team.pk,
                'traded_to': pick.current_team if pick.is_traded else None,
                'keeper': kept_by_pick.get(pick.pk),
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
        'sandbox_players': _sandbox_players(own_team, roster_season, season),
        'show_all_rounds': show_all_rounds,
        'total_rounds': len(rounds),
        'visible_round_count': len(visible_rounds),
    })


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
                'player': assignment.entry.player.name if assignment.entry else '',
            }
            for assignment in result.burned_picks
        ],
    })


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
