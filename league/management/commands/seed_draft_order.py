"""Load a season's locked draft order into DraftSlot.

The 2026 order is fixed by the transition rule in docs/keeper_rules_v3.md
section 6 (Yahoo's final 2025 standings, reversed) and is reproduced in
CLAUDE.md. From 2027 on the order comes from finish order plus consolation slot
choice, so later seasons are entered in the admin instead.

    python manage.py seed_draft_order --season 2026
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from league.models import DraftSlot, Season, Team

# docs/keeper_rules_v3.md section 6, "Transition rule -- 2026 draft only".
LOCKED_ORDERS = {
    2026: [
        (1, 'Ricky'),
        (2, 'Jake'),
        (3, 'Isaac'),
        (4, 'Sonny'),
        (5, 'Luke'),
        (6, 'Pechman'),
        (7, 'Rimler'),
        (8, 'Nick'),
        (9, 'Chris'),
        (10, 'Marcus'),
    ],
}


class Command(BaseCommand):
    help = "Seed a season's draft order from the locked table in the rules doc."

    def add_arguments(self, parser):
        parser.add_argument('--season', type=int, default=2026)

    @transaction.atomic
    def handle(self, *args, **options):
        year = options['season']
        order = LOCKED_ORDERS.get(year)
        if order is None:
            raise CommandError(
                f'No locked order for {year}. Known: {sorted(LOCKED_ORDERS)}. '
                f'Later seasons are entered in the admin.'
            )

        season, _ = Season.objects.get_or_create(year=year)

        for slot, owner in order:
            try:
                team = Team.objects.get(owner_name=owner)
            except Team.DoesNotExist:
                raise CommandError(f'No team with owner "{owner}". Run import_rosters first.')

            DraftSlot.objects.update_or_create(
                season=season, team=team, defaults={'slot': slot}
            )
            self.stdout.write(f'  {slot:>2}. {team.name} ({owner})')

        self.stdout.write(self.style.SUCCESS(f'{year} draft order set ({len(order)} slots).'))
