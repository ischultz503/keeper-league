"""Create every DraftPick for a season from that season's DraftSlots.

Each team gets one pick per round, initially owned by the team whose slot it is.
Trades (PickTrade) and forfeitures (KeeperSelection) move things afterwards.

Idempotent, and deliberately so: get_or_create never touches an existing row, so
re-running will not undo a recorded trade or un-forfeit a burned pick.

    python manage.py generate_picks --season 2026 --rounds 16
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from league.keeper_engine import snake_overall
from league.models import DraftPick, DraftSlot, Season

DEFAULT_ROUNDS = 16   # the 2025 draft ran 16 rounds


class Command(BaseCommand):
    help = 'Generate all draft picks for a season from its draft slots.'

    def add_arguments(self, parser):
        parser.add_argument('--season', type=int, default=2026)
        parser.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS)

    @transaction.atomic
    def handle(self, *args, **options):
        year, rounds = options['season'], options['rounds']

        try:
            season = Season.objects.get(year=year)
        except Season.DoesNotExist:
            raise CommandError(f'No Season {year}. Run seed_draft_order --season {year} first.')

        slots = list(DraftSlot.objects.filter(season=season).select_related('team'))
        if not slots:
            raise CommandError(
                f'No draft slots for {year}. Run seed_draft_order --season {year} '
                f'or enter them in the admin.'
            )

        created = 0
        for slot in slots:
            for round_number in range(1, rounds + 1):
                _, was_created = DraftPick.objects.get_or_create(
                    season=season,
                    round=round_number,
                    original_team=slot.team,
                    defaults={'current_team': slot.team},
                )
                created += was_created

        total = DraftPick.objects.filter(season=season).count()
        self.stdout.write(
            f'{len(slots)} slots x {rounds} rounds | created: {created} | total now: {total}'
        )

        # Show round 1 and 2 so the snake is visible at a glance.
        for round_number in (1, 2):
            order = sorted(
                slots, key=lambda s: snake_overall(s.slot, round_number, len(slots))
            )
            names = ', '.join(s.team.owner_name for s in order)
            self.stdout.write(f'  Round {round_number}: {names}')

        self.stdout.write(self.style.SUCCESS(f'Picks generated for {year}.'))
