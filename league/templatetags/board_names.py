"""Template filters for the draft board.

Django finds custom tags and filters in `<app>/templatetags/<module>.py` and
loads them with `{% load board_names %}`. The app must be in INSTALLED_APPS and
the package needs its `__init__.py`, or the load tag fails.

Only one filter here, and it exists because the abbreviation a cell should print
cannot be decided from the player alone: whether "J. Chase" is unambiguous
depends on everyone ELSE on the board. So the view works the whole map out once
(league.names.short_names) and hands it to the template, and this filter is the
lookup -- which Django templates cannot otherwise do with a variable key.
"""

from django import template

register = template.Library()


@register.filter
def short_name(player, names):
    """Grid name for `player`, from the map the board view built.

    Falls back to the full name if the player is somehow not in the map, so a
    missed code path shows a long name rather than an empty cell.
    """
    if player is None:
        return ''
    return (names or {}).get(player.pk, player.name)
