"""Short player names for the draft board grid.

The grid is ten columns wide on one screen, so a cell has room for a name only
if the name is short: "J. Chase", not "Ja'Marr Chase". Buying that width back is
what lets the name itself be set at a readable size.

Only the GRID abbreviates. The sandbox sidebar, the prediction popover and the
mock-draft chooser all keep full names -- they are lists with room to spare, and
picking the wrong player out of one of them costs far more than the space saved.

Pure functions, no Django imports: this is presentation logic with fiddly edge
cases, which makes it worth testing on its own (see NameAbbreviationTests).
"""

# A defense's "name" is a city and/or a nickname, never a person's -- so the
# first-initial rule would produce "G. Bay Packers". Our data spells them both
# ways ("Eagles" and "Green Bay Packers"), and both must survive untouched.
DEFENSE = 'DEF'


def abbreviate(name, position=''):
    """"Ja'Marr Chase" -> "J. Chase". Everything after the first word survives.

    Keeping the whole tail is what handles the awkward cases without a list of
    exceptions in it:

        "Travis Etienne Jr."  -> "T. Etienne Jr."     (suffix kept)
        "Amon-Ra St. Brown"   -> "A. St. Brown"       (two-part surname kept)
        "Eagles"              -> "Eagles"             (nothing to abbreviate)

    A first name that is ALREADY initials is left alone: "A.J. Brown" stays
    "A.J. Brown" rather than collapsing to "A. Brown". Those players are known
    by the initials, and the pair is barely longer than one letter.
    """
    text = (name or '').strip()

    if position == DEFENSE:
        return text

    first, separator, rest = text.partition(' ')
    rest = rest.strip()
    if not separator or not rest:
        # One word: a defense nickname, or a name we have no business splitting.
        return text

    if '.' in first:
        return f'{first} {rest}'

    return f'{first[0]}. {rest}'


def short_names(players):
    """{player id: the name to print in a grid cell} for a set of players.

    Abbreviates via `abbreviate`, then undoes it for anyone it would make
    ambiguous. Two different people who shorten to the same thing at the same
    position -- say a "J. Williams" WR and another "J. Williams" WR -- both keep
    their full first names, because on a board full of trade decisions a name
    that could be either player is worse than a name that is merely long.

    Position is part of the comparison on purpose: the cell prints "WR - CIN"
    under the name, so a WR and an RB who shorten alike are already told apart.
    """
    rows = [(player.pk, player.name, player.position) for player in players]
    shortened = {pk: abbreviate(name, position) for pk, name, position in rows}

    # How many DISTINCT people land on each (short name, position). Distinct
    # matters: the same player arriving twice is not a collision.
    claimants = {}
    for pk, name, position in rows:
        claimants.setdefault((shortened[pk], position), set()).add(name)

    return {
        pk: (name if len(claimants[(shortened[pk], position)]) > 1 else shortened[pk])
        for pk, name, position in rows
    }
