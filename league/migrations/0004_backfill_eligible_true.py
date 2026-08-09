"""Data migration: default existing players to eligible, and repair pick owners.

Two fixes travel together because both correct existing rows rather than the
schema:

1. Changing a field's default only affects NEW rows, so the 164 players already
   imported stay NULL until something backfills them. That is this migration.

2. PickTrade.save() used to move a pick without ever handing back a pick it had
   previously moved, so correcting or deleting a trade could strand a pick with
   an owner no surviving trade justifies. Re-deriving every pick's owner from
   its trade log repairs any such stranding and is safe to run on a clean
   database, where it changes nothing.

Note the migration re-implements the replay instead of calling
DraftPick.recompute_owner(): apps.get_model() returns a *historical* model built
from the migration state, which carries fields but none of the methods defined
on the real class.
"""

from django.db import migrations


def backfill_eligible(apps, schema_editor):
    RosterEntry = apps.get_model('league', 'RosterEntry')
    # Only untouched rows -- never clobber a deliberate "not eligible".
    updated = RosterEntry.objects.filter(eligible__isnull=True).update(eligible=True)
    print(f'\n    marked {updated} roster entries eligible')


def repair_pick_owners(apps, schema_editor):
    DraftPick = apps.get_model('league', 'DraftPick')

    repaired = 0
    for pick in DraftPick.objects.prefetch_related('trades'):
        owner_id = pick.original_team_id
        for trade in sorted(pick.trades.all(), key=lambda t: (t.date, t.pk)):
            owner_id = trade.to_team_id

        if pick.current_team_id != owner_id:
            pick.current_team_id = owner_id
            pick.save(update_fields=['current_team'])
            repaired += 1

    print(f'    re-derived {repaired} pick owner(s) from the trade log')


class Migration(migrations.Migration):

    dependencies = [
        ('league', '0003_alter_rosterentry_eligible'),
    ]

    operations = [
        # RunPython.noop as the reverse: these are corrections, and un-applying
        # them would mean restoring known-bad data.
        migrations.RunPython(backfill_eligible, migrations.RunPython.noop),
        migrations.RunPython(repair_pick_owners, migrations.RunPython.noop),
    ]
