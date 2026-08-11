from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from .keeper_engine import recompute_team_selections, validate_keeper_set
from .models import (
    DraftPick,
    DraftPoll,
    DraftPollAlternative,
    DraftPollOption,
    DraftPollVote,
    DraftSlot,
    Feedback,
    KeeperSelection,
    PickTrade,
    Player,
    RosterEntry,
    RulesPoll,
    RulesProposal,
    RulesSuggestion,
    RulesVote,
    Season,
    Team,
)
from .poll import format_slot, tally
from .rules_vote import count as count_choices, turnout


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):
    list_display = ['year', 'keepers_revealed']
    list_editable = ['keepers_revealed']
    search_fields = ['year']


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ['name', 'owner_name', 'user']
    search_fields = ['name', 'owner_name']


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ['name', 'position']
    list_filter = ['position']
    search_fields = ['name']


@admin.register(RosterEntry)
class RosterEntryAdmin(admin.ModelAdmin):
    list_display = [
        'player', 'team', 'season', 'draft_round', 'overall_pick',
        'base_keeper_cost', 'eligible', 'eligibility_note',
    ]
    # list_editable turns those columns into form widgets right on the change
    # list, so ~170 eligibility flags can be set in a few saves instead of 170
    # trips through the detail page. Editable columns must not be the link
    # column, hence list_display_links below.
    list_editable = ['eligible', 'eligibility_note']
    list_display_links = ['player']
    list_filter = ['season', 'eligible', 'team', 'player__position']
    search_fields = ['player__name']
    autocomplete_fields = ['team', 'player']
    list_per_page = 200

    @admin.display(description='Base cost')
    def base_keeper_cost(self, obj):
        return f'Round {obj.base_keeper_cost}'


@admin.register(DraftSlot)
class DraftSlotAdmin(admin.ModelAdmin):
    list_display = ['season', 'slot', 'team']
    list_filter = ['season']
    ordering = ['season', 'slot']
    autocomplete_fields = ['team']


@admin.register(DraftPick)
class DraftPickAdmin(admin.ModelAdmin):
    list_display = [
        'season', 'round', 'overall_position', 'original_team',
        'current_team', 'is_traded', 'forfeited',
    ]
    list_filter = ['season', 'round', 'current_team', 'forfeited']
    autocomplete_fields = ['original_team', 'current_team']
    search_fields = ['original_team__name', 'current_team__name']

    @admin.display(description='Overall', ordering='round')
    def overall_position(self, obj):
        return obj.overall_position

    @admin.display(description='Traded', boolean=True)
    def is_traded(self, obj):
        return obj.is_traded


@admin.register(PickTrade)
class PickTradeAdmin(admin.ModelAdmin):
    """Where the commissioner records pick trades. Saving one moves the pick."""

    list_display = ['pick', 'from_team', 'to_team', 'date', 'note']
    list_filter = ['season']
    autocomplete_fields = ['pick', 'from_team', 'to_team']
    date_hierarchy = 'date'

    def delete_queryset(self, request, queryset):
        """Bulk delete goes through queryset.delete(), which does NOT call
        Model.delete() -- so the picks have to be re-derived here by hand."""
        picks = [trade.pick for trade in queryset.select_related('pick')]
        super().delete_queryset(request, queryset)
        for pick in picks:
            pick.recompute_owner()


class KeeperSelectionForm(forms.ModelForm):
    """Validates the team's ENTIRE keeper set, not just the row being saved.

    Composition limits and pick forfeiture are set-dependent (rules sections 3
    and 4), so the only meaningful question is whether the set this save would
    produce is legal.
    """

    class Meta:
        model = KeeperSelection
        fields = ['season', 'team', 'roster_entry']

    def clean(self):
        cleaned = super().clean()
        season = cleaned.get('season')
        team = cleaned.get('team')
        roster_entry = cleaned.get('roster_entry')

        # Field-level errors already reported; nothing useful to add.
        if not (season and team and roster_entry):
            return cleaned

        others = (
            KeeperSelection.objects
            .filter(season=season, team=team)
            .exclude(pk=self.instance.pk)
            .select_related('roster_entry__player', 'roster_entry__team', 'roster_entry__season')
        )
        proposed = [s.roster_entry for s in others] + [roster_entry]

        result = validate_keeper_set(team, season, proposed)
        if not result.valid:
            raise ValidationError(result.errors)

        # Warnings are surfaced as admin messages after the save (see
        # KeeperSelectionAdmin._report) -- calling add_error here would mark the
        # form invalid and block a legal set.
        return cleaned


@admin.register(KeeperSelection)
class KeeperSelectionAdmin(admin.ModelAdmin):
    """Commissioner-only entry, after the deadline (rules section 1).

    Managers text their keepers privately; nothing is entered here until the
    deadline passes, so there is nothing in the database to leak beforehand.
    """

    form = KeeperSelectionForm
    list_display = ['season', 'team', 'player_name', 'cost_round_label', 'burned_pick_label']
    list_filter = ['season', 'team']
    autocomplete_fields = ['team', 'roster_entry']
    readonly_fields = ['cost_round', 'burned_pick', 'created']
    actions = ['validate_team_keepers']

    @admin.display(description='Player', ordering='roster_entry__player__name')
    def player_name(self, obj):
        return obj.player.name

    @admin.display(description='Cost', ordering='cost_round')
    def cost_round_label(self, obj):
        return f'Round {obj.cost_round}' if obj.cost_round else '-'

    @admin.display(description='Burns')
    def burned_pick_label(self, obj):
        if not obj.burned_pick:
            return '-'
        walked = '' if obj.burned_pick.round == obj.cost_round else ' (walked)'
        return f'Round {obj.burned_pick.round}{walked}'

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Recompute the whole team: adding a keeper can change which pick an
        # existing keeper burns (same-round collision, rules section 3).
        result = recompute_team_selections(obj.team, obj.season)
        self._report(request, obj.team, result)

    def delete_model(self, request, obj):
        team, season = obj.team, obj.season
        super().delete_model(request, obj)
        # Hands the burned pick back and re-resolves whatever is left.
        self._report(request, team, recompute_team_selections(team, season))

    def delete_queryset(self, request, queryset):
        affected = {(s.team, s.season) for s in queryset}
        super().delete_queryset(request, queryset)
        for team, season in affected:
            recompute_team_selections(team, season)

    @admin.action(description="Validate selected teams' keeper sets")
    def validate_team_keepers(self, request, queryset):
        seen = set()
        for selection in queryset.select_related('team', 'season'):
            key = (selection.team_id, selection.season_id)
            if key in seen:
                continue
            seen.add(key)
            result = recompute_team_selections(selection.team, selection.season)
            self._report(request, selection.team, result, always=True)

    def _report(self, request, team, result, always=False):
        """Surface the engine's structured result as admin messages."""
        for error in result.errors:
            self.message_user(request, f'{team.name}: {error}', level=messages.ERROR)
        for warning in result.warnings:
            self.message_user(request, f'{team.name}: {warning}', level=messages.WARNING)

        if result.valid and (always or result.burned_picks):
            burns = ', '.join(str(a) for a in result.burned_picks) or 'no picks burned'
            self.message_user(
                request, f'{team.name}: keeper set is legal. Burns {burns}.', level=messages.SUCCESS
            )


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    """The suggestion box, from the commissioner's side.

    Read-mostly: the notes are other people's words, so nothing here invites
    editing them. `resolved` is the one field this side owns, and list_editable
    puts it on the list page so working through a batch is a column of ticks
    and one Save.
    """

    list_display = ['created', 'user', 'short_message', 'page', 'resolved']
    list_editable = ['resolved']
    list_filter = ['resolved']
    search_fields = ['message', 'user__username']
    readonly_fields = ['user', 'message', 'page', 'created']

    @admin.display(description='Note')
    def short_message(self, obj):
        return obj.message[:80] + ('...' if len(obj.message) > 80 else '')

    def has_add_permission(self, request):
        # Feedback comes from the site's form, attributed to whoever sent it.
        # Typing one here would create a note from nobody.
        return False


class DraftPollOptionInline(admin.TabularInline):
    """The candidate times, all on the poll's own screen.

    A TabularInline renders related rows as a compact table inside the parent's
    form, so a list of eight dates is eight rows and one Save -- rather than
    eight trips through an add page. `extra` is how many blank rows come up
    ready to type into.

    Times are entered in settings.TIME_ZONE (Pacific) and stored in UTC. The
    admin does that conversion itself; type the wall clock you mean.
    """

    model = DraftPollOption
    extra = 6
    fields = ['starts_at', 'label']


@admin.register(DraftPoll)
class DraftPollAdmin(admin.ModelAdmin):
    """Where the commissioner sets the candidate times and reads the answers."""

    list_display = ['season', 'option_count', 'closed', 'chosen_option']
    list_editable = ['closed']
    list_display_links = ['season']
    inlines = [DraftPollOptionInline]
    readonly_fields = ['vote_summary']

    @admin.display(description='Times offered')
    def option_count(self, obj):
        return obj.options.count()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """Limit "chosen option" to THIS poll's times.

        By default a ForeignKey dropdown lists every row in the target table,
        which here would mean offering next year's candidate times as this
        year's answer. The object being edited is on request.resolver_match --
        the admin puts the pk there when it resolves the change URL.
        """
        if db_field.name == 'chosen_option':
            poll_id = request.resolver_match.kwargs.get('object_id')
            kwargs['queryset'] = (
                DraftPollOption.objects.filter(poll_id=poll_id)
                if poll_id else DraftPollOption.objects.none()
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @admin.display(description='Answers so far')
    def vote_summary(self, obj):
        """Per-option counts, plus who has not answered at all.

        That second list is the actual job of running a poll: chasing the four
        people who have not replied. Read-only on purpose -- see
        DraftPollVoteAdmin for why nothing here is editable.
        """
        if obj.pk is None:
            return 'Save the poll first, then add times below.'

        options = list(obj.options.all())
        if not options:
            return 'No candidate times yet. Add some below, then save.'

        votes = list(DraftPollVote.objects.filter(option__poll=obj).select_related('team'))
        by_option = {}
        for vote in votes:
            by_option.setdefault(vote.option_id, []).append(vote.answer)

        lines = []
        for option in options:
            counts = tally(by_option.get(option.pk, []))
            lines.append(
                f'{format_slot(option.starts_at)}  --  '
                f'{counts["yes"]} yes / {counts["maybe"]} if I have to / {counts["no"]} no'
            )

        answered = {vote.team_id for vote in votes}
        silent = [team.owner_name for team in Team.objects.exclude(pk__in=answered)]
        lines.append('')
        lines.append(
            'Not answered at all: ' + (', '.join(silent) if silent else 'nobody -- all in')
        )

        # A plain newline-joined string; the admin renders readonly text in a
        # <div> that respects white-space, so no HTML (and no escaping worry).
        return '\n'.join(lines)


@admin.register(DraftPollOption)
class DraftPollOptionAdmin(admin.ModelAdmin):
    """Registered so options are searchable and reachable on their own.

    Day-to-day entry happens on the poll page through the inline above.
    """

    list_display = ['poll', 'local_time', 'label', 'vote_count']
    list_filter = ['poll']

    @admin.display(description='Starts', ordering='starts_at')
    def local_time(self, obj):
        # Never the bare datetime: it would print in whatever the admin's
        # formatting says and leave the reader to guess the zone.
        return format_slot(obj.starts_at)

    @admin.display(description='Answers')
    def vote_count(self, obj):
        return obj.votes.count()


@admin.register(DraftPollVote)
class DraftPollVoteAdmin(admin.ModelAdmin):
    """The answers, visible but not edited from here.

    Deliberately NOT an inline on the poll: with every team's answer sitting in
    a form on the commissioner's own screen, nudging one is a click away, and a
    poll whose administrator can quietly change an answer is not a poll.
    Managers answer on the site; this is the record.
    """

    list_display = ['option', 'team', 'answer', 'updated']
    list_filter = ['option__poll', 'answer', 'team']
    readonly_fields = ['option', 'team', 'answer', 'updated']

    def has_add_permission(self, request):
        return False


@admin.register(DraftPollAlternative)
class DraftPollAlternativeAdmin(admin.ModelAdmin):
    """The "when else?" notes. Read-only here for the same reason as the votes."""

    list_display = ['poll', 'team', 'short_text', 'updated']
    list_filter = ['poll']
    readonly_fields = ['poll', 'team', 'text', 'updated']

    @admin.display(description='Note')
    def short_text(self, obj):
        return obj.text[:80] + ('...' if len(obj.text) > 80 else '')

    def has_add_permission(self, request):
        return False


class RulesProposalInline(admin.StackedInline):
    """The proposals, written and later graded on the ballot's own screen.

    A StackedInline, not the TabularInline the draft poll's times use. A tabular
    row is one line per object with a column per field, which works for a date
    and a short label -- but a proposal carries four TextFields of prose, and
    four textareas squeezed into one table row is unusable at any window width.
    Stacked gives each proposal a labelled block with full-width fields.

    `outcome` is in the fieldset on purpose: after the vote, recording all three
    results is then one screen and one Save.
    """

    model = RulesProposal
    extra = 0
    fields = [
        'order', 'title', 'rule_reference',
        'current_text', 'proposed_text',
        'case_for', 'case_against', 'note',
        'outcome',
    ]


@admin.register(RulesPoll)
class RulesPollAdmin(admin.ModelAdmin):
    """Where the commissioner writes the ballot, closes it, and records results."""

    list_display = ['season', 'proposal_count', 'closed']
    list_editable = ['closed']
    list_display_links = ['season']
    inlines = [RulesProposalInline]
    readonly_fields = ['vote_summary']

    @admin.display(description='Proposals')
    def proposal_count(self, obj):
        return obj.proposals.count()

    @admin.display(description='Where the vote stands')
    def vote_summary(self, obj):
        """Who has voted while it is open; the full tallies once it is closed.

        THE SECRET BALLOT APPLIES TO THIS SCREEN TOO, and that is not an
        oversight to be tidied up later. The commissioner is a voting member of
        this league. Letting him watch a running count that the other nine
        cannot see means he votes with information nobody else has, which is
        exactly the thing the page's secrecy is for. So while the poll is open
        this shows names and only names -- who has voted, who has not -- which is
        what he actually needs in order to chase people, and which gives away
        nothing about how anyone voted.

        Ticking `closed` is what releases the tallies, to him and to the league
        at the same moment.
        """
        if obj.pk is None:
            return 'Save the ballot first, then write the proposals below.'

        proposals = list(obj.proposals.all())
        if not proposals:
            return 'No proposals yet. Add some below, then save.'

        votes = list(
            RulesVote.objects.filter(proposal__poll=obj).select_related('team')
        )
        by_proposal = {}
        for vote in votes:
            by_proposal.setdefault(vote.proposal_id, []).append(vote)

        team_names = list(Team.objects.values_list('owner_name', flat=True))
        lines = []

        for proposal in proposals:
            cast = by_proposal.get(proposal.pk, [])
            lines.append(f'{proposal.order}. {proposal.title}')

            if obj.closed:
                counts = count_choices([vote.choice for vote in cast])
                lines.append(
                    f'   {counts["for"]} for / {counts["against"]} against / '
                    f'{counts["abstain"]} abstain  --  '
                    f'{turnout(len(team_names), len(cast))}'
                )
                lines.append(
                    '   Outcome: '
                    + (proposal.get_outcome_display() if proposal.outcome else 'not recorded')
                )
            else:
                voted = sorted(vote.team.owner_name for vote in cast)
                silent = sorted(set(team_names) - set(voted))
                lines.append('   Voted: ' + (', '.join(voted) if voted else 'nobody yet'))
                lines.append(
                    '   Still to vote: '
                    + (', '.join(silent) if silent else 'nobody -- all in')
                )
            lines.append('')

        if not obj.closed:
            lines.append(
                'Choices and tallies stay hidden until you tick "closed" -- '
                'including from you. See the docstring on this method.'
            )

        # A plain newline-joined string; the admin renders readonly text in a
        # <div> that respects white-space, so no HTML (and no escaping worry).
        return '\n'.join(lines)


@admin.register(RulesVote)
class RulesVoteAdmin(admin.ModelAdmin):
    """The ballots, visible but never entered or edited from here.

    Same reasoning as DraftPollVoteAdmin, and it bites harder on a governance
    vote: an administrator who can quietly nudge an answer is not running a
    vote. Managers vote on the site; this is the record.
    """

    list_display = ['proposal', 'team', 'choice', 'updated']
    list_filter = ['proposal__poll', 'choice', 'team']
    readonly_fields = ['proposal', 'team', 'choice', 'updated']

    def has_add_permission(self, request):
        return False


@admin.register(RulesSuggestion)
class RulesSuggestionAdmin(admin.ModelAdmin):
    """The "anything else?" notes. Read-only for the same reason as the votes."""

    list_display = ['poll', 'team', 'short_text', 'updated']
    list_filter = ['poll']
    readonly_fields = ['poll', 'team', 'text', 'updated']

    @admin.display(description='Note')
    def short_text(self, obj):
        return obj.text[:80] + ('...' if len(obj.text) > 80 else '')

    def has_add_permission(self, request):
        return False
