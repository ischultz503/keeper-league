from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from .keeper_engine import recompute_team_selections, validate_keeper_set
from .models import (
    DraftPick,
    DraftSlot,
    Feedback,
    KeeperSelection,
    PickTrade,
    Player,
    RosterEntry,
    Season,
    Team,
)


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

    list_display = ['created', 'user', 'kind', 'short_message', 'page', 'resolved']
    list_editable = ['resolved']
    list_filter = ['resolved', 'kind']
    search_fields = ['message', 'user__username']
    readonly_fields = ['user', 'kind', 'message', 'page', 'created']

    @admin.display(description='Note')
    def short_message(self, obj):
        return obj.message[:80] + ('...' if len(obj.message) > 80 else '')

    def has_add_permission(self, request):
        # Feedback comes from the site's form, attributed to whoever sent it.
        # Typing one here would create a note from nobody.
        return False
