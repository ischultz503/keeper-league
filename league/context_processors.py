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


def nav(request):
    """The season the navigation labels itself with.

    base.html shows a "2025 Standings" tab, and hardcoding the year there would
    quietly become a lie next autumn. One indexed query on a ten-row table, on a
    ten-user site, is a fair price for a label that stays true.
    """
    return {
        'nav_roster_season': (
            Season.objects.filter(roster_entries__isnull=False).distinct().first()
        ),
    }
