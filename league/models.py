from django.conf import settings
from django.db import models

# The round a player's keeper cost falls back to when he was drafted late or
# not at all. See docs/keeper_rules_v3.md section 2.
LATE_ROUND_COST_FLOOR = 8


class Season(models.Model):
    """One fantasy season, e.g. 2025. Rosters and (later) keepers hang off this."""

    year = models.PositiveIntegerField(unique=True)

    class Meta:
        ordering = ['-year']

    def __str__(self):
        return str(self.year)


class Team(models.Model):
    """A franchise in the league. The team name may change; the owner is the constant."""

    name = models.CharField(max_length=100)
    owner_name = models.CharField(max_length=50)

    # Nullable one-to-one to Django's User: a Team can exist before its manager
    # has a login, and auth is swapped for Cognito later. Referencing
    # settings.AUTH_USER_MODEL rather than importing User directly is the
    # standard Django way -- it survives a future custom user model.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='team',
    )

    class Meta:
        ordering = ['owner_name']

    def __str__(self):
        return f'{self.name} ({self.owner_name})'


class Player(models.Model):
    """An NFL player. One row per player, reused across seasons."""

    class Position(models.TextChoices):
        QB = 'QB', 'Quarterback'
        RB = 'RB', 'Running Back'
        WR = 'WR', 'Wide Receiver'
        TE = 'TE', 'Tight End'
        K = 'K', 'Kicker'
        DEF = 'DEF', 'Defense'

    name = models.CharField(max_length=100)
    position = models.CharField(max_length=3, choices=Position.choices)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.position})'


class RosterEntry(models.Model):
    """A player on a team's roster at the end of a given season.

    draft_round / overall_pick are null when the player was undrafted (a waiver
    or free-agent pickup).
    """

    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name='roster_entries')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='roster_entries')
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='roster_entries')
    draft_round = models.PositiveIntegerField(null=True, blank=True)
    overall_pick = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            # A player appears on exactly one roster per season.
            models.UniqueConstraint(
                fields=['season', 'player'],
                name='unique_player_per_season',
            )
        ]
        # Drafted players first (by round), undrafted last, then by name.
        ordering = [models.F('draft_round').asc(nulls_last=True), 'player__name']
        verbose_name_plural = 'roster entries'

    def __str__(self):
        return f'{self.player.name} - {self.team.name} ({self.season.year})'

    @property
    def is_undrafted(self):
        return self.draft_round is None

    @property
    def base_keeper_cost(self):
        """The round-cost to keep this player, per keeper_rules_v3.md section 2.

        Drafted in rounds 1-8  -> the round he was drafted in.
        Round 9+ or undrafted  -> a Round 8 pick.

        This is the *base* cost only. Escalation for repeat keeps (section 2,
        "Escalation for repeat keeps") is Phase 2 and is not applied here.
        """
        if self.draft_round is None:
            return LATE_ROUND_COST_FLOOR
        return min(self.draft_round, LATE_ROUND_COST_FLOOR)
