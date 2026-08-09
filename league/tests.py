"""Tests for the keeper engine and the site's views.

Layout mirrors the rules doc: costs (section 2), forfeiture (section 3),
composition (section 4), eligibility (section 5), draft order (section 6).

Pure arithmetic is tested with SimpleTestCase (no database at all). Anything
that depends on pick inventory or keep history needs real rows, so it uses
TestCase.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from . import keeper_engine as engine
from .models import (
    DraftPick,
    DraftSlot,
    KeeperSelection,
    PickTrade,
    Player,
    RosterEntry,
    Season,
    Team,
)

OWNERS = ['Ricky', 'Jake', 'Isaac', 'Sonny', 'Luke',
          'Pechman', 'Rimler', 'Nick', 'Chris', 'Marcus']
ROUNDS = 16


def make_teams():
    """The ten franchises, in the locked 2026 slot order."""
    return {owner: Team.objects.create(name=f'Team {owner}', owner_name=owner)
            for owner in OWNERS}


def make_draft(season, teams, rounds=ROUNDS):
    """Slots plus a full pick inventory for one season."""
    for slot, owner in enumerate(OWNERS, start=1):
        team = teams[owner]
        DraftSlot.objects.create(season=season, team=team, slot=slot)
        for round_number in range(1, rounds + 1):
            DraftPick.objects.create(
                season=season, round=round_number,
                original_team=team, current_team=team,
            )


def make_entry(season, team, name, draft_round, eligible=True, position='WR'):
    player = Player.objects.create(name=name, position=position)
    return RosterEntry.objects.create(
        season=season, team=team, player=player,
        draft_round=draft_round, eligible=eligible,
    )


# --- Section 2: costs -------------------------------------------------------


class BaseCostTests(SimpleTestCase):
    """The base cost a player carries out of the draft."""

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
        self.assertEqual(self.cost(13), 8)

    def test_is_undrafted_flag(self):
        self.assertTrue(RosterEntry(draft_round=None).is_undrafted)
        self.assertFalse(RosterEntry(draft_round=4).is_undrafted)


class EscalationTests(SimpleTestCase):
    """Section 2 escalation, checked against the rules doc's own chart."""

    def chain(self, draft_round, keeps):
        entry = RosterEntry(draft_round=draft_round)
        return engine.current_cost(entry, keeps)

    def test_round_6_chain_matches_chart(self):
        # Chart row: Round 6 pick -> 6, 5, 4
        self.assertEqual([self.chain(6, n).cost_round for n in (0, 1, 2)], [6, 5, 4])

    def test_late_round_chain_matches_chart(self):
        # Chart row: Round 9+ or waiver -> 8, 7, 6
        self.assertEqual([self.chain(None, n).cost_round for n in (0, 1, 2)], [8, 7, 6])

    def test_round_2_escalates_to_round_1_then_becomes_impossible(self):
        # Chart row: Round 2 pick -> R2, R1, cannot keep
        self.assertEqual(self.chain(2, 0).cost_round, 2)
        self.assertEqual(self.chain(2, 1).cost_round, 1)

        third = self.chain(2, 2)
        self.assertFalse(third.keepable)
        self.assertIn('escalate past Round 1', third.reason)

    def test_round_1_can_never_be_kept_twice(self):
        # Chart row: Round 1 pick -> R1, cannot keep
        self.assertEqual(self.chain(1, 0).cost_round, 1)
        self.assertFalse(self.chain(1, 1).keepable)

    def test_three_keep_cap_stops_a_round_8_player(self):
        """A Round 8 base never escalates past Round 1, so only the keep cap
        stops it. Both stop conditions are needed."""
        self.assertTrue(self.chain(None, 2).keepable)

        fourth = self.chain(None, 3)
        self.assertFalse(fourth.keepable)
        self.assertIn('maximum', fourth.reason.lower())

    def test_mcconkey_worked_example(self):
        """Rules doc: Ladd McConkey (R3) costs R3 in 2026, R2 in 2027,
        R1 in 2028 -- his final possible keep year."""
        self.assertEqual([self.chain(3, n).cost_round for n in (0, 1, 2)], [3, 2, 1])
        self.assertFalse(self.chain(3, 3).keepable)


class KeepHistoryTests(TestCase):
    """times_kept_before / chain_start_entry against real KeeperSelection rows."""

    @classmethod
    def setUpTestData(cls):
        cls.years = {y: Season.objects.create(year=y) for y in (2025, 2026, 2027, 2028)}
        cls.teams = make_teams()
        for year in (2026, 2027, 2028):
            make_draft(cls.years[year], cls.teams)

    def keep(self, entry, team, season):
        """Record a keep, and create the following season's roster row.

        A kept player was not drafted that year, so his next roster entry has
        draft_round=None -- which is exactly the trap chain_start_entry avoids.
        """
        KeeperSelection.objects.create(season=season, team=team, roster_entry=entry)
        return RosterEntry.objects.create(
            season=season, team=team, player=entry.player,
            draft_round=None, eligible=True,
        )

    def test_no_history_means_zero_keeps(self):
        entry = make_entry(self.years[2025], self.teams['Isaac'], 'Fresh Guy', 4)
        self.assertEqual(engine.times_kept_before(entry.player, self.years[2026]), 0)

    def test_consecutive_keeps_are_counted(self):
        isaac = self.teams['Isaac']
        e2025 = make_entry(self.years[2025], isaac, 'Ladd McConkey', 3)
        e2026 = self.keep(e2025, isaac, self.years[2026])
        self.keep(e2026, isaac, self.years[2027])

        self.assertEqual(engine.times_kept_before(e2025.player, self.years[2028]), 2)

    def test_base_cost_survives_the_missing_draft_round(self):
        """The real regression risk: a kept player's next roster row has
        draft_round=None, which would read as a Round 8 base if we looked at
        the wrong entry. McConkey must still cost R2 in 2027, not R7."""
        isaac = self.teams['Isaac']
        e2025 = make_entry(self.years[2025], isaac, 'Ladd McConkey', 3)
        e2026 = self.keep(e2025, isaac, self.years[2026])

        self.assertIsNone(e2026.draft_round)

        cost = engine.resolve_current_cost(e2026, self.years[2027])
        self.assertEqual(cost.base_round, 3)
        self.assertEqual(cost.cost_round, 2)

    def test_keep_history_follows_the_player_across_a_trade(self):
        """Section 2: 'if you trade for a player who has already been kept
        twice, you inherit his escalated cost and his remaining keep count.'"""
        isaac, marcus = self.teams['Isaac'], self.teams['Marcus']
        e2025 = make_entry(self.years[2025], isaac, 'Traded Guy', 6)
        e2026 = self.keep(e2025, isaac, self.years[2026])

        # Marcus acquires him in the offseason.
        e2026.team = marcus
        e2026.save(update_fields=['team'])

        cost = engine.resolve_current_cost(e2026, self.years[2027])
        self.assertEqual(cost.times_kept_before, 1)
        self.assertEqual(cost.cost_round, 5)

    def test_a_gap_year_resets_the_chain(self):
        """Not kept means back in the draft pool, which starts a fresh chain."""
        isaac = self.teams['Isaac']
        make_entry(self.years[2025], isaac, 'Gap Guy', 2)
        # No KeeperSelection for 2026; he is redrafted in the 5th that year.
        redrafted = RosterEntry.objects.create(
            season=self.years[2026], team=isaac,
            player=Player.objects.get(name='Gap Guy'), draft_round=5, eligible=True,
        )

        cost = engine.resolve_current_cost(redrafted, self.years[2027])
        self.assertEqual(cost.times_kept_before, 0)
        self.assertEqual(cost.cost_round, 5)


# --- Section 6: draft order -------------------------------------------------


class SnakeOrderTests(SimpleTestCase):

    def test_odd_rounds_run_forwards(self):
        self.assertEqual(engine.snake_overall(slot=3, round_number=1, team_count=10), 3)
        self.assertEqual(engine.snake_overall(slot=1, round_number=3, team_count=10), 21)

    def test_even_rounds_run_backwards(self):
        # Slot 3 of 10 picks 8th in round 2 -> overall 18.
        self.assertEqual(engine.snake_overall(slot=3, round_number=2, team_count=10), 18)
        self.assertEqual(engine.snake_overall(slot=10, round_number=2, team_count=10), 11)
        self.assertEqual(engine.snake_overall(slot=1, round_number=2, team_count=10), 20)


class DraftPickModelTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)

    def test_overall_position_uses_the_teams_slot(self):
        isaac = self.teams['Isaac']          # slot 3
        first = DraftPick.objects.get(season=self.season, round=1, original_team=isaac)
        second = DraftPick.objects.get(season=self.season, round=2, original_team=isaac)

        self.assertEqual(first.overall_position, 3)
        self.assertEqual(second.overall_position, 18)

    def test_traded_pick_keeps_the_original_slot_position(self):
        """Section 7: a traded pick is the original team's slot in that round."""
        pick = DraftPick.objects.get(
            season=self.season, round=4, original_team=self.teams['Marcus']
        )
        pick.current_team = self.teams['Isaac']
        pick.save()

        self.assertTrue(pick.is_traded)
        # Marcus is slot 10, so his round 4 pick is 31st overall regardless.
        self.assertEqual(pick.overall_position, engine.snake_overall(10, 4, 10))


class PickTradeTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)

    def test_saving_a_trade_moves_the_pick(self):
        """The real 2026 trade: Marcus's Round 4 goes to Isaac."""
        pick = DraftPick.objects.get(
            season=self.season, round=4, original_team=self.teams['Marcus']
        )
        PickTrade.objects.create(
            season=self.season, pick=pick,
            from_team=self.teams['Marcus'], to_team=self.teams['Isaac'],
            date='2026-01-15', note='Part of the JSN deal',
        )

        pick.refresh_from_db()
        self.assertEqual(pick.current_team, self.teams['Isaac'])
        self.assertEqual(pick.original_team, self.teams['Marcus'])

    def test_repointing_a_trade_hands_the_old_pick_back(self):
        """Regression: picking the wrong pick in the admin autocomplete, then
        correcting it, used to leave the first pick stranded with the new owner
        forever -- there was no trade left to justify it."""
        wrong = DraftPick.objects.get(
            season=self.season, round=2, original_team=self.teams['Ricky']
        )
        right = DraftPick.objects.get(
            season=self.season, round=4, original_team=self.teams['Marcus']
        )

        trade = PickTrade.objects.create(
            season=self.season, pick=wrong,
            from_team=self.teams['Marcus'], to_team=self.teams['Isaac'],
            date='2026-01-15',
        )
        wrong.refresh_from_db()
        self.assertEqual(wrong.current_team, self.teams['Isaac'])

        trade.pick = right
        trade.save()

        wrong.refresh_from_db()
        right.refresh_from_db()
        self.assertEqual(wrong.current_team, self.teams['Ricky'])   # handed back
        self.assertEqual(right.current_team, self.teams['Isaac'])

    def test_deleting_a_trade_hands_the_pick_back(self):
        pick = DraftPick.objects.get(
            season=self.season, round=4, original_team=self.teams['Marcus']
        )
        trade = PickTrade.objects.create(
            season=self.season, pick=pick,
            from_team=self.teams['Marcus'], to_team=self.teams['Isaac'],
            date='2026-01-15',
        )
        trade.delete()

        pick.refresh_from_db()
        self.assertEqual(pick.current_team, self.teams['Marcus'])
        self.assertFalse(pick.is_traded)

    def test_ownership_replays_the_whole_trade_log_in_date_order(self):
        """A pick traded on and then on again ends with the last buyer."""
        pick = DraftPick.objects.get(
            season=self.season, round=4, original_team=self.teams['Marcus']
        )
        PickTrade.objects.create(
            season=self.season, pick=pick, from_team=self.teams['Marcus'],
            to_team=self.teams['Isaac'], date='2026-01-15',
        )
        PickTrade.objects.create(
            season=self.season, pick=pick, from_team=self.teams['Isaac'],
            to_team=self.teams['Luke'], date='2026-02-01',
        )

        pick.refresh_from_db()
        self.assertEqual(pick.current_team, self.teams['Luke'])

        # Undo the later trade only: ownership falls back to the earlier buyer.
        PickTrade.objects.get(to_team=self.teams['Luke']).delete()
        pick.refresh_from_db()
        self.assertEqual(pick.current_team, self.teams['Isaac'])

    def test_admin_bulk_delete_hands_picks_back(self):
        """queryset.delete() skips Model.delete(), so PickTradeAdmin has to
        re-derive owners itself. Same failure mode as the bug above."""
        from django.contrib.admin.sites import site

        pick = DraftPick.objects.get(
            season=self.season, round=4, original_team=self.teams['Marcus']
        )
        PickTrade.objects.create(
            season=self.season, pick=pick, from_team=self.teams['Marcus'],
            to_team=self.teams['Isaac'], date='2026-01-15',
        )

        site._registry[PickTrade].delete_queryset(None, PickTrade.objects.all())

        pick.refresh_from_db()
        self.assertEqual(pick.current_team, self.teams['Marcus'])

    def test_recompute_owner_is_idempotent(self):
        pick = DraftPick.objects.get(
            season=self.season, round=6, original_team=self.teams['Isaac']
        )
        pick.recompute_owner()
        pick.recompute_owner()

        pick.refresh_from_db()
        self.assertEqual(pick.current_team, self.teams['Isaac'])

    def test_a_team_cannot_trade_to_itself(self):
        pick = DraftPick.objects.get(
            season=self.season, round=4, original_team=self.teams['Isaac']
        )
        trade = PickTrade(
            season=self.season, pick=pick,
            from_team=self.teams['Isaac'], to_team=self.teams['Isaac'],
            date='2026-01-15',
        )
        with self.assertRaises(ValidationError):
            trade.full_clean()


# --- Section 3: pick forfeiture ---------------------------------------------


class BurnResolutionTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)

    def setUp(self):
        self.isaac = self.teams['Isaac']
        self.marcus = self.teams['Marcus']

    def pick(self, team, round_number):
        return DraftPick.objects.get(
            season=self.season, round=round_number, original_team=team
        )

    def give_away(self, team, round_number, to_team=None):
        pick = self.pick(team, round_number)
        pick.current_team = to_team or self.teams['Luke']
        pick.save(update_fields=['current_team'])
        return pick

    def burn(self, team, costs, chosen=None):
        return engine.resolve_burned_picks(team, self.season, costs, chosen)

    def rounds_burned(self, result):
        return sorted(a.pick.round for a in result.assignments)

    def test_a_single_keeper_burns_its_own_cost_round(self):
        result = self.burn(self.isaac, [5])
        self.assertTrue(result.ok)
        self.assertEqual(self.rounds_burned(result), [5])
        self.assertFalse(result.assignments[0].walked)

    def test_missing_pick_walks_to_the_next_earlier_round(self):
        """Rules doc worked example: Marcus traded his R4 away, so keeping a
        Round-4-cost player (JSN) burns his R3 instead."""
        self.give_away(self.marcus, 4, to_team=self.isaac)

        result = self.burn(self.marcus, [4])
        self.assertTrue(result.ok)
        self.assertEqual(self.rounds_burned(result), [3])
        self.assertTrue(result.assignments[0].walked)

    def test_same_cost_round_collision_burns_that_round_and_the_one_before(self):
        """Rules doc worked example: Rashee Rice (R8) and Khalil Shakir
        (R10 -> R8 cost) together forfeit Isaac's R8 and R7."""
        result = self.burn(self.isaac, [8, 8])
        self.assertTrue(result.ok)
        self.assertEqual(self.rounds_burned(result), [7, 8])

    def test_collision_and_missing_pick_combine(self):
        """Two R8-cost keepers when the R7 is already traded away -> R8 and R6."""
        self.give_away(self.isaac, 7)

        result = self.burn(self.isaac, [8, 8])
        self.assertTrue(result.ok)
        self.assertEqual(self.rounds_burned(result), [6, 8])

    def test_multiple_picks_in_a_round_default_to_the_teams_own(self):
        """Section 3: owning two picks in the cost round is the manager's
        choice; with no choice given we keep the acquired one."""
        acquired = self.pick(self.marcus, 4)
        acquired.current_team = self.isaac
        acquired.save(update_fields=['current_team'])

        result = self.burn(self.isaac, [4])
        burned = result.assignments[0].pick
        self.assertEqual(burned.original_team, self.isaac)

    def test_an_explicit_choice_overrides_the_default(self):
        acquired = self.pick(self.marcus, 4)
        acquired.current_team = self.isaac
        acquired.save(update_fields=['current_team'])

        result = self.burn(self.isaac, [4], chosen={4: acquired})
        self.assertEqual(result.assignments[0].pick.pk, acquired.pk)

    def test_a_keeper_is_impossible_with_nothing_at_or_before_the_cost_round(self):
        self.give_away(self.isaac, 1)

        result = self.burn(self.isaac, [1])
        self.assertFalse(result.ok)
        self.assertEqual(result.impossible, [1])
        self.assertEqual(result.assignments, [])

    def test_resolution_does_not_depend_on_input_order(self):
        """Costs are sorted internally so the commissioner's typing order
        cannot change which slots get burned."""
        forwards = self.rounds_burned(self.burn(self.isaac, [8, 2, 8]))
        backwards = self.rounds_burned(self.burn(self.isaac, [8, 8, 2]))
        self.assertEqual(forwards, backwards)
        self.assertEqual(forwards, [2, 7, 8])

    def test_forfeited_picks_are_not_available_to_burn_again(self):
        pick = self.pick(self.isaac, 8)
        pick.forfeited = True
        pick.save(update_fields=['forfeited'])

        result = self.burn(self.isaac, [8])
        self.assertEqual(self.rounds_burned(result), [7])


# --- Sections 1, 4, 5: whole-set validation ---------------------------------


class ValidationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.roster_season = Season.objects.create(year=2025)
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)

    def setUp(self):
        self.isaac = self.teams['Isaac']

    def entry(self, name, draft_round, eligible=True):
        return make_entry(self.roster_season, self.isaac, name, draft_round, eligible)

    def validate(self, entries):
        return engine.validate_keeper_set(self.isaac, self.season, entries)

    def assertInvalid(self, result, fragment):
        self.assertFalse(result.valid)
        self.assertTrue(
            any(fragment.lower() in e.lower() for e in result.errors),
            f'expected an error containing {fragment!r}, got {result.errors}',
        )

    def test_a_legal_trio_validates(self):
        """Rules doc section 4: 'a Round 1 cost + a Round 3 cost + a Round 8
        cost' is the stated example of a legal trio."""
        result = self.validate([
            self.entry('Elite Guy', 1),
            self.entry('Good Guy', 3),
            self.entry('Cheap Guy', 10),      # -> Round 8 cost
        ])
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(sorted(a.pick.round for a in result.burned_picks), [1, 3, 8])

    def test_more_than_three_keepers_is_rejected(self):
        result = self.validate([self.entry(f'Guy {i}', 9) for i in range(4)])
        self.assertInvalid(result, 'limit is 3')

    def test_two_premium_keepers_are_rejected(self):
        """Rules doc worked example: Jeanty (R2) and Bowers (R2) -- only one
        keeper may cost Rounds 1-2, so Isaac can keep only one."""
        result = self.validate([
            self.entry('Ashton Jeanty', 2),
            self.entry('Brock Bowers', 2),
        ])
        self.assertInvalid(result, 'Rounds 1-2')

    def test_a_round_1_and_a_round_2_keeper_are_also_rejected(self):
        result = self.validate([self.entry('R1 Guy', 1), self.entry('R2 Guy', 2)])
        self.assertInvalid(result, 'Rounds 1-2')

    def test_three_keepers_need_one_costing_round_8_or_later(self):
        result = self.validate([
            self.entry('A', 2), self.entry('B', 5), self.entry('C', 6),
        ])
        self.assertInvalid(result, 'Round 8 or later')

    def test_two_expensive_keepers_are_fine_without_a_cheap_one(self):
        """The Round-8 floor only applies to a full three-keeper set."""
        result = self.validate([self.entry('A', 2), self.entry('B', 5)])
        self.assertTrue(result.valid, result.errors)

    def test_pending_eligibility_blocks_a_keeper(self):
        entry = self.entry('Unreviewed Guy', 5)
        entry.eligible = None
        entry.save(update_fields=['eligible'])

        self.assertInvalid(self.validate([entry]), 'not been reviewed')

    def test_ineligible_players_are_rejected_with_the_note(self):
        entry = self.entry('Bench Guy', 5, eligible=False)
        entry.eligibility_note = 'rostered 3 wks'
        entry.save(update_fields=['eligibility_note'])

        result = self.validate([entry])
        self.assertInvalid(result, 'not keeper-eligible')
        self.assertInvalid(result, 'rostered 3 wks')

    def test_unpayable_keeper_is_reported_not_raised(self):
        pick = DraftPick.objects.get(season=self.season, round=1, original_team=self.isaac)
        pick.current_team = self.teams['Luke']
        pick.save(update_fields=['current_team'])

        self.assertInvalid(self.validate([self.entry('R1 Guy', 1)]), 'No pick owned in Round 1')

    def test_validation_reports_every_problem_at_once(self):
        """The commissioner should see the whole list, not fix them one at a
        time -- which is why the engine returns results instead of raising."""
        bad = self.entry('Bad Guy', 5, eligible=False)
        result = self.validate([bad, self.entry('R1', 1), self.entry('R2', 2)])
        self.assertFalse(result.valid)
        self.assertGreaterEqual(len(result.errors), 2)


class EscalatedCompositionTests(TestCase):
    """Section 4 is judged on CURRENT-year cost, after escalation."""

    @classmethod
    def setUpTestData(cls):
        cls.years = {y: Season.objects.create(year=y) for y in (2025, 2026, 2027)}
        cls.teams = make_teams()
        make_draft(cls.years[2026], cls.teams)
        make_draft(cls.years[2027], cls.teams)

    def test_an_escalated_round_3_counts_against_the_premium_limit(self):
        """A Round 3 draftee on his second keep costs Round 2 and counts
        against the one-keeper-in-Rounds-1-2 limit -- the exact case called
        out in the rules doc."""
        isaac = self.teams['Isaac']

        # Drafted R3 in 2025, kept in 2026 -> costs R2 in 2027.
        escalating = make_entry(self.years[2025], isaac, 'Ladd McConkey', 3)
        KeeperSelection.objects.create(
            season=self.years[2026], team=isaac, roster_entry=escalating
        )
        escalating_2026 = RosterEntry.objects.create(
            season=self.years[2026], team=isaac,
            player=escalating.player, draft_round=None, eligible=True,
        )

        # Drafted R2 in the 2026 draft -> also costs R2 in 2027.
        plain_r2 = make_entry(self.years[2026], isaac, 'Fresh R2 Guy', 2)

        costs = [
            engine.resolve_current_cost(escalating_2026, self.years[2027]).cost_round,
            engine.resolve_current_cost(plain_r2, self.years[2027]).cost_round,
        ]
        self.assertEqual(costs, [2, 2])

        result = engine.validate_keeper_set(
            isaac, self.years[2027], [escalating_2026, plain_r2]
        )
        self.assertFalse(result.valid)
        self.assertTrue(any('Rounds 1-2' in e for e in result.errors), result.errors)


# --- Applying a set ---------------------------------------------------------


class RecomputeTests(TestCase):
    """recompute_team_selections is the only engine function that writes."""

    @classmethod
    def setUpTestData(cls):
        cls.roster_season = Season.objects.create(year=2025)
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)

    def setUp(self):
        self.isaac = self.teams['Isaac']

    def select(self, name, draft_round):
        entry = make_entry(self.roster_season, self.isaac, name, draft_round)
        return KeeperSelection.objects.create(
            season=self.season, team=self.isaac, roster_entry=entry
        )

    def forfeited_rounds(self):
        return sorted(
            DraftPick.objects
            .filter(season=self.season, current_team=self.isaac, forfeited=True)
            .values_list('round', flat=True)
        )

    def test_two_same_cost_keepers_forfeit_two_rounds(self):
        self.select('Rashee Rice', 8)
        self.select('Khalil Shakir', 10)      # -> Round 8 cost

        result = engine.recompute_team_selections(self.isaac, self.season)

        self.assertTrue(result.valid, result.errors)
        self.assertEqual(self.forfeited_rounds(), [7, 8])

    def test_costs_and_burned_picks_are_written_back(self):
        selection = self.select('Ladd McConkey', 3)
        engine.recompute_team_selections(self.isaac, self.season)

        selection.refresh_from_db()
        self.assertEqual(selection.cost_round, 3)
        self.assertEqual(selection.burned_pick.round, 3)

    def test_removing_a_keeper_hands_its_pick_back(self):
        self.select('Rashee Rice', 8)
        second = self.select('Khalil Shakir', 10)
        engine.recompute_team_selections(self.isaac, self.season)
        self.assertEqual(self.forfeited_rounds(), [7, 8])

        second.delete()
        engine.recompute_team_selections(self.isaac, self.season)

        self.assertEqual(self.forfeited_rounds(), [8])

    def test_recomputing_twice_is_stable(self):
        """Re-running must not creep forwards through the rounds -- the first
        pass forfeits picks that the second pass has to recognise as its own."""
        self.select('Rashee Rice', 8)
        self.select('Khalil Shakir', 10)

        engine.recompute_team_selections(self.isaac, self.season)
        engine.recompute_team_selections(self.isaac, self.season)

        self.assertEqual(self.forfeited_rounds(), [7, 8])


class AdminKeeperEntryTests(TestCase):
    """The commissioner's actual workflow: entering declarations in the admin.

    Exercises the real POST path, so it covers KeeperSelectionForm.clean()
    blocking an illegal set and save_model() triggering the burn recompute.
    """

    @classmethod
    def setUpTestData(cls):
        cls.roster_season = Season.objects.create(year=2025)
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)
        cls.isaac = cls.teams['Isaac']

    def setUp(self):
        self.commish = get_user_model().objects.create_superuser(
            'commish', password='test-pass-1234'
        )
        self.client.force_login(self.commish)

    def add_url(self):
        return reverse('admin:league_keeperselection_add')

    def post_keeper(self, entry):
        return self.client.post(self.add_url(), {
            'season': self.season.pk,
            'team': self.isaac.pk,
            'roster_entry': entry.pk,
        })

    def test_entering_a_legal_keeper_forfeits_its_pick(self):
        entry = make_entry(self.roster_season, self.isaac, 'Rashee Rice', 8)
        response = self.post_keeper(entry)

        self.assertEqual(response.status_code, 302)      # redirect = saved
        selection = KeeperSelection.objects.get(roster_entry=entry)
        self.assertEqual(selection.cost_round, 8)
        self.assertEqual(selection.burned_pick.round, 8)
        self.assertTrue(selection.burned_pick.forfeited)

    def test_a_second_same_cost_keeper_walks_to_the_earlier_round(self):
        rice = make_entry(self.roster_season, self.isaac, 'Rashee Rice', 8)
        shakir = make_entry(self.roster_season, self.isaac, 'Khalil Shakir', 10)
        self.post_keeper(rice)
        self.post_keeper(shakir)

        burned = sorted(
            DraftPick.objects
            .filter(season=self.season, current_team=self.isaac, forfeited=True)
            .values_list('round', flat=True)
        )
        self.assertEqual(burned, [7, 8])

    def test_an_illegal_second_premium_keeper_is_blocked(self):
        jeanty = make_entry(self.roster_season, self.isaac, 'Ashton Jeanty', 2)
        bowers = make_entry(self.roster_season, self.isaac, 'Brock Bowers', 2)
        self.post_keeper(jeanty)

        response = self.post_keeper(bowers)

        self.assertEqual(response.status_code, 200)      # re-rendered = rejected
        self.assertContains(response, 'Rounds 1-2')
        self.assertFalse(KeeperSelection.objects.filter(roster_entry=bowers).exists())

    def test_an_ineligible_player_is_blocked(self):
        entry = make_entry(self.roster_season, self.isaac, 'Bench Guy', 5, eligible=False)
        response = self.post_keeper(entry)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not keeper-eligible')
        self.assertEqual(KeeperSelection.objects.count(), 0)

    def test_keepers_must_come_from_the_prior_season(self):
        """Model.clean() runs from the admin's ModelForm automatically."""
        wrong_season = make_entry(self.season, self.isaac, 'Too New', 4)
        response = self.post_keeper(wrong_season)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'must come from the 2025 roster')


# --- Views ------------------------------------------------------------------


class RosterOrderingTests(TestCase):
    """Drafted players sort by round; undrafted sort last."""

    def test_undrafted_entries_come_last(self):
        season = Season.objects.create(year=2025)
        team = Team.objects.create(name='Zimbo Baggins', owner_name='Isaac')
        for i, rnd in enumerate([3, None, 1, 10]):
            player = Player.objects.create(name=f'Player {i}', position=Player.Position.WR)
            RosterEntry.objects.create(season=season, team=team, player=player, draft_round=rnd)

        ordered = list(team.roster_entries.values_list('draft_round', flat=True))
        self.assertEqual(ordered, [1, 3, 10, None])


class ViewAccessTests(TestCase):
    """Every page requires a login; /my-team/ routes a manager to their team."""

    def setUp(self):
        self.roster_season = Season.objects.create(year=2025)
        self.season = Season.objects.create(year=2026)
        self.user = get_user_model().objects.create_user('isaac', password='test-pass-1234')
        self.team = Team.objects.create(name='Zimbo Baggins', owner_name='Isaac', user=self.user)

    def test_anonymous_user_is_redirected_to_login(self):
        targets = [
            ('league_overview', []), ('my_team', []), ('my_keepers', []),
            ('rules', []), ('team_detail', [self.team.pk]),
        ]
        for name, args in targets:
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
        self.assertRedirects(
            self.client.get(reverse('my_team')), reverse('league_overview')
        )

    def test_team_detail_lists_the_roster(self):
        player = Player.objects.create(name='Ladd McConkey', position=Player.Position.WR)
        RosterEntry.objects.create(
            season=self.roster_season, team=self.team, player=player,
            draft_round=3, overall_pick=24,
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('team_detail', args=[self.team.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ladd McConkey')
        self.assertContains(response, 'Round 3')

    def test_my_keepers_shows_cost_and_eligibility(self):
        player = Player.objects.create(name='Rashee Rice', position=Player.Position.WR)
        RosterEntry.objects.create(
            season=self.roster_season, team=self.team, player=player,
            draft_round=8, eligible=True,
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('my_keepers'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Rashee Rice')
        self.assertContains(response, 'Round 8')
        self.assertContains(response, 'Eligible')

    def test_my_keepers_marks_unreviewed_players_pending(self):
        player = Player.objects.create(name='Unknown Guy', position=Player.Position.RB)
        # eligible defaults to True, so "pending" now has to be set deliberately.
        RosterEntry.objects.create(
            season=self.roster_season, team=self.team, player=player,
            draft_round=5, eligible=None,
        )
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('my_keepers')), 'Pending review')

    def test_rules_page_renders_the_markdown_doc(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('rules'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Keeper Costs')
        # The tables extension must be on, or section 6's draft order is lost.
        self.assertContains(response, '<table>')
