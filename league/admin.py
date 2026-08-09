from django.contrib import admin

from .models import Player, RosterEntry, Season, Team


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):
    list_display = ['year']


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
    list_display = ['player', 'team', 'season', 'draft_round', 'overall_pick', 'base_keeper_cost']
    list_filter = ['season', 'team', 'player__position']
    search_fields = ['player__name']
    # Renders the season/team/player foreign keys as search boxes instead of
    # 160-item dropdowns. Requires search_fields on the target ModelAdmin.
    autocomplete_fields = ['team', 'player']

    @admin.display(description='Base keeper cost')
    def base_keeper_cost(self, obj):
        return f'Round {obj.base_keeper_cost}'
