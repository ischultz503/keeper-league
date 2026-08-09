from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import Player, RosterEntry, Season, Team


class BaseKeeperCostTests(SimpleTestCase):
    """docs/keeper_rules_v3.md section 2.

    SimpleTestCase means no database: base_keeper_cost is pure logic over
    draft_round, so an unsaved RosterEntry is enough to exercise it.
    """

    def cost(self, draft_round):
        return RosterEntry(draft_round=draft_round).base_keeper_cost

    def test_drafted_round_5_costs_round_5(self):
        self.assertEqual(self.cost(5), 5)

    def test_drafted_round_8_costs_round_8(self):
        self.assertEqual(self.cost(8), 8)

    def test_drafted_round_9_costs_round_8(self):
        self.assertEqual(self.cost(9), 8)

    def test_undrafted_costs_round_8(self):
        self.assertEqual(self.cost(None), 8)

    def test_round_1_costs_round_1(self):
        self.assertEqual(self.cost(1), 1)

    def test_deep_round_still_costs_round_8(self):
        # 13th-round picks are common in this league's 16-round drafts.
        self.assertEqual(self.cost(13), 8)

    def test_is_undrafted_flag(self):
        self.assertTrue(RosterEntry(draft_round=None).is_undrafted)
        self.assertFalse(RosterEntry(draft_round=4).is_undrafted)


class RosterOrderingTests(TestCase):
    """Drafted players sort by round; undrafted sort last."""

    def test_undrafted_entries_come_last(self):
        season = Season.objects.create(year=2025)
        team = Team.objects.create(name='Zimbo Baggins', owner_name='Isaac')
        rounds = [3, None, 1, 10]
        for i, rnd in enumerate(rounds):
            player = Player.objects.create(name=f'Player {i}', position=Player.Position.WR)
            RosterEntry.objects.create(season=season, team=team, player=player, draft_round=rnd)

        ordered = list(team.roster_entries.values_list('draft_round', flat=True))
        self.assertEqual(ordered, [1, 3, 10, None])


class ViewAccessTests(TestCase):
    """Every page requires a login; /my-team/ routes a manager to their own team."""

    def setUp(self):
        self.season = Season.objects.create(year=2025)
        self.user = get_user_model().objects.create_user('isaac', password='test-pass-1234')
        self.team = Team.objects.create(name='Zimbo Baggins', owner_name='Isaac', user=self.user)

    def test_anonymous_user_is_redirected_to_login(self):
        for name, args in [('league_overview', []), ('my_team', []), ('team_detail', [self.team.pk])]:
            with self.subTest(view=name):
                response = self.client.get(reverse(name, args=args))
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse('login'), response.url)

    def test_my_team_redirects_to_own_team_page(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('my_team'))
        self.assertRedirects(response, reverse('team_detail', args=[self.team.pk]))

    def test_unlinked_user_falls_back_to_league_overview(self):
        stranger = get_user_model().objects.create_user('commish', password='test-pass-1234')
        self.client.force_login(stranger)
        response = self.client.get(reverse('my_team'))
        self.assertRedirects(response, reverse('league_overview'))

    def test_team_detail_lists_the_roster(self):
        player = Player.objects.create(name='Ladd McConkey', position=Player.Position.WR)
        RosterEntry.objects.create(
            season=self.season, team=self.team, player=player, draft_round=3, overall_pick=24
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('team_detail', args=[self.team.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ladd McConkey')
        self.assertContains(response, 'Round 3')
