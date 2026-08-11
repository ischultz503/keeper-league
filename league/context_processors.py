"""Extra values every template gets, without each view having to pass them.

A context processor is a function taking the request and returning a dict; the
names in that dict are merged into the context of every template rendered
through the standard machinery. Registered in settings.TEMPLATES under
OPTIONS.context_processors -- `request`, `auth` and `messages` in that same list
are Django's own, and are how `{{ user }}` reaches base.html.

Use them sparingly: this runs on every single page, so anything expensive here
is expensive everywhere.
"""

from .models import Season

# Which menu entry a given URL name lights up. Several routes share one entry:
# `My Team` is the same tab whether you arrived via /my-team/, the bare
# /teams/ switch, or a permalink to somebody's roster.
NAV_KEYS = {
    'board': 'board',
    'rules': 'rules',
    'my_team': 'my_team',
    'team_switch': 'my_team',
    'team_detail': 'my_team',
    'league_overview': 'standings',
    'eligibility': 'eligibility',
    'feedback': 'feedback',
}


def nav_key(url_name):
    """The menu entry a URL name belongs to, or '' for pages with no entry.

    A plain dict lookup, kept as a function so it can be tested without a
    request and so the template needs one comparison instead of eight.
    """
    return NAV_KEYS.get(url_name or '', '')


def nav(request):
    """What the navigation needs to label and orient itself.

    `nav_roster_season` is the year on the Standings entry: hardcoding it there
    would quietly become a lie next autumn. One indexed query on a ten-row
    table, on a ten-user site, is a fair price for a label that stays true.

    `nav_current` is which entry to mark with aria-current. It matters more now
    than it did: the links live behind a menu, so "where am I" can no longer be
    answered by glancing at the header.

    request.resolver_match is filled in by Django *after* URL resolution and
    before the view runs, so it is always there by render time -- except on a
    404/500 handled before resolution, hence the getattr.
    """
    match = getattr(request, 'resolver_match', None)

    return {
        'nav_roster_season': (
            Season.objects.filter(roster_entries__isnull=False).distinct().first()
        ),
        'nav_current': nav_key(match.url_name if match else ''),
    }
