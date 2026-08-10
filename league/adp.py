"""Matching FantasyPros player rows to our Player rows.

Pure functions, no Django imports beyond the model at call time -- name
normalization is fiddly enough to deserve its own tests.

The hard part is that two sources spell the same human differently:
"Travis Etienne Jr." vs "Travis Etienne", "Ken Walker III" vs "Kenneth Walker",
"Marvin Harrison Jr." vs "Marvin Harrison Jr". Defenses are worse -- our roster
CSV calls them "Eagles" while FantasyPros says "Philadelphia Eagles".
"""

import re
import unicodedata

# Generational suffixes carry no identity and are inconsistently present.
SUFFIXES = {'jr', 'sr', 'ii', 'iii', 'iv', 'v'}

# FantasyPros position codes -> ours.
POSITION_ALIASES = {
    'DST': 'DEF',
    'D/ST': 'DEF',
    'DEF': 'DEF',
    'PK': 'K',
    'K': 'K',
    'QB': 'QB',
    'RB': 'RB',
    'WR': 'WR',
    'TE': 'TE',
}


def normalize_name(name):
    """Lowercase, strip accents/punctuation/suffixes, collapse whitespace."""
    text = unicodedata.normalize('NFKD', name or '')
    text = text.encode('ascii', 'ignore').decode('ascii').lower()
    text = re.sub(r"[.'`,*]", '', text)      # D.K. -> dk, O'Neal -> oneal
    text = re.sub(r'[-/]', ' ', text)        # Smith-Njigba -> smith njigba
    tokens = [t for t in text.split() if t and t not in SUFFIXES]
    return ' '.join(tokens)


def defense_key(name):
    """Defenses are identified by their nickname alone.

    "Philadelphia Eagles" and "Eagles" both reduce to "eagles".
    """
    tokens = normalize_name(name).split()
    return tokens[-1] if tokens else ''


def normalize_position(position):
    """Map a source position code onto our Player.Position values.

    FantasyPros CSV exports put the positional rank in the same column --
    "RB1", "WR12", "DST3" -- so the trailing digits come off first.
    """
    code = re.sub(r'\d+$', '', (position or '').strip().upper())
    return POSITION_ALIASES.get(code, code)


def build_index(players):
    """{(position, key): [Player]} for fast lookup, including defense nicknames.

    Keys are collected in a set first: for a single-word defense like "Eagles"
    the normalized name and the nickname key are identical, and appending twice
    would make the player look ambiguous to find_player().
    """
    index = {}
    for player in players:
        keys = {(player.position, normalize_name(player.name))}
        if player.position == 'DEF':
            keys.add(('DEF', defense_key(player.name)))
        for key in keys:
            index.setdefault(key, []).append(player)
    return index


def find_player(index, name, position):
    """The single Player matching this source row, or None.

    Returns None on an ambiguous match (two players sharing a normalized name
    and position) rather than guessing -- the caller reports it for review.
    """
    position = normalize_position(position)

    keys = [(position, normalize_name(name))]
    if position == 'DEF':
        keys.append(('DEF', defense_key(name)))

    for key in keys:
        found = index.get(key)
        if found and len(found) == 1:
            return found[0]
        if found:
            return None      # ambiguous; better to report than to guess

    return None
