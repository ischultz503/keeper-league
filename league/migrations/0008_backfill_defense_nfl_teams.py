"""Fill in the NFL team code for defenses already in the database.

A DATA migration rather than a schema one: nothing about the table changes,
but the rows in it are wrong. import_adp now derives a defense's team code from
its nickname, and this applies the same fix to the rows imported before it did
-- so the data comes right on `migrate`, with no need to re-run an import (and
no hand-edited database, per CLAUDE.md).

Written with `makemigrations --empty`, which is how a data migration is made.
"""

from django.db import migrations

from league.adp import team_code_for_defense


def fill_defense_teams(apps, schema_editor):
    # apps.get_model, not a direct import: a migration must see the model as it
    # was at THIS point in history, not as models.py happens to define it today.
    # (team_code_for_defense is safe to import directly -- it is a pure function
    # over strings, with no model behind it.)
    Player = apps.get_model('league', 'Player')

    fixed = []
    for player in Player.objects.filter(position='DEF'):
        if player.nfl_team:
            continue
        code = team_code_for_defense(player.name)
        if code:
            player.nfl_team = code
            fixed.append(player)

    Player.objects.bulk_update(fixed, ['nfl_team'])


def clear_defense_teams(apps, schema_editor):
    """Reverse: hand the column back to blank for defenses.

    Reversible on purpose. It loses nothing -- the code is derived from the
    name, so re-applying rebuilds it exactly.
    """
    Player = apps.get_model('league', 'Player')
    Player.objects.filter(position='DEF').update(nfl_team='')


class Migration(migrations.Migration):

    dependencies = [
        ('league', '0007_keeperprediction'),
    ]

    operations = [
        migrations.RunPython(fill_defense_teams, clear_defense_teams),
    ]
