"""Create one login per team and link it to that Team.

Usernames are the lowercased owner name (Isaac -> isaac). Passwords are left
*unusable* -- Django stores a hash that can never match any password, so nobody
can log in until you set one from the admin (Users -> pick a user -> "this
form" link next to Password). That is deliberate: no default passwords in the
repo, and it is the same shape the Cognito swap will take later.

Idempotent: existing users are relinked, not duplicated or reset.

    python manage.py seed_users
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from league.models import Team


class Command(BaseCommand):
    help = 'Create and link a Django user for each team (idempotent).'

    @transaction.atomic
    def handle(self, *args, **options):
        # get_user_model() instead of importing User directly -- the standard
        # Django way, so this keeps working under a custom user model.
        User = get_user_model()

        teams = Team.objects.order_by('owner_name')
        if not teams:
            self.stdout.write(self.style.WARNING('No teams found. Run import_rosters first.'))
            return

        created_count = linked_count = 0

        for team in teams:
            username = team.owner_name.lower().replace(' ', '')
            user, was_created = User.objects.get_or_create(
                username=username,
                defaults={'first_name': team.owner_name},
            )
            if was_created:
                user.set_unusable_password()
                user.save(update_fields=['password'])
                created_count += 1

            if team.user_id != user.pk:
                team.user = user
                team.save(update_fields=['user'])
                linked_count += 1

            status = 'created' if was_created else 'existing'
            self.stdout.write(f'  {username:<10} -> {team.name}  ({status})')

        self.stdout.write(
            f'Users created: {created_count} | team links set: {linked_count} | teams: {teams.count()}'
        )

        self.stdout.write(self.style.SUCCESS('Team logins seeded.'))
        self.stdout.write('')
        self.stdout.write(self.style.WARNING('NEXT STEPS'))
        self.stdout.write('  1. These accounts have NO usable password yet.')
        if not User.objects.filter(is_superuser=True).exists():
            self.stdout.write('  2. Create your admin account:  python manage.py createsuperuser')
        else:
            supers = ', '.join(User.objects.filter(is_superuser=True).values_list('username', flat=True))
            self.stdout.write(f'  2. Superuser(s) already exist: {supers}')
        self.stdout.write('  3. Log in at /admin/ and set a password for each team user.')
