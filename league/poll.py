"""Pure logic for the draft-time poll: labels, tallies, and the change stamp.

Named `poll` rather than `draft_poll` because views.py has a `draft_poll` view
function -- `from . import draft_poll` followed by `def draft_poll(...)` would
shadow the module with the function, and the failure would be baffling.

Nothing here touches the database or the request. Everything takes plain
values or model instances and returns plain values, which is what lets the
whole file be tested without a client, a login or a fixture.
"""

from datetime import timezone as datetime_timezone

from django.utils import timezone


def zone_label(moment):
    """The short name of the wall clock a time is being shown on: "PT", "ET".

    Every rendered draft time carries one. A poll that says "Sunday 7pm" and
    leaves you to guess whose seven is worse than useless, because everyone
    guesses their own.

    Derived from the zone rather than hardcoded, so it cannot drift out of step
    with settings.TIME_ZONE. Python names the zone by season -- PDT in August,
    PST in December -- and the season is not the point here, so the middle
    letter is dropped: "PT" is what a person writes and what a person reads.
    Anything that is not a three-letter D/S abbreviation (UTC, +05:30) is passed
    through untouched.
    """
    abbreviation = timezone.localtime(moment).strftime('%Z')
    if len(abbreviation) == 3 and abbreviation[1] in ('D', 'S'):
        return abbreviation[0] + abbreviation[2]
    return abbreviation


def format_slot(moment):
    """"Sun Aug 23, 7:00 PM PT" -- one candidate draft time, unambiguously.

    Hand-built rather than strftime'd end to end because the zero-stripped hour
    ("7:00", not "07:00") spells differently on Windows and Linux (%-I vs %#I),
    and this project is developed on one and deployed on the other.
    """
    local = timezone.localtime(moment)
    hour = local.hour % 12 or 12
    meridiem = 'AM' if local.hour < 12 else 'PM'
    return f'{local:%a %b} {local.day}, {hour}:{local.minute:02d} {meridiem} {zone_label(local)}'


def announcement(poll):
    """"Tue Sep 8, 5:00 PM PT (after the Cowboys game)", or None.

    What the board header says once the poll has actually decided something.
    Two conditions, both required:

      * a chosen_option -- there is nothing to announce otherwise;
      * closed -- a time the commissioner is still weighing is not an
        announcement, and the board is not where anyone should learn of one.

    Built with format_slot rather than a template's {{ option.starts_at }},
    which would print UTC and tell the league the draft is at 1 a.m. Wednesday.

    Takes the poll (or None) and reads only fields already in memory, so the
    caller decides how many queries this costs -- select_related the chosen
    option and it costs none.
    """
    if poll is None or poll.chosen_option is None or not poll.closed:
        return None

    option = poll.chosen_option
    when = format_slot(option.starts_at)
    return f'{when} ({option.label})' if option.label else when


# --- Answers ----------------------------------------------------------------

# A mark for each answer, so no cell in the grid is distinguishable by colour
# alone. Kept here rather than in the template because both the server render
# and draft_poll.js need it, and two copies would eventually disagree -- the
# page hands the map to the browser (see the json_script tag in the template)
# instead of the browser owning a second one.
GLYPHS = {'yes': '✓', 'maybe': '~', 'no': '✕'}


def glyph(answer):
    """The mark drawn for an answer; '' for a team that has not answered."""
    return GLYPHS.get(answer, '')


# --- Tallies ----------------------------------------------------------------


def tally(answers):
    """{'yes': n, 'maybe': n, 'no': n, 'score': ...} for one option's answers.

    `answers` is whatever iterable of answer strings the caller has; a team that
    has not answered contributes nothing, because "unanswered" is the absence of
    a row, not a fourth state to count.

    `score` is what ranks the columns: yes first, then yes+maybe as the
    tie-break. A time six people can definitely make beats one six people can
    probably make, and neither is beaten by a wall of maybes.
    """
    counts = {'yes': 0, 'maybe': 0, 'no': 0}
    for answer in answers:
        if answer in counts:
            counts[answer] += 1

    counts['available'] = counts['yes'] + counts['maybe']
    counts['score'] = (counts['yes'], counts['available'])
    return counts


def best_option_ids(tallies):
    """The option ids that tie for the best score, or () if nobody has voted.

    Every tied column is returned, not just the first: telling the commissioner
    there is one clear winner when there are three would be a lie in the one
    direction that matters.
    """
    scored = [(option_id, counts['score']) for option_id, counts in tallies.items()]
    live = [pair for pair in scored if pair[1] != (0, 0)]
    if not live:
        return ()

    best = max(score for _, score in live)
    return tuple(sorted(option_id for option_id, score in live if score == best))


def answers_by_option(votes):
    """{option_id: {team_id: answer}} from a flat iterable of votes."""
    grid = {}
    for vote in votes:
        grid.setdefault(vote.option_id, {})[vote.team_id] = vote.answer
    return grid


def tallies_for(option_ids, votes):
    """{option_id: tally} for every option, including the ones nobody answered."""
    answers = answers_by_option(votes)
    return {
        option_id: tally(answers.get(option_id, {}).values())
        for option_id in option_ids
    }


def stamp(moments, counts=()):
    """A change stamp: the newest `updated` seen, plus how many rows there are.

    The poll page's clients ask "has anything moved since X?", and this is X.
    ISO-8601 in UTC, so it compares correctly as a plain string and survives a
    JSON round trip without anyone parsing a date.

    The counts are in it because a timestamp alone cannot see a deletion.
    Clearing an answer removes its row, and if that row was not the newest one,
    max(updated) is completely unchanged -- so ten browsers would sit there
    showing an answer that no longer exists. The row count moves on every
    create and every delete, which closes that hole for one more aggregate on a
    table with at most a hundred rows in it.
    """
    live = [moment for moment in moments if moment is not None]
    # datetime's own UTC, not django.utils.timezone.utc -- that alias was
    # removed in Django 5.
    newest = max(live).astimezone(datetime_timezone.utc).isoformat() if live else ''
    return '|'.join([newest] + [str(count) for count in counts])
