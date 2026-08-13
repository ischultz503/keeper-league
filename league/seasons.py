"""Which season are we talking about?

"The season being drafted" and "the last season we have rosters for" are two
questions this app asks constantly, and from every layer of it: the views, the
management commands, the admin, and now the navigation's context processor.

They lived in views.py, which meant everything else had to import from views to
ask them -- and views sit at the TOP of the stack, not in the library layer. A
context processor importing a view module is backwards; so is a management
command doing it (`seed_rules_poll` did exactly that). Both now import from
here, and nothing has to reach upwards.

Plain functions over the ORM: no request, no rendering, no templates, testable
on their own, the same shape as poll.py and keeper_engine.py.
"""

from .models import Season


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
