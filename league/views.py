from pathlib import Path

import markdown
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.safestring import mark_safe

from .keeper_engine import resolve_current_cost, times_kept_before
from .models import Season, Team

RULES_PATH = Path(settings.BASE_DIR) / 'docs' / 'keeper_rules_v3.md'


def latest_roster_season():
    """The most recent season we have roster data for (2025 today).

    Deliberately not "the newest Season row": 2026 exists as a Season because it
    has a draft order and picks, but no rosters have been played yet.
    """
    return Season.objects.filter(roster_entries__isnull=False).distinct().first()


def keeper_season():
    """The season being drafted for -- the one after the latest roster season."""
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
    try:
        # 'team' is the related_name on Team.user, so this walks the one-to-one
        # backwards from User to Team.
        team = request.user.team
    except Team.DoesNotExist:
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
    try:
        team = request.user.team
    except Team.DoesNotExist:
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
