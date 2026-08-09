from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .models import Season, Team


def current_season():
    """The most recent season on record. Season.Meta.ordering is ['-year']."""
    return Season.objects.first()


@login_required
def league_overview(request):
    """Landing page: every team in the league, linking to its roster."""
    teams = Team.objects.all()
    return render(
        request,
        'league/league_overview.html',
        {'teams': teams, 'season': current_season()},
    )


@login_required
def team_detail(request, pk):
    """One team's full roster for the current season, with base keeper costs."""
    team = get_object_or_404(Team, pk=pk)
    season = current_season()

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
