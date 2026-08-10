"""Set keeper eligibility on the 2025 roster from a reviewed CSV.

Rules section 5: a player is keeper-eligible if he started 4+ weeks OR was
rostered 9+ weeks. That determination is made outside the app (from Yahoo
weekly rosters) and imported here.

    python manage.py import_eligibility                     # dry run, the default
    python manage.py import_eligibility --apply             # actually write

Dry-run by default is deliberate: this rewrites the eligible flag on every
roster row at once, and eligibility is what gates a keeper from being declared.
Seeing the diff before it lands is worth one extra flag.

Expected columns:
    Owner, Team, Player_Name, Player_Position, weeks_started, weeks_rostered,
    eligible, reason, source
"""

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from league.adp import defense_key, normalize_name, normalize_position
from league.models import RosterEntry

DEFAULT_CSV = Path('data/processed/eligibility_2025.csv')
DEFAULT_SEASON = 2025
REQUIRED_COLUMNS = ['Player_Name', 'Player_Position', 'eligible', 'reason']

TRUE_VALUES = {'yes', 'y', 'true', '1'}
FALSE_VALUES = {'no', 'n', 'false', '0'}


def parse_eligible(value):
    """'yes' -> True, 'NO' -> False, anything else -> None (reported, not guessed)."""
    text = (value or '').strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def build_entry_index(entries):
    """{(position, key): [RosterEntry]}, mirroring league.adp.build_index.

    Reuses the same normalization the ADP importer uses -- suffixes, accents and
    punctuation stripped, defenses keyed on their nickname -- so "Travis Etienne
    Jr." and "Travis Etienne" land on the same roster row.
    """
    index = {}
    for entry in entries:
        player = entry.player
        keys = {(player.position, normalize_name(player.name))}
        if player.position == 'DEF':
            keys.add(('DEF', defense_key(player.name)))
        for key in keys:
            index.setdefault(key, []).append(entry)
    return index


def find_entry(index, name, position):
    """The single roster entry this row refers to, or None if unknown/ambiguous."""
    position = normalize_position(position)
    keys = [(position, normalize_name(name))]
    if position == 'DEF':
        keys.append(('DEF', defense_key(name)))

    for key in keys:
        found = index.get(key)
        if found:
            return found[0] if len(found) == 1 else None
    return None


class Command(BaseCommand):
    help = 'Import keeper eligibility for a season from a reviewed CSV.'

    def add_arguments(self, parser):
        parser.add_argument('--csv', default=str(DEFAULT_CSV))
        parser.add_argument('--season', type=int, default=DEFAULT_SEASON)
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Write the changes. Without this the command only reports them.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        path = Path(options['csv'])
        if not path.exists():
            raise CommandError(f'CSV not found: {path}')

        with path.open(newline='', encoding='utf-8-sig') as handle:
            rows = list(csv.DictReader(handle))

        if not rows:
            raise CommandError(f'{path} has no rows.')

        missing = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
        if missing:
            raise CommandError(f'CSV is missing required columns: {", ".join(missing)}')

        entries = list(
            RosterEntry.objects
            .filter(season__year=options['season'])
            .select_related('player', 'team', 'season')
        )
        if not entries:
            raise CommandError(
                f'No roster entries for {options["season"]}. Run import_rosters first.'
            )

        index = build_entry_index(entries)
        self.process(rows, index, entries, options['season'], options['apply'])

    def process(self, rows, index, entries, year, apply_changes):
        changes, unchanged, unmatched, unparsed, team_mismatch = [], 0, [], [], []
        seen = set()

        for row in rows:
            name = (row.get('Player_Name') or '').strip()
            position = (row.get('Player_Position') or '').strip()
            eligible = parse_eligible(row.get('eligible'))
            note = (row.get('reason') or '').strip()

            if eligible is None:
                unparsed.append(f'{name} ({position}): eligible={row.get("eligible")!r}')
                continue

            entry = find_entry(index, name, position)
            if entry is None:
                unmatched.append(f'{name} ({position})')
                continue

            seen.add(entry.pk)

            # The CSV carries Owner too; disagreeing with the database means one
            # of the two is stale, which is worth knowing before trusting either.
            owner = (row.get('Owner') or '').strip()
            if owner and owner != entry.team.owner_name:
                team_mismatch.append(
                    f'{name}: CSV says {owner}, database says {entry.team.owner_name}'
                )

            if entry.eligible == eligible and entry.eligibility_note == note[:100]:
                unchanged += 1
                continue

            changes.append((entry, eligible, note[:100]))

        if apply_changes:
            for entry, eligible, note in changes:
                entry.eligible = eligible
                entry.eligibility_note = note
            RosterEntry.objects.bulk_update(
                [entry for entry, _, _ in changes], ['eligible', 'eligibility_note']
            )

        uncovered = [e for e in entries if e.pk not in seen]
        self.report(
            rows, changes, unchanged, unmatched, unparsed,
            team_mismatch, uncovered, year, apply_changes,
        )

    def report(self, rows, changes, unchanged, unmatched, unparsed,
               team_mismatch, uncovered, year, apply_changes):
        becoming_false = sum(1 for _, eligible, _ in changes if eligible is False)
        becoming_true = sum(1 for _, eligible, _ in changes if eligible is True)

        self.stdout.write(
            f'{len(rows)} CSV rows | to change: {len(changes)} '
            f'({becoming_true} -> eligible, {becoming_false} -> not eligible) | '
            f'already correct: {unchanged}'
        )

        if changes:
            self.stdout.write('')
            self.stdout.write('Changes:')
            for entry, eligible, note in sorted(
                changes, key=lambda c: (c[1], c[0].player.name)
            ):
                was = {True: 'eligible', False: 'not eligible'}.get(entry.eligible, 'unset')
                now = 'eligible' if eligible else 'NOT eligible'
                self.stdout.write(
                    f'    {entry.player.name:<24} {entry.player.position:<4} '
                    f'{entry.team.owner_name:<9} {was} -> {now}   {note}'
                )

        for label, items in [
            ('CSV rows matching no roster entry', unmatched),
            ('CSV rows with an unreadable eligible value', unparsed),
            (f'{year} roster entries absent from the CSV (left untouched)',
             [f'{e.player.name} ({e.player.position})' for e in uncovered]),
            ('rows whose owner disagrees with the database', team_mismatch),
        ]:
            if items:
                self.stdout.write('')
                self.stdout.write(self.style.WARNING(f'{len(items)} {label}:'))
                for item in items[:25]:
                    self.stdout.write(f'    {item}')
                if len(items) > 25:
                    self.stdout.write(f'    ... and {len(items) - 25} more')

        self.stdout.write('')
        if apply_changes:
            self.stdout.write(self.style.SUCCESS(f'Applied {len(changes)} change(s).'))
        else:
            self.stdout.write(self.style.WARNING(
                'Dry run -- nothing written. Re-run with --apply to save.'
            ))
