from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

# All keeper rule logic lives in keeper_engine so it stays unit-testable.
# The models only store data and delegate.
from .keeper_engine import base_cost, snake_overall
# Same reasoning for the poll: how a draft time is spelled (and which clock it
# names) lives in one testable place, not in a __str__ and a template and a view.
from .poll import format_slot


class Season(models.Model):
    """One fantasy season, e.g. 2025. Rosters and (later) keepers hang off this."""

    year = models.PositiveIntegerField(unique=True)

    # Rules section 1: keepers are secret until the deadline, then the
    # commissioner enters them and they become visible to everyone. This flag is
    # that switch -- the board shows candidate lists before it and actual
    # declarations after it.
    keepers_revealed = models.BooleanField(
        default=False,
        help_text='Tick once all declarations are entered. Reveals keepers to the league.',
    )

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

    # Filled by the import_adp command. Nullable because a player can exist on a
    # roster long before any ADP data is loaded (or may have none at all).
    nfl_team = models.CharField(max_length=4, blank=True, help_text='Short code, e.g. KC.')
    adp = models.FloatField(
        null=True, blank=True, help_text='Average draft position, lower is earlier.'
    )
    adp_updated = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.position})'

    @property
    def has_adp(self):
        return self.adp is not None


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

    # Section 5. Defaults to eligible: almost every rostered player clears the
    # 4-starts-or-9-weeks bar, so opting the exceptions out is far less work
    # than reviewing 164 players one at a time. Still nullable, so a player can
    # be explicitly parked as "pending" while the commissioner checks Yahoo.
    eligible = models.BooleanField(
        null=True,
        blank=True,
        default=True,
        help_text='Started 4+ weeks, or rostered 9+ weeks. Blank = not yet reviewed.',
    )
    eligibility_note = models.CharField(
        max_length=100,
        blank=True,
        help_text='Evidence, e.g. "started 6 wks" or "added wk 14".',
    )

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

        This is the *base* cost only -- it ignores keep history. For the cost a
        player actually carries this year, use
        keeper_engine.resolve_current_cost(entry, season), which applies
        escalation.
        """
        return base_cost(self.draft_round)

    @property
    def eligibility_label(self):
        """Human-readable eligibility state for templates and the admin."""
        if self.eligible is None:
            return 'Pending review'
        return 'Eligible' if self.eligible else 'Not eligible'


class KeeperPrediction(models.Model):
    """One user's private guess that a rival will keep a particular player.

    Strictly per-user: every read filters on request.user. Nothing here is
    shared, aggregated, or visible to another manager -- two people can predict
    the same player without ever knowing it.

    Deliberately only ever covers OTHER teams. A manager's own keeper plan is
    the one thing that must not exist in the database before the deadline
    (rules section 1), so the own-team sandbox lives in page state alone.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='keeper_predictions'
    )
    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name='predictions')
    roster_entry = models.ForeignKey(
        RosterEntry, on_delete=models.CASCADE, related_name='predictions'
    )
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['season', 'roster_entry']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'season', 'roster_entry'],
                name='unique_prediction_per_user',
            )
        ]

    def __str__(self):
        return f'{self.user.username} predicts {self.roster_entry.player.name}'

    @property
    def player(self):
        return self.roster_entry.player


class DraftSlot(models.Model):
    """A team's position in one season's snake draft (section 6).

    The 2026 order is locked in docs/keeper_rules_v3.md section 6. Later
    seasons are set by finish order and consolation slot choice, so this is
    admin-editable rather than computed.
    """

    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name='draft_slots')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='draft_slots')
    slot = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ['season', 'slot']
        constraints = [
            models.UniqueConstraint(fields=['season', 'slot'], name='unique_slot_per_season'),
            models.UniqueConstraint(fields=['season', 'team'], name='unique_team_slot_per_season'),
        ]

    def __str__(self):
        return f'{self.season.year} slot {self.slot}: {self.team.name}'


class DraftPick(models.Model):
    """One pick in one round of one season's draft.

    original_team is whose slot it is (that never changes); current_team is who
    owns it after trades. Section 7: "a traded pick is the original team's slot
    in that round" -- which is why the snake position follows original_team.
    """

    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name='draft_picks')
    round = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    original_team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name='original_picks'
    )
    current_team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name='owned_picks'
    )
    forfeited = models.BooleanField(
        default=False,
        help_text='Burned to keep a player. Forfeited slots are skipped in the draft.',
    )

    class Meta:
        ordering = ['season', 'round', 'original_team']
        constraints = [
            models.UniqueConstraint(
                fields=['season', 'round', 'original_team'],
                name='unique_pick_per_team_round',
            )
        ]

    def __str__(self):
        owner = '' if self.original_team_id == self.current_team_id else f' -> {self.current_team.name}'
        return f'{self.season.year} R{self.round} ({self.original_team.name}{owner})'

    @property
    def is_traded(self):
        return self.original_team_id != self.current_team_id

    def recompute_owner(self):
        """Re-derive current_team from original_team plus the trade log.

        current_team is a cache, not an independent fact: the PickTrade rows are
        the source of truth. Replaying them means correcting or deleting a trade
        automatically hands the pick back, instead of stranding it with an owner
        no surviving trade justifies.
        """
        owner_id = self.original_team_id
        for trade in self.trades.order_by('date', 'pk'):
            owner_id = trade.to_team_id

        if self.current_team_id != owner_id:
            self.current_team_id = owner_id
            self.save(update_fields=['current_team'])
        return self

    @property
    def overall_position(self):
        """Section 6. Snake position, derived from the slot -- never stored.

        Two queries per call, so this is fine for a pick detail but should be
        precomputed in bulk for the Phase 3 board grid.
        """
        slot = (
            DraftSlot.objects
            .filter(season_id=self.season_id, team_id=self.original_team_id)
            .values_list('slot', flat=True)
            .first()
        )
        if slot is None:
            return None
        team_count = DraftSlot.objects.filter(season_id=self.season_id).count()
        return snake_overall(slot, self.round, team_count)


class PickTrade(models.Model):
    """A record of one pick changing hands (section 7).

    This is an append-only log the commissioner enters; saving one moves the
    pick. Keeping the log separate from DraftPick.current_team means we can
    always answer "how did Isaac end up with Marcus's 4th?".
    """

    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name='pick_trades')
    pick = models.ForeignKey(DraftPick, on_delete=models.CASCADE, related_name='trades')
    from_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='picks_traded_away')
    to_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='picks_traded_for')
    note = models.CharField(max_length=200, blank=True)
    date = models.DateField(help_text='When the trade was agreed.')

    class Meta:
        ordering = ['-date', '-pk']

    def __str__(self):
        return f'{self.pick} : {self.from_team.owner_name} -> {self.to_team.owner_name}'

    def clean(self):
        """Model-level validation. Django calls this from ModelForms (so the
        admin runs it automatically) but NOT from plain .save()."""
        if self.from_team_id == self.to_team_id:
            raise ValidationError('A team cannot trade a pick to itself.')
        if self.pick_id and self.season_id and self.pick.season_id != self.season_id:
            raise ValidationError('That pick belongs to a different season.')

    def save(self, *args, **kwargs):
        # Remember which pick this row pointed at before the edit: if the
        # commissioner corrects a mis-selected pick, the old one has to be
        # handed back, not left stranded with the wrong owner.
        previous_pick_id = (
            PickTrade.objects.filter(pk=self.pk).values_list('pick_id', flat=True).first()
            if self.pk else None
        )

        super().save(*args, **kwargs)

        # Recording the trade is what moves the pick -- there is no separate
        # step for the commissioner to forget.
        self.pick.recompute_owner()
        if previous_pick_id and previous_pick_id != self.pick_id:
            DraftPick.objects.get(pk=previous_pick_id).recompute_owner()

    def delete(self, *args, **kwargs):
        pick = self.pick
        super().delete(*args, **kwargs)
        # Deleting the log entry undoes the transfer it recorded.
        pick.recompute_owner()


class KeeperSelection(models.Model):
    """One declared keeper, entered by the commissioner after the deadline.

    Declarations arrive by text (rules section 1) -- nothing here is manager-
    editable, and nothing exists in the database before the deadline passes.

    roster_entry points at the *prior* season's roster row, which is what
    carries the draft round the cost derives from.
    """

    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name='keeper_selections')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='keeper_selections')
    roster_entry = models.ForeignKey(
        RosterEntry, on_delete=models.CASCADE, related_name='keeper_selections'
    )
    cost_round = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text='Computed by the engine on save.'
    )
    burned_pick = models.ForeignKey(
        DraftPick,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='burned_by',
        help_text='Computed by the engine on save.',
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['season', 'team', 'cost_round']
        constraints = [
            # One selection per player per season. RosterEntry is already unique
            # per (season, player), so keying on the entry gives us the
            # player-level guarantee for free.
            models.UniqueConstraint(
                fields=['season', 'roster_entry'], name='unique_keeper_per_season'
            ),
            # A pick can only be burned once. Conditional so the many rows with
            # burned_pick=NULL don't collide with each other.
            models.UniqueConstraint(
                fields=['burned_pick'],
                condition=models.Q(burned_pick__isnull=False),
                name='unique_burned_pick',
            ),
        ]

    def __str__(self):
        cost = f'R{self.cost_round}' if self.cost_round else 'cost TBD'
        return f'{self.season.year} {self.team.name}: {self.player.name} ({cost})'

    @property
    def player(self):
        return self.roster_entry.player

    def clean(self):
        if self.roster_entry_id and self.season_id:
            if self.roster_entry.season.year != self.season.year - 1:
                raise ValidationError(
                    f'Keepers for {self.season.year} must come from the '
                    f'{self.season.year - 1} roster.'
                )


class Feedback(models.Model):
    """A note from a manager about the site itself.

    Nothing to do with the keeper rules -- this is the suggestion box. It is
    stored rather than emailed so the commissioner can work through it in the
    admin, and so a manager can see that theirs landed.
    """

    # SET_NULL rather than CASCADE: if a login is ever removed (the Cognito
    # swap is coming), the suggestion is still worth reading. Attribution goes,
    # the substance stays.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='feedback',
    )
    # Just the note. Deliberately uncategorised: a box of ten managers a year
    # does not need triage, and a required choice is one more thing between
    # having a thought and sending it.
    message = models.TextField()
    # Where they were when they hit the tab. Filled in by the view, not typed:
    # "the board is unreadable" is a different bug report depending on the page.
    page = models.CharField(max_length=200, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    # The commissioner's tick in the admin. Shown back to the submitter, so
    # sending a note does not feel like shouting into a well.
    resolved = models.BooleanField(
        default=False,
        help_text='Tick once this has been done, or decided against.',
    )

    class Meta:
        ordering = ['-created']
        verbose_name_plural = 'feedback'

    def __str__(self):
        who = self.user.username if self.user else 'someone'
        return f'{who}: {self.message[:50]}'


class DraftPoll(models.Model):
    """The "when are we drafting?" poll for one season.

    A OneToOneField to Season, so two live polls cannot exist. Ten managers
    answering two grids and disagreeing about which one counted is the exact
    failure this rules out at the database level.

    Everything about this poll is PUBLIC by design: every team sees every other
    team's answer, which is the point of a scheduling grid. That is the
    deliberate opposite of KeeperPrediction, which is private to one user. See
    the note on DraftPollVote.
    """

    season = models.OneToOneField(
        Season, on_delete=models.CASCADE, related_name='draft_poll'
    )
    intro = models.TextField(
        blank=True,
        help_text="Your note at the top of the page, e.g. \"Need 8 of 10 to make it work.\"",
    )
    closed = models.BooleanField(
        default=False,
        help_text='Tick to stop all answering. The grid stays visible, read-only.',
    )
    # SET_NULL rather than CASCADE: deleting a candidate time must not delete
    # the poll it was chosen for. Nullable because for most of the poll's life
    # nothing has been chosen yet.
    chosen_option = models.ForeignKey(
        'DraftPollOption',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='chosen_for',
        help_text='The time we are actually drafting at. Called out on the page.',
    )

    def __str__(self):
        return f'{self.season.year} draft-time poll'


class DraftPollOption(models.Model):
    """One candidate draft time.

    starts_at is stored in UTC like every datetime here (USE_TZ), and rendered
    in settings.TIME_ZONE with the zone named in the label -- see
    league.poll.format_slot. Never print one of these without its zone.
    """

    poll = models.ForeignKey(DraftPoll, on_delete=models.CASCADE, related_name='options')
    starts_at = models.DateTimeField()
    label = models.CharField(
        max_length=100,
        blank=True,
        help_text='Optional aside, e.g. "after the Cowboys game".',
    )

    class Meta:
        # Chronological, always. The grid reads like a calendar, and sorting the
        # columns by popularity would make it unreadable.
        ordering = ['starts_at']
        constraints = [
            models.UniqueConstraint(
                fields=['poll', 'starts_at'], name='unique_option_per_poll'
            )
        ]

    def __str__(self):
        when = format_slot(self.starts_at)
        return f'{when} ({self.label})' if self.label else when


class DraftPollVote(models.Model):
    """One team's answer about one candidate time.

    Keyed to TEAM, not to User, and that is deliberate on three counts:

      * everything public on this site derives from views._own_team(request);
      * the grid's row labels are owner names, so Team is what a row IS;
      * User is what gets replaced when the login flow moves to Cognito. A
        team-keyed vote survives that migration untouched; a user-keyed one
        would be orphaned by it.

    This is the exact opposite of KeeperPrediction, which is keyed to User on
    purpose because it is private to a person -- two managers on one team page
    must never see each other's guesses. Do not "fix" either one to match the
    other: they differ because their audiences differ.

    No row means "hasn't answered". There is deliberately no fourth choice for
    it -- an absent row and a stored "unanswered" would be two spellings of one
    state, and something would eventually disagree about which was which.
    """

    class Answer(models.TextChoices):
        YES = 'yes', 'Yes'
        MAYBE = 'maybe', 'If I have to'
        NO = 'no', 'No'

    option = models.ForeignKey(
        DraftPollOption, on_delete=models.CASCADE, related_name='votes'
    )
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='poll_votes')
    answer = models.CharField(max_length=5, choices=Answer.choices)
    # auto_now: rewritten on every save, which is what the page's "has anything
    # changed since?" poll compares against. See league.poll.stamp.
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['option', 'team']
        constraints = [
            models.UniqueConstraint(
                fields=['option', 'team'], name='unique_vote_per_team_option'
            )
        ]

    def __str__(self):
        return f'{self.team.owner_name}: {self.get_answer_display()}'


class DraftPollAlternative(models.Model):
    """One team's "if none of these work, when does?" note.

    One row per team, edited in place -- not append-only. Someone revising
    "weeknights after 8" should replace their old answer, not leave the
    commissioner a trail of contradictions to reconcile. The unique constraint
    is what makes that structural rather than a habit.

    Team-keyed for the same reasons as DraftPollVote, and public for the same
    reason: it is scheduling, and scheduling is a conversation.
    """

    poll = models.ForeignKey(
        DraftPoll, on_delete=models.CASCADE, related_name='alternatives'
    )
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name='poll_alternatives'
    )
    text = models.TextField()
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['team__owner_name']
        constraints = [
            models.UniqueConstraint(
                fields=['poll', 'team'], name='unique_alternative_per_team'
            )
        ]

    def __str__(self):
        return f'{self.team.owner_name}: {self.text[:50]}'
