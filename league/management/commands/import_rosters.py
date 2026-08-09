"""Import a tidy roster CSV into Season / Team / Player / RosterEntry.

Idempotent: running it twice produces the same database. Re-running after the
CSV changes updates the existing rows rather than duplicating them, and prunes
roster entries for that season that are no longer in the file.

    python manage.py import_rosters
    python manage.py import_rosters --csv data/processed/rosters_2026.csv --season 2026
"""

from pathlib import Path

import pandas as pd
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from league.models import Player, RosterEntry, Season, Team

DEFAULT_CSV = Path('data/processed/rosters_2025.csv')
DEFAULT_SEASON = 2025
REQUIRED_COLUMNS = ['Team', 'Owner', 'Player_Name', 'Player_Position', 'round', 'overall_pick']


def _optional_int(value):
    """CSV numeric columns arrive as floats with NaN for blanks -> int or None."""
    if pd.isna(value):
        return None
    return int(value)


class Command(BaseCommand):
    help = 'Import rosters from a tidy CSV into the database (idempotent).'

    def add_arguments(self, parser):
        parser.add_argument('--csv', default=str(DEFAULT_CSV), help='Path to the roster CSV.')
        parser.add_argument('--season', type=int, default=DEFAULT_SEASON, help='Season year.')

    # transaction.atomic means the whole import commits or none of it does --
    # a malformed row halfway through can't leave a half-loaded database.
    @transaction.atomic
    def handle(self, *args, **options):
        csv_path = Path(options['csv'])
        if not csv_path.exists():
            raise CommandError(f'CSV not found: {csv_path}')

        df = pd.read_csv(csv_path)
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise CommandError(f'CSV is missing required columns: {", ".join(missing)}')

        valid_positions = set(Player.Position.values)
        unknown = sorted(set(df['Player_Position'].dropna()) - valid_positions)
        if unknown:
            raise CommandError(f'Unknown position(s) in CSV: {", ".join(unknown)}')

        season, created = Season.objects.get_or_create(year=options['season'])
        self.stdout.write(f'Season {season.year}: {"created" if created else "already existed"}')

        teams_created = players_created = entries_created = entries_updated = 0
        imported_player_ids = []

        for row in df.itertuples(index=False):
            # Owner is the stable identity of a franchise; the team name can be
            # renamed year to year, so it is a value we refresh, not a lookup key.
            team, team_was_created = Team.objects.get_or_create(
                owner_name=row.Owner,
                defaults={'name': row.Team},
            )
            teams_created += team_was_created
            if team.name != row.Team:
                team.name = row.Team
                team.save(update_fields=['name'])

            player, player_was_created = Player.objects.get_or_create(
                name=row.Player_Name,
                position=row.Player_Position,
            )
            players_created += player_was_created
            imported_player_ids.append(player.pk)

            # update_or_create keys on the model's unique constraint
            # (season, player), which is what makes re-running safe.
            _, entry_was_created = RosterEntry.objects.update_or_create(
                season=season,
                player=player,
                defaults={
                    'team': team,
                    'draft_round': _optional_int(row.round),
                    'overall_pick': _optional_int(row.overall_pick),
                },
            )
            entries_created += entry_was_created
            entries_updated += not entry_was_created

        stale = RosterEntry.objects.filter(season=season).exclude(player_id__in=imported_player_ids)
        stale_count = stale.count()
        stale.delete()

        self.stdout.write(
            f'Rows read: {len(df)} | teams created: {teams_created} | '
            f'players created: {players_created} | roster entries created: {entries_created}, '
            f'updated: {entries_updated}, pruned: {stale_count}'
        )
        self.stdout.write(self.style.SUCCESS(f'Import complete for {season.year}.'))
