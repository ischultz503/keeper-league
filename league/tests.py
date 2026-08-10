"""Tests for the keeper engine and the site's views.

Layout mirrors the rules doc: costs (section 2), forfeiture (section 3),
composition (section 4), eligibility (section 5), draft order (section 6).

Pure arithmetic is tested with SimpleTestCase (no database at all). Anything
that depends on pick inventory or keep history needs real rows, so it uses
TestCase.
"""

import json
import tempfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from . import adp
from . import draft_sim
from . import keeper_engine as engine
from . import views
from .models import (
    DraftPick,
    DraftSlot,
    KeeperPrediction,
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


# --- Fixtures for the pure draft simulator ----------------------------------
# draft_sim reads nothing but .pk / .round / .position / .adp and the two team
# ids, so these stand-ins are enough -- and using them keeps its tests off the
# database entirely.


def sim_slots(count=3):
    """`count` teams, team id n sitting in draft slot n."""
    return [SimpleNamespace(team_id=n, slot=n) for n in range(1, count + 1)]


def sim_picks(slots, rounds):
    """A full, untraded, unforfeited pick inventory. pk 1..N in round order."""
    picks = []
    for round_number in range(1, rounds + 1):
        for slot in slots:
            picks.append(SimpleNamespace(
                pk=len(picks) + 1,
                round=round_number,
                original_team_id=slot.team_id,
                current_team_id=slot.team_id,
                forfeited=False,
            ))
    return picks


def sim_players(specs):
    """specs: (pk, position, adp) triples. adp of None means "unranked"."""
    return [SimpleNamespace(pk=pk, position=position, adp=adp) for pk, position, adp in specs]


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
        self.assertEqual(result.assignments[0].via, engine.VIA_BASE)

    def test_via_distinguishes_a_collision_from_a_missing_pick(self):
        """Both rules walk earlier, but for different reasons, and the board
        explains which to the manager."""
        collision = self.burn(self.isaac, [8, 8])
        walked = [a for a in collision.assignments if a.walked]
        self.assertEqual([a.via for a in walked], [engine.VIA_COLLISION])

        self.give_away(self.marcus, 4)
        missing = self.burn(self.marcus, [4])
        self.assertEqual(missing.assignments[0].via, engine.VIA_MISSING)

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

    def test_solo_burn_targets_agree_with_resolving_one_keeper_at_a_time(self):
        """solo_burn_targets is a batched shortcut. If it ever disagrees with
        resolve_burned_picks for a lone keeper, the board is lying."""
        self.give_away(self.marcus, 4)
        self.give_away(self.marcus, 7)

        targets = engine.solo_burn_targets(self.marcus, self.season)

        for cost in range(1, 9):
            with self.subTest(cost_round=cost):
                expected = self.burn(self.marcus, [cost]).assignments[0].pick
                self.assertEqual(targets[cost].pk, expected.pk)

    def test_solo_burn_targets_walk_past_a_missing_round(self):
        self.give_away(self.marcus, 4)
        targets = engine.solo_burn_targets(self.marcus, self.season)

        self.assertEqual(targets[4].round, 3)      # missing pick -> next earlier
        self.assertEqual(targets[5].round, 5)      # untouched

    def test_solo_burn_targets_omit_rounds_that_cannot_be_paid(self):
        self.give_away(self.isaac, 1)
        targets = engine.solo_burn_targets(self.isaac, self.season)

        self.assertNotIn(1, targets)
        self.assertEqual(targets[2].round, 2)

    def test_solo_burn_targets_prefer_the_teams_own_pick(self):
        acquired = self.pick(self.marcus, 4)
        acquired.current_team = self.isaac
        acquired.save(update_fields=['current_team'])

        targets = engine.solo_burn_targets(self.isaac, self.season)
        self.assertEqual(targets[4].original_team, self.isaac)

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


class NameNormalizationTests(SimpleTestCase):
    """league/adp.py -- matching FantasyPros spellings to ours."""

    def test_suffixes_are_dropped(self):
        self.assertEqual(adp.normalize_name('Travis Etienne Jr.'), 'travis etienne')
        self.assertEqual(adp.normalize_name('Ken Walker III'), 'ken walker')

    def test_punctuation_is_stripped(self):
        self.assertEqual(adp.normalize_name('Amon-Ra St. Brown'), 'amon ra st brown')
        self.assertEqual(adp.normalize_name("Ja'Marr Chase"), 'jamarr chase')
        self.assertEqual(adp.normalize_name('D.K. Metcalf'), 'dk metcalf')

    def test_hyphenated_names_match_either_spelling(self):
        self.assertEqual(
            adp.normalize_name('Jaxon Smith-Njigba'),
            adp.normalize_name('Jaxon Smith Njigba'),
        )

    def test_accents_are_folded(self):
        self.assertEqual(adp.normalize_name('Ronnie Bell'), 'ronnie bell')
        self.assertEqual(adp.normalize_name('Audric Estimé'), 'audric estime')

    def test_combined_player_cell_splits_into_name_and_team(self):
        """The ADP export packs everything into one column."""
        self.assertEqual(
            adp.split_player_and_team('Jahmyr Gibbs   DET (6)'), ('Jahmyr Gibbs', 'DET')
        )
        self.assertEqual(
            adp.split_player_and_team("Ka'imi Fairbairn   HOU (8)"),
            ("Ka'imi Fairbairn", 'HOU'),
        )

    def test_a_defense_cell_does_not_treat_dst_as_a_team(self):
        """"Houston Texans DST (8)" -- the trailing token is a position marker.
        Keeping it would also break the nickname the defense matches on."""
        name, team = adp.split_player_and_team('Houston Texans DST   (8)')
        self.assertEqual(name, 'Houston Texans')
        self.assertEqual(team, '')
        self.assertEqual(adp.defense_key(name), 'texans')

    def test_a_free_agent_cell_has_no_team_or_bye(self):
        self.assertEqual(adp.split_player_and_team('Tyreek Hill'), ('Tyreek Hill', ''))

    def test_defense_matches_on_nickname(self):
        self.assertEqual(adp.defense_key('Philadelphia Eagles'), 'eagles')
        self.assertEqual(adp.defense_key('Eagles'), 'eagles')

    def test_position_aliases(self):
        self.assertEqual(adp.normalize_position('DST'), 'DEF')
        self.assertEqual(adp.normalize_position('D/ST'), 'DEF')
        self.assertEqual(adp.normalize_position('PK'), 'K')
        self.assertEqual(adp.normalize_position('wr'), 'WR')


class PlayerMatchingTests(TestCase):

    def setUp(self):
        self.etienne = Player.objects.create(name='Travis Etienne Jr.', position='RB')
        self.eagles = Player.objects.create(name='Eagles', position='DEF')
        self.index = adp.build_index(Player.objects.all())

    def test_matches_across_suffix_differences(self):
        self.assertEqual(adp.find_player(self.index, 'Travis Etienne', 'RB'), self.etienne)

    def test_matches_a_defense_by_city_and_nickname(self):
        self.assertEqual(adp.find_player(self.index, 'Philadelphia Eagles', 'DST'), self.eagles)

    def test_position_must_agree(self):
        self.assertIsNone(adp.find_player(self.index, 'Travis Etienne', 'WR'))

    def test_unknown_players_return_none(self):
        self.assertIsNone(adp.find_player(self.index, 'Nobody At All', 'RB'))

    def test_ambiguous_names_are_refused_rather_than_guessed(self):
        """Two real players can share a name and position. Guessing would
        silently attach one player's ADP to the other."""
        Player.objects.create(name='Michael Thomas', position='WR')
        Player.objects.create(name='Michael Thomas', position='WR')
        index = adp.build_index(Player.objects.all())

        self.assertIsNone(adp.find_player(index, 'Michael Thomas', 'WR'))


class ImportAdpCsvTests(TestCase):
    """The CSV path -- the practical source, since the free API tier caps at 10."""

    def setUp(self):
        # These three are ROSTERED, which is what makes them "ours" -- the
        # stranded-without-ADP report is scoped to players with a roster entry,
        # so free agents created by --create-missing cannot drown the signal.
        season = Season.objects.create(year=2025)
        team = Team.objects.create(name='Zimbo Baggins', owner_name='Isaac')

        self.jeanty = Player.objects.create(name='Ashton Jeanty', position='RB')
        self.etienne = Player.objects.create(name='Travis Etienne Jr.', position='RB')
        self.eagles = Player.objects.create(name='Eagles', position='DEF')

        for player in (self.jeanty, self.etienne, self.eagles):
            RosterEntry.objects.create(
                season=season, team=team, player=player, draft_round=5
            )

    def write_csv(self, rows, header='Rank,Player,Team,POS,AVG'):
        path = Path(tempfile.mkdtemp()) / 'adp.csv'
        path.write_text('\n'.join([header] + rows), encoding='utf-8')
        return str(path)

    def test_csv_import_sets_adp_and_team(self):
        path = self.write_csv([
            '1,Ashton Jeanty,LV,RB1,3.4',
            '2,Travis Etienne,JAC,RB2,55.1',
        ])
        call_command('import_adp', csv=path, stdout=StringIO())

        self.jeanty.refresh_from_db()
        self.etienne.refresh_from_db()
        self.assertEqual(self.jeanty.adp, 3.4)
        self.assertEqual(self.jeanty.nfl_team, 'LV')
        self.assertEqual(self.etienne.adp, 55.1)      # matched despite the "Jr."
        self.assertIsNotNone(self.jeanty.adp_updated)

    def test_csv_import_matches_defenses(self):
        path = self.write_csv(['1,Philadelphia Eagles,PHI,DST,140.2'])
        call_command('import_adp', csv=path, stdout=StringIO())

        self.eagles.refresh_from_db()
        self.assertEqual(self.eagles.adp, 140.2)

    def test_our_players_left_without_adp_are_named_for_review(self):
        """The file covers the whole NFL, so most rows matching nobody is
        normal and listing them buries the signal. What needs a human is one of
        OUR players coming out with no ADP -- that suggests a name mismatch."""
        out = StringIO()
        path = self.write_csv(['1,Some Rookie,KC,RB1,88.0'])
        call_command('import_adp', csv=path, stdout=out)

        output = out.getvalue()
        self.assertIn('Ashton Jeanty', output)
        self.assertIn('name mismatch', output)
        # The unrostered NFL player is counted, not listed.
        self.assertNotIn('Some Rookie', output)
        self.assertIn('rows for players nobody rosters: 1', output)

    def test_import_is_idempotent(self):
        path = self.write_csv(['1,Ashton Jeanty,LV,RB1,3.4'])
        call_command('import_adp', csv=path, stdout=StringIO())
        call_command('import_adp', csv=path, stdout=StringIO())

        self.jeanty.refresh_from_db()
        self.assertEqual(self.jeanty.adp, 3.4)
        self.assertEqual(Player.objects.count(), 3)

    def test_a_dry_run_writes_nothing(self):
        path = self.write_csv(['1,Ashton Jeanty,LV,RB1,3.4'])
        call_command('import_adp', csv=path, dry_run=True, stdout=StringIO())

        self.jeanty.refresh_from_db()
        self.assertIsNone(self.jeanty.adp)

    def test_a_rankings_export_falls_back_to_the_rank_column(self):
        """FantasyPros' Rankings export has RK but no AVG. Using RK as the
        draft ordering is correct there -- and the warning must say so."""
        out = StringIO()
        path = self.write_csv(
            ['"1",1,"Ashton Jeanty",LV,"RB1","10"'],
            header='"RK",TIERS,"PLAYER NAME",TEAM,"POS","BYE WEEK"',
        )
        call_command('import_adp', csv=path, stdout=out)

        self.jeanty.refresh_from_db()
        self.assertEqual(self.jeanty.adp, 1.0)
        self.assertEqual(self.jeanty.nfl_team, 'LV')
        self.assertIn('no ADP column', out.getvalue())

    def test_an_adp_export_prefers_avg_over_rank(self):
        """When both columns exist, RK is the ordering and AVG is the
        measurement -- taking RK would silently swap them."""
        path = self.write_csv(
            ['1,Ashton Jeanty,LV,RB1,3.4'], header='Rank,Player,Team,POS,AVG'
        )
        call_command('import_adp', csv=path, stdout=StringIO())

        self.jeanty.refresh_from_db()
        self.assertEqual(self.jeanty.adp, 3.4)      # not 1.0

    def test_an_adp_export_with_a_combined_player_column(self):
        """The real FantasyPros ADP export shape, end to end."""
        path = self.write_csv(
            [
                '1,Ashton Jeanty   LV (10),RB1,11.0',
                '120,Philadelphia Eagles DST   (10),DST1,147.7',
            ],
            header='Rank,Player (Bye),POS,AVG',
        )
        call_command('import_adp', csv=path, stdout=StringIO())

        self.jeanty.refresh_from_db()
        self.eagles.refresh_from_db()
        self.assertEqual(self.jeanty.adp, 11.0)
        self.assertEqual(self.jeanty.nfl_team, 'LV')
        self.assertEqual(self.eagles.adp, 147.7)

    def test_replace_clears_values_absent_from_the_new_file(self):
        """Switching exports must not leave two different scales in one column."""
        first = self.write_csv(['1,Travis Etienne,JAC,RB1,55.1'])
        call_command('import_adp', csv=first, stdout=StringIO())
        self.etienne.refresh_from_db()
        self.assertEqual(self.etienne.adp, 55.1)

        second = self.write_csv(['1,Ashton Jeanty,LV,RB1,3.4'])
        call_command('import_adp', csv=second, replace=True, stdout=StringIO())

        self.etienne.refresh_from_db()
        self.jeanty.refresh_from_db()
        self.assertIsNone(self.etienne.adp)
        self.assertEqual(self.jeanty.adp, 3.4)

    def test_free_agents_are_not_created_by_default(self):
        """The conservative behaviour stays the default; creating rows is opt-in."""
        path = self.write_csv(['1,Some Rookie,KC,RB1,88.0'])
        call_command('import_adp', csv=path, stdout=StringIO())

        self.assertEqual(Player.objects.count(), 3)
        self.assertFalse(Player.objects.filter(name='Some Rookie').exists())

    def test_create_missing_adds_unrostered_players(self):
        """Without these the simulator could only draft last season's rosters --
        no rookies, no free agents."""
        path = self.write_csv([
            '1,Some Rookie,KC,RB1,8.0',
            '2,Ashton Jeanty,LV,RB2,11.0',
        ])
        call_command('import_adp', csv=path, create_missing=True, stdout=StringIO())

        rookie = Player.objects.get(name='Some Rookie')
        self.assertEqual(rookie.position, 'RB')
        self.assertEqual(rookie.nfl_team, 'KC')
        self.assertEqual(rookie.adp, 8.0)
        # Correctly has no roster entry -- that is what "free agent" means.
        self.assertFalse(rookie.roster_entries.exists())

    def test_create_missing_is_idempotent(self):
        path = self.write_csv(['1,Some Rookie,KC,RB1,8.0'])
        call_command('import_adp', csv=path, create_missing=True, stdout=StringIO())
        call_command('import_adp', csv=path, create_missing=True, stdout=StringIO())

        self.assertEqual(Player.objects.filter(name='Some Rookie').count(), 1)

    def test_create_missing_respects_the_limit_taking_best_adp_first(self):
        path = self.write_csv([
            '1,Third Best,KC,RB1,30.0',
            '2,Very Best,KC,RB2,5.0',
            '3,Second Best,KC,RB3,10.0',
        ])
        call_command(
            'import_adp', csv=path, create_missing=True,
            create_missing_limit=2, stdout=StringIO(),
        )

        created = set(Player.objects.values_list('name', flat=True))
        self.assertIn('Very Best', created)
        self.assertIn('Second Best', created)
        self.assertNotIn('Third Best', created)

    def test_create_missing_never_deepens_an_ambiguity(self):
        """Two rostered players already share a name. Adding a third would make
        the ambiguity permanent, so that row is reported, not created."""
        Player.objects.create(name='Michael Thomas', position='WR')
        Player.objects.create(name='Michael Thomas', position='WR')
        out = StringIO()

        path = self.write_csv(['1,Michael Thomas,SF,WR1,44.0'])
        call_command('import_adp', csv=path, create_missing=True, stdout=out)

        self.assertEqual(Player.objects.filter(name='Michael Thomas').count(), 2)
        self.assertIn('matched more than one', out.getvalue())

    def test_duplicate_rows_in_one_file_create_one_player(self):
        path = self.write_csv([
            '1,Some Rookie,KC,RB1,8.0',
            '2,Some Rookie,KC,RB2,9.0',
        ])
        call_command('import_adp', csv=path, create_missing=True, stdout=StringIO())

        self.assertEqual(Player.objects.filter(name='Some Rookie').count(), 1)

    def test_free_agents_do_not_drown_the_stranded_report(self):
        """A created free agent with no ADP is noise; a rostered player without
        one is the signal."""
        out = StringIO()
        path = self.write_csv(['1,Some Rookie,KC,RB1,8.0'])
        call_command('import_adp', csv=path, create_missing=True, stdout=out)

        output = out.getvalue()
        self.assertIn('Ashton Jeanty', output)      # rostered, no ADP -> flagged
        self.assertIn('created as free agents: 1', output)

    def test_a_csv_with_no_ordering_column_is_a_clean_error(self):
        path = self.write_csv(['Ashton Jeanty,LV'], header='Player,Team')
        with self.assertRaises(CommandError):
            call_command('import_adp', csv=path, stdout=StringIO())

    def test_a_missing_csv_is_a_clean_error(self):
        with self.assertRaises(CommandError):
            call_command('import_adp', csv='no/such/file.csv', stdout=StringIO())

    def test_rows_without_a_usable_adp_are_skipped(self):
        path = self.write_csv(['1,Ashton Jeanty,LV,RB1,'])
        call_command('import_adp', csv=path, stdout=StringIO())

        self.jeanty.refresh_from_db()
        self.assertIsNone(self.jeanty.adp)


class ImportEligibilityTests(TestCase):
    """Rules section 5, imported from a reviewed CSV."""

    def setUp(self):
        self.season = Season.objects.create(year=2025)
        self.team = Team.objects.create(name='Zimbo Baggins', owner_name='Isaac')
        self.jeanty = self.entry('Ashton Jeanty', 'RB')
        self.etienne = self.entry('Travis Etienne Jr.', 'RB')
        self.eagles = self.entry('Eagles', 'DEF')

    def entry(self, name, position):
        player = Player.objects.create(name=name, position=position)
        return RosterEntry.objects.create(
            season=self.season, team=self.team, player=player, draft_round=5
        )

    def write_csv(self, rows):
        header = ('Owner,Team,Player_Name,Player_Position,weeks_started,'
                  'weeks_rostered,eligible,reason,source')
        path = Path(tempfile.mkdtemp()) / 'eligibility.csv'
        path.write_text('\n'.join([header] + rows), encoding='utf-8')
        return str(path)

    def run_import(self, rows, apply=False):
        out = StringIO()
        call_command(
            'import_eligibility', csv=self.write_csv(rows), apply=apply, stdout=out
        )
        return out.getvalue()

    def test_yes_and_no_become_true_and_false(self):
        self.run_import([
            'Isaac,Zimbo Baggins,Ashton Jeanty,RB,16,17,yes,"started 16 wks",yahoo',
            'Isaac,Zimbo Baggins,Eagles,DEF,0,7,NO,"failed both: started 0, rostered 7",yahoo',
        ], apply=True)

        self.jeanty.refresh_from_db()
        self.eagles.refresh_from_db()
        self.assertIs(self.jeanty.eligible, True)
        self.assertIs(self.eagles.eligible, False)

    def test_the_reason_becomes_the_note(self):
        self.run_import(
            ['Isaac,Zimbo Baggins,Ashton Jeanty,RB,16,17,yes,"started 16 wks",yahoo'],
            apply=True,
        )
        self.jeanty.refresh_from_db()
        self.assertEqual(self.jeanty.eligibility_note, 'started 16 wks')

    def test_dry_run_is_the_default_and_writes_nothing(self):
        """This rewrites the flag that gates every keeper declaration, so
        seeing the diff first is worth a flag."""
        output = self.run_import(
            ['Isaac,Zimbo Baggins,Ashton Jeanty,RB,16,17,NO,"nope",yahoo']
        )

        self.jeanty.refresh_from_db()
        self.assertIs(self.jeanty.eligible, True)          # model default, untouched
        self.assertIn('Dry run', output)

    def test_matching_survives_a_suffix_difference(self):
        self.run_import(
            ['Isaac,Zimbo Baggins,Travis Etienne,RB,13,17,yes,"started 13 wks",yahoo'],
            apply=True,
        )
        self.etienne.refresh_from_db()
        self.assertEqual(self.etienne.eligibility_note, 'started 13 wks')

    def test_a_defense_matches_on_its_nickname(self):
        self.run_import(
            ['Isaac,Zimbo Baggins,Philadelphia Eagles,DEF,0,7,NO,"failed both",yahoo'],
            apply=True,
        )
        self.eagles.refresh_from_db()
        self.assertIs(self.eagles.eligible, False)

    def test_unmatched_rows_are_reported_not_guessed(self):
        output = self.run_import(
            ['Isaac,Zimbo Baggins,Nobody At All,RB,1,1,NO,"failed both",yahoo']
        )
        self.assertIn('Nobody At All', output)
        self.assertIn('matching no roster entry', output)

    def test_an_unreadable_eligible_value_is_reported(self):
        output = self.run_import(
            ['Isaac,Zimbo Baggins,Ashton Jeanty,RB,16,17,maybe,"unsure",yahoo']
        )
        self.assertIn('unreadable', output)
        self.assertIn('Ashton Jeanty', output)

    def test_roster_entries_absent_from_the_csv_are_left_alone(self):
        output = self.run_import(
            ['Isaac,Zimbo Baggins,Ashton Jeanty,RB,16,17,yes,"started 16 wks",yahoo'],
            apply=True,
        )
        self.etienne.refresh_from_db()
        self.assertEqual(self.etienne.eligibility_note, '')
        self.assertIn('absent from the CSV', output)

    def test_an_owner_disagreeing_with_the_database_is_flagged(self):
        """One of the two is stale, which is worth knowing before trusting either."""
        output = self.run_import(
            ['Marcus,Shedeur for ROTY,Ashton Jeanty,RB,16,17,yes,"started 16 wks",yahoo']
        )
        self.assertIn('owner disagrees', output)

    def test_reimporting_reports_nothing_left_to_change(self):
        rows = ['Isaac,Zimbo Baggins,Ashton Jeanty,RB,16,17,yes,"started 16 wks",yahoo']
        self.run_import(rows, apply=True)
        output = self.run_import(rows, apply=True)

        self.assertIn('to change: 0', output)
        self.assertIn('already correct: 1', output)

    def test_a_missing_csv_is_a_clean_error(self):
        with self.assertRaises(CommandError):
            call_command('import_eligibility', csv='no/such.csv', stdout=StringIO())


class DraftSimTests(SimpleTestCase):
    """The ADP autofill (league/draft_sim.py).

    SimpleTestCase, with hand-built stand-ins for slots, picks and players: the
    module is meant to be pure, and building fixtures out of SimpleNamespace
    proves it -- if a Django query ever crept in, these tests would fail.
    """

    def setUp(self):
        self.slots = sim_slots(3)

    def rbs(self, count, start=1):
        """`count` running backs, ADP ascending, so no positional cap applies."""
        return sim_players([(n, 'RB', float(n)) for n in range(start, start + count)])

    def simulate(self, rounds=3, pool=None, slots=None, **kwargs):
        slots = slots if slots is not None else self.slots
        picks = sim_picks(slots, rounds)
        return picks, draft_sim.simulate_draft(
            slots=slots,
            picks=picks,
            pool=self.rbs(9) if pool is None else pool,
            rounds=rounds,
            **kwargs,
        )

    def test_picks_run_in_snake_order_taking_the_best_adp_available(self):
        _, result = self.simulate()

        self.assertEqual([p.overall for p in result], list(range(1, 10)))
        self.assertEqual([p.player_id for p in result], list(range(1, 10)))
        self.assertTrue(all(p.source == draft_sim.SOURCE_SIM for p in result))

    def test_the_second_round_runs_backwards(self):
        _, result = self.simulate()
        picking = {p.overall: p.team_id for p in result}

        self.assertEqual([picking[1], picking[2], picking[3]], [1, 2, 3])
        self.assertEqual([picking[4], picking[5], picking[6]], [3, 2, 1])

    def test_players_without_adp_are_taken_last(self):
        pool = sim_players([(1, 'RB', None), (2, 'RB', 5.0), (3, 'RB', 1.0)])
        _, result = self.simulate(rounds=1, pool=pool)

        self.assertEqual([p.player_id for p in result], [3, 2, 1])

    def test_adp_ties_break_on_id_so_the_board_is_reproducible(self):
        tied = sim_players([(9, 'RB', 2.0), (3, 'RB', 2.0), (5, 'RB', 2.0)])
        _, result = self.simulate(rounds=1, pool=tied)

        self.assertEqual([p.player_id for p in result], [3, 5, 9])

    def test_the_same_inputs_always_produce_the_same_board(self):
        """Scenario comparison is the whole point; a reshuffle would break it."""
        pool = sim_players([(n, 'RB', 2.0) for n in range(1, 10)])
        _, first = self.simulate(rounds=3, pool=pool)
        _, second = self.simulate(rounds=3, pool=list(reversed(pool)))

        self.assertEqual([p.player_id for p in first], [p.player_id for p in second])

    def test_a_burned_pick_is_dead_but_moves_nobody(self):
        picks, before = self.simulate()
        burned_pick = next(p for p in picks if p.pk == 2)

        _, after = self.simulate(burned_pick_ids={burned_pick.pk})

        # Same cells, same owners, same pick numbers -- one of them just goes
        # unused. A forfeited slot is skipped, not removed (rules section 3).
        self.assertEqual([p.overall for p in before], [p.overall for p in after])
        self.assertEqual([p.team_id for p in before], [p.team_id for p in after])

        dead = next(p for p in after if p.pick_id == burned_pick.pk)
        self.assertEqual(dead.source, draft_sim.SOURCE_BURNED)
        self.assertIsNone(dead.player_id)

        # The player that cell would have taken is still on the board for the
        # next team, which is exactly what happens in a real draft.
        self.assertEqual(after[2].player_id, 2)

    def test_the_forfeited_flag_alone_does_not_kill_a_cell(self):
        """Only the caller's burned list matters, and that is deliberate.

        DraftPick.forfeited goes true the moment the commissioner enters a
        declaration -- before the reveal. Honouring it here would make the
        simulator quietly announce who has already declared, which rules
        section 1 forbids. views.simulate decides what is burned.
        """
        slots = sim_slots(3)
        picks = sim_picks(slots, 1)
        picks[0].forfeited = True

        result = draft_sim.simulate_draft(
            slots=slots, picks=picks, pool=self.rbs(9), rounds=1
        )
        self.assertEqual(result[0].source, draft_sim.SOURCE_SIM)

    def test_no_second_quarterback_until_round_nine(self):
        pool = sim_players(
            [(1, 'QB', 1.0), (2, 'QB', 2.0)] + [(n, 'RB', float(n)) for n in range(3, 12)]
        )
        _, result = self.simulate(rounds=10, pool=pool, slots=sim_slots(1))
        positions = [{p.pk: p.position for p in pool}[cell.player_id] for cell in result]

        self.assertEqual(positions[0], 'QB')
        self.assertEqual(positions[1:8], ['RB'] * 7)
        # Round 9 lifts the cap, and the second QB is the best player left.
        self.assertEqual(positions[8], 'QB')

    def test_no_second_tight_end_until_round_nine(self):
        pool = sim_players(
            [(1, 'TE', 1.0), (2, 'TE', 2.0)] + [(n, 'RB', float(n)) for n in range(3, 12)]
        )
        _, result = self.simulate(rounds=10, pool=pool, slots=sim_slots(1))
        positions = [{p.pk: p.position for p in pool}[cell.player_id] for cell in result]

        self.assertEqual(positions[1:8], ['RB'] * 7)
        self.assertEqual(positions[8], 'TE')

    def test_kickers_and_defenses_wait_for_the_final_two_rounds(self):
        pool = sim_players([
            (1, 'K', 1.0), (2, 'DEF', 2.0), (3, 'RB', 3.0), (4, 'RB', 4.0),
        ])
        _, result = self.simulate(rounds=4, pool=pool, slots=sim_slots(1))

        # Best ADP in the pool is the kicker, and he still waits until Round 3.
        self.assertEqual([cell.player_id for cell in result], [3, 4, 1, 2])

    def test_keepers_predictions_and_sandbox_players_are_never_projected(self):
        _, result = self.simulate(taken_player_ids={1, 2})
        drafted = [p.player_id for p in result]

        self.assertNotIn(1, drafted)
        self.assertNotIn(2, drafted)
        self.assertEqual(drafted[0], 3)

    def test_a_kept_quarterback_counts_against_the_cap(self):
        pool = sim_players([(1, 'QB', 1.0), (2, 'RB', 2.0)])
        _, result = self.simulate(
            rounds=1, pool=pool, slots=sim_slots(1), roster_positions={1: {'QB'}}
        )
        self.assertEqual(result[0].player_id, 2)

    def test_an_exhausted_pool_yields_empty_cells_rather_than_an_error(self):
        _, result = self.simulate(rounds=1, pool=[])

        self.assertTrue(all(p.source == draft_sim.SOURCE_EMPTY for p in result))
        self.assertTrue(all(p.player_id is None for p in result))

    def test_every_candidate_being_capped_also_yields_empty(self):
        pool = sim_players([(1, 'K', 1.0)])
        _, result = self.simulate(rounds=4, pool=pool, slots=sim_slots(1))

        # Rounds 1-2 have nothing takeable (a kicker is not legal yet), Round 3
        # takes him, Round 4 is out of players. Neither case raises.
        self.assertEqual(
            [cell.source for cell in result],
            [draft_sim.SOURCE_EMPTY, draft_sim.SOURCE_EMPTY,
             draft_sim.SOURCE_SIM, draft_sim.SOURCE_EMPTY],
        )

    def test_a_traded_pick_drafts_for_its_new_owner(self):
        """The cell stays in the original team's column; the player does not."""
        slots = sim_slots(2)
        picks = sim_picks(slots, 1)
        picks[0].current_team_id = 2          # team 1's opener, owned by team 2

        pool = sim_players([(1, 'QB', 1.0), (2, 'RB', 2.0)])
        result = draft_sim.simulate_draft(
            slots=slots, picks=picks, pool=pool, rounds=1,
            roster_positions={2: {'QB'}},
        )

        self.assertEqual(result[0].team_id, 2)
        # Team 2 already has a QB, so its cap applies to the pick it acquired.
        self.assertEqual(result[0].player_id, 2)


class MockDraftTests(SimpleTestCase):
    """run_sim's pause / inject / resume behaviour.

    The design is stateless: every step replays the whole draft from the same
    inputs plus one more manual pick. These tests are what hold that honest --
    if replay were not deterministic, "resume" would silently rewrite the
    already-drafted part of the board.
    """

    def setUp(self):
        self.slots = sim_slots(3)
        self.me = 2                       # team 2 drafts second in odd rounds

    def pool(self, count=12, start=1):
        return sim_players([(n, 'RB', float(n)) for n in range(start, start + count)])

    def step(self, rounds=3, pool=None, manual=None, stop=True, **kwargs):
        return draft_sim.run_sim(
            slots=self.slots,
            picks=sim_picks(self.slots, rounds),
            pool=self.pool() if pool is None else pool,
            rounds=rounds,
            user_team_id=self.me,
            manual_picks=manual or {},
            stop_at_next_user_pick=stop,
            **kwargs,
        )

    # -- stopping -----------------------------------------------------------

    def test_it_stops_at_my_first_pick(self):
        run = self.step()

        self.assertFalse(run.done)
        self.assertEqual(run.paused_at.team_id, self.me)
        self.assertEqual(run.paused_at.round, 1)
        self.assertEqual(run.paused_at.overall, 2)
        # Only the picks before mine are decided; everything after depends on
        # who I take.
        self.assertEqual([c.overall for c in run.cells], [1])

    def test_the_pause_offers_cap_respecting_suggestions(self):
        pool = sim_players([
            (1, 'QB', 1.0), (2, 'QB', 2.0), (3, 'RB', 3.0), (4, 'RB', 4.0), (5, 'K', 0.5),
        ])
        run = self.step(rounds=6, pool=pool, roster_positions={self.me: {'QB'}})

        offered = [p.pk for p in run.paused_at.suggestions]
        self.assertNotIn(1, offered)      # already have a QB, and it is round 1
        self.assertNotIn(5, offered)      # kicker, nowhere near the last rounds
        self.assertEqual(offered[0], 3)

    def test_suggestions_are_capped_in_number(self):
        run = self.step(suggestion_count=2)
        self.assertEqual(len(run.paused_at.suggestions), 2)

    def test_a_burned_cell_of_mine_is_never_offered(self):
        """There is no pick to make there -- it paid for a keeper."""
        picks = sim_picks(self.slots, 3)
        mine_first = next(p for p in picks if p.round == 1 and p.original_team_id == self.me)

        run = draft_sim.run_sim(
            slots=self.slots, picks=picks, pool=self.pool(), rounds=3,
            burned_pick_ids={mine_first.pk},
            user_team_id=self.me, stop_at_next_user_pick=True,
        )

        self.assertNotEqual(run.paused_at.pick_id, mine_first.pk)
        self.assertEqual(run.paused_at.round, 2)     # my next real pick
        burned = next(c for c in run.cells if c.pick_id == mine_first.pk)
        self.assertEqual(burned.source, draft_sim.SOURCE_BURNED)

    # -- injecting ----------------------------------------------------------

    def test_my_choice_is_injected_and_the_replay_moves_on(self):
        first = self.step()
        mine = first.paused_at.pick_id

        second = self.step(manual={mine: 7})          # deliberately not the BPA

        chosen = next(c for c in second.cells if c.pick_id == mine)
        self.assertEqual(chosen.player_id, 7)
        self.assertEqual(chosen.source, draft_sim.SOURCE_MANUAL)
        # ...and it now pauses at my *next* pick, in round 2.
        self.assertEqual(second.paused_at.round, 2)

    def test_a_player_i_took_is_gone_for_everyone_after_me(self):
        first = self.step()
        run = self.step(manual={first.paused_at.pick_id: 7})

        drafted = [c.player_id for c in run.cells if c.player_id is not None]
        self.assertEqual(drafted.count(7), 1)
        self.assertNotIn(7, [p.pk for p in run.available])

    def test_taking_an_already_drafted_player_is_refused(self):
        first = self.step()
        gone = first.cells[0].player_id          # team 1 just took him

        with self.assertRaises(draft_sim.SimError):
            self.step(manual={first.paused_at.pick_id: gone})

    def test_a_player_outside_the_pool_is_refused(self):
        first = self.step()
        with self.assertRaises(draft_sim.SimError):
            self.step(manual={first.paused_at.pick_id: 9999})

    def test_a_pick_that_is_not_mine_is_refused(self):
        """Mock drafting is for my own team only -- out of scope by design."""
        picks = sim_picks(self.slots, 3)
        theirs = next(p for p in picks if p.round == 1 and p.original_team_id == 1)

        with self.assertRaises(draft_sim.SimError):
            draft_sim.run_sim(
                slots=self.slots, picks=picks, pool=self.pool(), rounds=3,
                user_team_id=self.me, manual_picks={theirs.pk: 5},
                stop_at_next_user_pick=True,
            )

    # -- resuming -----------------------------------------------------------

    def test_replaying_never_rewrites_what_was_already_decided(self):
        """The heart of the stateless design: earlier cells must not move."""
        run = self.step()
        chosen = {}

        while not run.done:
            before = {c.pick_id: c.player_id for c in run.cells}
            chosen[run.paused_at.pick_id] = run.paused_at.suggestions[0].pk
            run = self.step(manual=chosen)
            after = {c.pick_id: c.player_id for c in run.cells}
            for pick_id, player_id in before.items():
                self.assertEqual(after[pick_id], player_id)

        self.assertTrue(run.done)

    def test_undo_returns_exactly_the_previous_board(self):
        run = self.step()
        first_pick = run.paused_at.pick_id
        one = self.step(manual={first_pick: 7})
        two = self.step(manual={first_pick: 7, one.paused_at.pick_id: 8})

        undone = self.step(manual={first_pick: 7})
        self.assertEqual(
            [(c.pick_id, c.player_id) for c in undone.cells],
            [(c.pick_id, c.player_id) for c in one.cells],
        )
        # Back to waiting on the pick that `two` had answered.
        self.assertEqual(undone.paused_at.pick_id, one.paused_at.pick_id)
        self.assertIn(8, [p.pk for p in undone.available])
        self.assertNotIn(8, [c.player_id for c in undone.cells])
        self.assertFalse(two.done)

    def test_choosing_every_pick_runs_the_draft_to_completion(self):
        run = self.step()
        chosen = {}
        while not run.done:
            chosen[run.paused_at.pick_id] = run.paused_at.suggestions[0].pk
            run = self.step(manual=chosen)

        self.assertIsNone(run.paused_at)
        self.assertEqual(len(run.cells), 9)
        self.assertEqual(len(chosen), 3)             # one per round, my slot
        mine = [c for c in run.cells if c.team_id == self.me]
        self.assertTrue(all(c.source == draft_sim.SOURCE_MANUAL for c in mine))

    def test_snake_order_holds_across_the_pauses(self):
        run = self.step()
        chosen = {}
        while not run.done:
            chosen[run.paused_at.pick_id] = run.paused_at.suggestions[0].pk
            run = self.step(manual=chosen)

        self.assertEqual([c.overall for c in run.cells], list(range(1, 10)))
        picking = {c.overall: c.team_id for c in run.cells}
        self.assertEqual([picking[4], picking[5], picking[6]], [3, 2, 1])

    def test_i_may_draft_against_the_caps(self):
        """The caps shape suggestions. They do not police my own choices."""
        # Six rounds, so the kicker is not legal for the sim until Round 5.
        pool = sim_players([(1, 'RB', 1.0), (2, 'K', 2.0), (3, 'RB', 3.0), (4, 'RB', 4.0)])
        run = self.step(rounds=6, pool=pool)

        self.assertNotIn(2, [p.pk for p in run.paused_at.suggestions])
        taken = self.step(rounds=6, pool=pool, manual={run.paused_at.pick_id: 2})

        kicker = next(c for c in taken.cells if c.player_id == 2)
        self.assertEqual(kicker.source, draft_sim.SOURCE_MANUAL)

    def test_full_auto_ignores_the_pause_entirely(self):
        run = self.step(stop=False)

        self.assertTrue(run.done)
        self.assertEqual(len(run.cells), 9)
        self.assertTrue(all(c.source != draft_sim.SOURCE_MANUAL for c in run.cells))


class DraftSimModelTests(TestCase):
    """The same module, driven by real rows -- the shape the view will use."""

    def test_it_runs_against_real_slots_picks_and_players(self):
        season = Season.objects.create(year=2026)
        teams = make_teams()
        make_draft(season, teams)

        pool = [
            Player.objects.create(name=f'Player {n}', position='RB', adp=float(n))
            for n in range(1, 30)
        ]

        result = draft_sim.simulate_draft(
            slots=list(DraftSlot.objects.filter(season=season)),
            picks=list(DraftPick.objects.filter(season=season)),
            pool=pool,
        )

        self.assertEqual(len(result), 10 * ROUNDS)
        self.assertEqual(result[0].player_id, pool[0].pk)
        self.assertEqual(result[0].round, 1)
        self.assertEqual(result[-1].overall, 10 * ROUNDS)


class BoardViewTests(TestCase):
    """The draft board grid."""

    @classmethod
    def setUpTestData(cls):
        cls.roster_season = Season.objects.create(year=2025)
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)
        cls.isaac = cls.teams['Isaac']
        cls.marcus = cls.teams['Marcus']

    def setUp(self):
        self.user = get_user_model().objects.create_user('isaac', password='test-pass-1234')
        self.isaac.user = self.user
        self.isaac.save(update_fields=['user'])
        self.client.force_login(self.user)

    def test_board_renders_every_team_in_slot_order(self):
        response = self.client.get(reverse('board'))
        self.assertEqual(response.status_code, 200)

        owners = [s.team.owner_name for s in response.context['slots']]
        self.assertEqual(owners, OWNERS)

    def test_only_the_first_eight_rounds_show_by_default(self):
        """Keeper costs never exceed Round 8, so rounds 1-8 are the whole
        planning surface and fit a screen without scrolling."""
        rows = self.client.get(reverse('board')).context['rows']

        self.assertEqual(len(rows), 8)
        self.assertEqual([r['round'] for r in rows], list(range(1, 9)))

    def test_the_collapse_link_names_the_view_it_goes_back_to(self):
        """Expanded, the link must offer 1-8, not restate the 16 on screen."""
        html = self.client.get(reverse('board'), {'rounds': 'all'}).content.decode()

        self.assertIn('Show rounds 1&ndash;8', html)
        self.assertNotIn(f'Show rounds 1&ndash;{ROUNDS}', html)

    def test_the_toggle_expands_to_every_round(self):
        response = self.client.get(reverse('board'), {'rounds': 'all'})

        self.assertEqual(len(response.context['rows']), ROUNDS)
        self.assertTrue(response.context['show_all_rounds'])

    def test_cells_carry_a_snaked_pick_label(self):
        """"3.4" is round 3, fourth pick of that round -- and the fourth pick
        of an even round belongs to the far end of the board."""
        rows = self.client.get(reverse('board')).context['rows']
        first_round = rows[0]['cells']
        second_round = rows[1]['cells']

        # Round 1 runs left to right: column order matches pick order.
        self.assertEqual([c['label'] for c in first_round[:3]], ['1.1', '1.2', '1.3'])
        # Round 2 runs back the other way.
        self.assertEqual(second_round[0]['label'], '2.10')
        self.assertEqual(second_round[-1]['label'], '2.1')

    def test_rows_alternate_snake_direction(self):
        rows = self.client.get(reverse('board')).context['rows']
        self.assertTrue(rows[0]['forward'])       # round 1, left to right
        self.assertFalse(rows[1]['forward'])      # round 2, right to left

    def test_a_traded_pick_is_badged_with_its_current_owner(self):
        pick = DraftPick.objects.get(
            season=self.season, round=4, original_team=self.marcus
        )
        PickTrade.objects.create(
            season=self.season, pick=pick, from_team=self.marcus,
            to_team=self.isaac, date='2026-01-15',
        )

        response = self.client.get(reverse('board'))
        cell = self.cell_for(response, round_number=4, team=self.marcus)

        # Stays in Marcus's column, badged with the new owner.
        self.assertEqual(cell['traded_to'], self.isaac)
        self.assertContains(response, '&rarr; Isaac')

    def cell_for(self, response, round_number, team):
        slots = response.context['slots']
        column = [s.team_id for s in slots].index(team.pk)
        row = next(r for r in response.context['rows'] if r['round'] == round_number)
        return row['cells'][column]

    def names_in(self, response, round_number, team):
        cell = self.cell_for(response, round_number, team)
        return [c['entry'].player.name for c in cell['candidates']]

    def test_candidates_are_listed_by_current_year_cost(self):
        make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        response = self.client.get(reverse('board'))

        self.assertIn('Jaxon Smith-Njigba', self.names_in(response, 4, self.marcus))
        self.assertContains(response, 'Jaxon Smith-Njigba')

    def test_a_candidate_lands_where_the_missing_pick_rule_puts_him(self):
        """Regression: candidates used to be placed at their base cost round,
        even when the team no longer owned a pick there. Marcus traded his R4
        to Isaac, so keeping JSN deterministically burns his R3."""
        pick = DraftPick.objects.get(season=self.season, round=4, original_team=self.marcus)
        PickTrade.objects.create(
            season=self.season, pick=pick, from_team=self.marcus,
            to_team=self.isaac, date='2026-01-15',
        )
        make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)

        response = self.client.get(reverse('board'))

        self.assertNotIn('Jaxon Smith-Njigba', self.names_in(response, 4, self.marcus))
        self.assertIn('Jaxon Smith-Njigba', self.names_in(response, 3, self.marcus))

    def test_a_shifted_candidate_is_annotated_with_his_real_cost(self):
        pick = DraftPick.objects.get(season=self.season, round=4, original_team=self.marcus)
        PickTrade.objects.create(
            season=self.season, pick=pick, from_team=self.marcus,
            to_team=self.isaac, date='2026-01-15',
        )
        make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)

        response = self.client.get(reverse('board'))
        cand = self.cell_for(response, 3, self.marcus)['candidates'][0]

        self.assertTrue(cand['walked'])
        self.assertEqual(cand['cost_round'], 4)
        self.assertEqual(cand['placed_round'], 3)
        self.assertContains(response, 'no longer owns that pick')

    def test_an_unaffected_candidate_stays_at_his_base_cost(self):
        """The trade must not disturb players whose own round is intact."""
        pick = DraftPick.objects.get(season=self.season, round=4, original_team=self.marcus)
        PickTrade.objects.create(
            season=self.season, pick=pick, from_team=self.marcus,
            to_team=self.isaac, date='2026-01-15',
        )
        make_entry(self.roster_season, self.marcus, 'Normal Guy', 6)

        response = self.client.get(reverse('board'))
        self.assertIn('Normal Guy', self.names_in(response, 6, self.marcus))
        self.assertFalse(self.cell_for(response, 6, self.marcus)['candidates'][0]['walked'])

    def test_the_receiving_team_places_candidates_in_a_round_it_owns_twice(self):
        """Isaac now owns two R4 picks. His R4-cost players still land on R4 --
        the extra pick changes nothing about placement."""
        pick = DraftPick.objects.get(season=self.season, round=4, original_team=self.marcus)
        PickTrade.objects.create(
            season=self.season, pick=pick, from_team=self.marcus,
            to_team=self.isaac, date='2026-01-15',
        )
        make_entry(self.roster_season, self.isaac, 'My R4 Guy', 4)

        response = self.client.get(reverse('board'))
        cand = self.cell_for(response, 4, self.isaac)['candidates'][0]

        self.assertEqual(cand['entry'].player.name, 'My R4 Guy')
        self.assertFalse(cand['walked'])
        # The cell shown is Isaac's own R4 slot, not the one acquired from Marcus.
        self.assertEqual(cand['placed_round'], 4)

    def test_sidebar_shows_the_shifted_cost_too(self):
        """Sidebar and grid must agree, or the manager sees two answers."""
        pick = DraftPick.objects.get(season=self.season, round=4, original_team=self.isaac)
        PickTrade.objects.create(
            season=self.season, pick=pick, from_team=self.isaac,
            to_team=self.marcus, date='2026-01-15',
        )
        make_entry(self.roster_season, self.isaac, 'My R4 Guy', 4)

        row = self.client.get(reverse('board')).context['sandbox_players'][0]
        self.assertEqual(row['cost'].cost_round, 4)
        self.assertEqual(row['burn_round'], 3)
        self.assertTrue(row['walked'])

    def test_an_entered_declaration_does_not_shift_candidates_before_reveal(self):
        """Rules section 1: declarations stay secret until the reveal. The
        commissioner enters all ten teams before flipping the switch, so a
        forfeited pick must not visibly move that team's candidate cells --
        that would leak who had already declared."""
        declared = make_entry(self.roster_season, self.isaac, 'Declared Guy', 2)
        KeeperSelection.objects.create(
            season=self.season, team=self.isaac, roster_entry=declared
        )
        engine.recompute_team_selections(self.isaac, self.season)
        self.assertTrue(
            DraftPick.objects.get(season=self.season, round=2, original_team=self.isaac).forfeited
        )

        other = make_entry(self.roster_season, self.isaac, 'Other R2 Guy', 2)
        response = self.client.get(reverse('board'))

        # Still on R2, not pushed to R1 by the hidden declaration.
        self.assertIn(other.player.name, self.names_in(response, 2, self.isaac))
        self.assertFalse(
            any(c['walked'] for c in self.cell_for(response, 2, self.isaac)['candidates'])
        )

    def test_ineligible_players_are_left_off_the_board(self):
        make_entry(self.roster_season, self.marcus, 'Bench Guy', 4, eligible=False)
        response = self.client.get(reverse('board'))

        cell = self.cell_for(response, round_number=4, team=self.marcus)
        self.assertEqual(cell['candidates'], [])

    def test_pending_players_are_shown_but_marked(self):
        entry = make_entry(self.roster_season, self.marcus, 'Unreviewed Guy', 4)
        entry.eligible = None
        entry.save(update_fields=['eligible'])

        response = self.client.get(reverse('board'))
        self.assertContains(response, 'Unreviewed Guy')
        self.assertContains(response, 'pending')

    def test_candidates_are_capped_at_three_with_an_expander(self):
        for i in range(5):
            make_entry(self.roster_season, self.marcus, f'Guy {i}', 4)

        cell = self.cell_for(self.client.get(reverse('board')), 4, self.marcus)
        self.assertEqual(len(cell['candidates']), 3)
        self.assertEqual(len(cell['extra_candidates']), 2)

    def test_revealed_board_shows_keepers_and_hides_candidates(self):
        entry = make_entry(self.roster_season, self.isaac, 'Rashee Rice', 8)
        KeeperSelection.objects.create(
            season=self.season, team=self.isaac, roster_entry=entry
        )
        engine.recompute_team_selections(self.isaac, self.season)

        self.season.keepers_revealed = True
        self.season.save(update_fields=['keepers_revealed'])

        response = self.client.get(reverse('board'))
        cell = self.cell_for(response, round_number=8, team=self.isaac)

        self.assertTrue(response.context['revealed'])
        self.assertIsNotNone(cell['keeper'])
        self.assertEqual(cell['keeper'].player.name, 'Rashee Rice')
        self.assertEqual(cell['candidates'], [])

    def test_the_board_offers_the_simulate_control(self):
        html = self.client.get(reverse('board')).content.decode()

        self.assertIn('id="simulate-btn"', html)
        self.assertIn(f'data-simulate-url="{reverse("simulate")}"', html)
        # The sandbox selection must reach the endpoint in a POST body, so the
        # control carries a CSRF token rather than a link.
        self.assertIn('csrfmiddlewaretoken', html)

    def test_the_sandbox_is_keyed_by_season_for_browser_storage(self):
        """Ticks survive the redirect a prediction lock causes, per tab."""
        html = self.client.get(reverse('board')).content.decode()

        self.assertIn(f'data-season="{self.season.year}"', html)
        self.assertIn('this browser tab only', html)

    def test_the_board_offers_the_mock_draft_controls(self):
        html = self.client.get(reverse('board')).content.decode()

        self.assertIn('id="mock-btn"', html)
        self.assertIn('id="undo-pick-btn"', html)
        self.assertIn('id="pick-modal"', html)

    def test_an_account_without_a_team_gets_no_mock_controls(self):
        """There are no picks of your own to make, so there is nothing to open."""
        self.isaac.user = None
        self.isaac.save(update_fields=['user'])

        html = self.client.get(reverse('board')).content.decode()
        self.assertNotIn('id="mock-btn"', html)
        self.assertNotIn('id="pick-modal"', html)
        self.assertIn('id="simulate-btn"', html)      # full auto still offered

    def test_the_board_script_loads_even_without_a_sandbox(self):
        """The simulate button is on the page for everyone, sandbox or not."""
        self.season.keepers_revealed = True
        self.season.save(update_fields=['keepers_revealed'])

        html = self.client.get(reverse('board')).content.decode()
        self.assertIn('league/board.js', html)
        self.assertIn('id="simulate-btn"', html)

    def test_the_sandbox_is_hidden_once_keepers_are_revealed(self):
        make_entry(self.roster_season, self.isaac, 'Rashee Rice', 8)
        self.assertContains(self.client.get(reverse('board')), 'id="sandbox"')

        self.season.keepers_revealed = True
        self.season.save(update_fields=['keepers_revealed'])

        self.assertNotContains(self.client.get(reverse('board')), 'id="sandbox"')

    def test_sandbox_lists_only_the_managers_own_eligible_players(self):
        mine = make_entry(self.roster_season, self.isaac, 'My Guy', 5)
        make_entry(self.roster_season, self.isaac, 'My Bench Guy', 5, eligible=False)
        make_entry(self.roster_season, self.marcus, 'Their Guy', 5)

        players = self.client.get(reverse('board')).context['sandbox_players']
        self.assertEqual([p['entry'].pk for p in players], [mine.pk])


class KeeperPredictionTests(TestCase):
    """Part C: private per-user calls on other teams' cells."""

    @classmethod
    def setUpTestData(cls):
        cls.roster_season = Season.objects.create(year=2025)
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)
        cls.isaac = cls.teams['Isaac']
        cls.marcus = cls.teams['Marcus']

    def setUp(self):
        self.user = get_user_model().objects.create_user('isaac', password='test-pass-1234')
        self.isaac.user = self.user
        self.isaac.save(update_fields=['user'])
        self.client.force_login(self.user)

    def lock(self, entry, follow=False):
        return self.client.post(
            reverse('toggle_prediction'), {'lock': entry.pk, 'next': reverse('board')},
            follow=follow,
        )

    def unlock(self, entry):
        return self.client.post(
            reverse('toggle_prediction'), {'unlock': entry.pk, 'next': reverse('board')}
        )

    def cell_for(self, response, round_number, team):
        column = [s.team_id for s in response.context['slots']].index(team.pk)
        row = next(r for r in response.context['rows'] if r['round'] == round_number)
        return row['cells'][column]

    # -- locking ------------------------------------------------------------

    def test_locking_a_rivals_player_fills_its_cost_cell(self):
        jsn = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        self.lock(jsn)

        cell = self.cell_for(self.client.get(reverse('board')), 4, self.marcus)
        self.assertIsNotNone(cell['prediction'])
        self.assertEqual(cell['prediction']['entry'], jsn)

    def test_a_locked_player_stops_being_offered_as_a_candidate(self):
        jsn = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        self.lock(jsn)

        cell = self.cell_for(self.client.get(reverse('board')), 4, self.marcus)
        self.assertNotIn(jsn.pk, [c['entry'].pk for c in cell['candidates']])

    def test_unlocking_restores_the_candidate(self):
        jsn = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        self.lock(jsn)
        self.unlock(jsn)

        cell = self.cell_for(self.client.get(reverse('board')), 4, self.marcus)
        self.assertIsNone(cell['prediction'])
        self.assertIn(jsn.pk, [c['entry'].pk for c in cell['candidates']])

    def test_locking_twice_does_not_duplicate(self):
        jsn = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        self.lock(jsn)
        self.lock(jsn)

        self.assertEqual(KeeperPrediction.objects.count(), 1)

    def test_two_predictions_at_the_same_cost_round_collide(self):
        """Predictions run through the engine as a SET, so the second guess at
        Round 8 walks to Round 7 exactly as a real keeper pair would."""
        first = make_entry(self.roster_season, self.marcus, 'Rashee Clone', 8)
        second = make_entry(self.roster_season, self.marcus, 'Khalil Clone', 10)
        self.lock(first)
        self.lock(second)

        response = self.client.get(reverse('board'))
        rounds = {
            r: self.cell_for(response, r, self.marcus)['prediction'] for r in (7, 8)
        }
        self.assertIsNotNone(rounds[8])
        self.assertIsNotNone(rounds[7])
        self.assertEqual(rounds[7]['via'], engine.VIA_COLLISION)

    def test_an_illegal_predicted_set_warns_but_still_locks(self):
        """You are allowed to predict that a rival does something illegal."""
        jeanty = make_entry(self.roster_season, self.marcus, 'Ashton Clone', 2)
        bowers = make_entry(self.roster_season, self.marcus, 'Brock Clone', 2)
        self.lock(jeanty)
        self.lock(bowers)

        response = self.client.get(reverse('board'))
        self.assertEqual(KeeperPrediction.objects.count(), 2)
        warnings = response.context['team_warnings'].get(self.marcus.pk)
        self.assertTrue(warnings)
        self.assertTrue(any('Rounds 1-2' in w for w in warnings))

    # -- privacy ------------------------------------------------------------

    def test_a_users_predictions_are_invisible_to_everyone_else(self):
        """The whole point: nobody can see what anyone else has called."""
        jsn = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        self.lock(jsn)

        other_user = get_user_model().objects.create_user('chris', password='test-pass-1234')
        self.teams['Chris'].user = other_user
        self.teams['Chris'].save(update_fields=['user'])

        self.client.force_login(other_user)
        response = self.client.get(reverse('board'))

        self.assertIsNone(self.cell_for(response, 4, self.marcus)['prediction'])
        self.assertEqual(response.context['prediction_count'], 0)

    def test_one_user_cannot_unlock_anothers_prediction(self):
        jsn = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        self.lock(jsn)

        other_user = get_user_model().objects.create_user('chris', password='test-pass-1234')
        self.client.force_login(other_user)
        self.client.post(
            reverse('toggle_prediction'), {'unlock': jsn.pk, 'next': reverse('board')}
        )

        self.assertTrue(
            KeeperPrediction.objects.filter(user=self.user, roster_entry=jsn).exists()
        )

    def test_two_users_can_predict_the_same_player_independently(self):
        jsn = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        self.lock(jsn)

        other_user = get_user_model().objects.create_user('chris', password='test-pass-1234')
        self.client.force_login(other_user)
        self.lock(jsn)

        self.assertEqual(KeeperPrediction.objects.filter(roster_entry=jsn).count(), 2)

    # -- own team is excluded ----------------------------------------------

    def test_a_manager_cannot_lock_a_player_on_their_own_team(self):
        """Rules section 1: a real keeper plan must not exist in the database
        before the deadline. Own-team planning stays in the sandbox."""
        mine = make_entry(self.roster_season, self.isaac, 'My Guy', 4)
        self.lock(mine)

        self.assertEqual(KeeperPrediction.objects.count(), 0)

    def test_own_team_cells_are_not_clickable(self):
        make_entry(self.roster_season, self.isaac, 'My Guy', 4)
        make_entry(self.roster_season, self.marcus, 'Their Guy', 4)

        response = self.client.get(reverse('board'))
        self.assertFalse(self.cell_for(response, 4, self.isaac)['predictable'])
        self.assertTrue(self.cell_for(response, 4, self.marcus)['predictable'])

    # -- endpoint hygiene ---------------------------------------------------

    def test_anonymous_users_cannot_toggle(self):
        jsn = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        self.client.logout()
        self.client.post(reverse('toggle_prediction'), {'lock': jsn.pk})

        self.assertEqual(KeeperPrediction.objects.count(), 0)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(reverse('toggle_prediction')).status_code, 405)

    def test_an_off_site_next_is_ignored(self):
        """A form-supplied redirect target must never leave the site."""
        jsn = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        response = self.client.post(
            reverse('toggle_prediction'),
            {'lock': jsn.pk, 'next': 'https://evil.example.com/steal'},
        )
        self.assertEqual(response['Location'], reverse('board'))

    def test_a_good_next_is_honoured(self):
        jsn = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        response = self.client.post(
            reverse('toggle_prediction'),
            {'lock': jsn.pk, 'next': '/board/?rounds=all'},
        )
        self.assertEqual(response['Location'], '/board/?rounds=all')


class KeeperPreviewApiTests(TestCase):
    """POST /api/keeper-preview/ -- the sandbox's engine call."""

    @classmethod
    def setUpTestData(cls):
        cls.roster_season = Season.objects.create(year=2025)
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)
        cls.isaac = cls.teams['Isaac']
        cls.marcus = cls.teams['Marcus']

    def setUp(self):
        self.user = get_user_model().objects.create_user('isaac', password='test-pass-1234')
        self.isaac.user = self.user
        self.isaac.save(update_fields=['user'])
        self.client.force_login(self.user)

    def preview(self, entries):
        return self.client.post(
            reverse('keeper_preview'),
            data=json.dumps({'entry_ids': [e.pk for e in entries]}),
            content_type='application/json',
        )

    def test_anonymous_users_are_rejected(self):
        self.client.logout()
        response = self.client.post(
            reverse('keeper_preview'), data='{}', content_type='application/json'
        )
        self.assertIn(response.status_code, (302, 403))
        if response.status_code == 302:
            self.assertIn(reverse('login'), response.url)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(reverse('keeper_preview')).status_code, 405)

    def test_a_simple_set_burns_its_own_cost_round(self):
        entry = make_entry(self.roster_season, self.isaac, 'Ladd McConkey', 3)
        data = self.preview([entry]).json()

        self.assertTrue(data['valid'])
        self.assertEqual(len(data['burned']), 1)
        self.assertEqual(data['burned'][0]['round'], 3)
        self.assertEqual(data['burned'][0]['via'], engine.VIA_BASE)
        self.assertEqual(data['burned'][0]['player'], 'Ladd McConkey')

    def test_two_round_8_keepers_collide_onto_round_7(self):
        rice = make_entry(self.roster_season, self.isaac, 'Rashee Rice', 8)
        shakir = make_entry(self.roster_season, self.isaac, 'Khalil Shakir', 10)

        data = self.preview([rice, shakir]).json()
        self.assertTrue(data['valid'])

        by_round = {b['round']: b for b in data['burned']}
        self.assertEqual(sorted(by_round), [7, 8])
        self.assertEqual(by_round[8]['via'], engine.VIA_BASE)
        self.assertEqual(by_round[7]['via'], engine.VIA_COLLISION)

    def test_a_missing_pick_walks_to_the_next_earlier_round(self):
        """Marcus keeps JSN (R4) after trading his R4 to Isaac -> burns his R3."""
        marcus_user = get_user_model().objects.create_user('marcus', password='test-pass-1234')
        self.marcus.user = marcus_user
        self.marcus.save(update_fields=['user'])

        pick = DraftPick.objects.get(season=self.season, round=4, original_team=self.marcus)
        PickTrade.objects.create(
            season=self.season, pick=pick, from_team=self.marcus,
            to_team=self.isaac, date='2026-01-15',
        )
        jsn = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)

        self.client.force_login(marcus_user)
        data = self.preview([jsn]).json()

        self.assertTrue(data['valid'])
        self.assertEqual(data['burned'][0]['round'], 3)
        self.assertEqual(data['burned'][0]['cost_round'], 4)
        self.assertEqual(data['burned'][0]['via'], engine.VIA_MISSING)

    def test_more_than_three_keepers_is_rejected(self):
        entries = [make_entry(self.roster_season, self.isaac, f'Guy {i}', 9) for i in range(4)]
        response = self.preview(entries)

        self.assertEqual(response.status_code, 400)
        self.assertIn('at most 3', response.json()['error'])

    def test_a_manager_cannot_preview_another_teams_players(self):
        """The endpoint scopes the lookup to request.user's own team, so
        another team's entry ids simply do not resolve."""
        theirs = make_entry(self.roster_season, self.marcus, 'Their Guy', 4)
        response = self.preview([theirs])

        self.assertEqual(response.status_code, 403)
        self.assertNotIn('Their Guy', response.content.decode())

    def test_mixing_in_another_teams_player_is_rejected(self):
        mine = make_entry(self.roster_season, self.isaac, 'My Guy', 5)
        theirs = make_entry(self.roster_season, self.marcus, 'Their Guy', 4)

        self.assertEqual(self.preview([mine, theirs]).status_code, 403)

    def test_illegal_sets_return_errors_rather_than_failing(self):
        jeanty = make_entry(self.roster_season, self.isaac, 'Ashton Jeanty', 2)
        bowers = make_entry(self.roster_season, self.isaac, 'Brock Bowers', 2)

        data = self.preview([jeanty, bowers]).json()
        self.assertFalse(data['valid'])
        self.assertTrue(any('Rounds 1-2' in e for e in data['errors']))

    def test_a_user_without_a_team_is_refused(self):
        stranger = get_user_model().objects.create_user('commish', password='test-pass-1234')
        self.client.force_login(stranger)
        response = self.client.post(
            reverse('keeper_preview'),
            data=json.dumps({'entry_ids': []}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_malformed_json_is_rejected_cleanly(self):
        response = self.client.post(
            reverse('keeper_preview'), data='not json', content_type='application/json'
        )
        self.assertEqual(response.status_code, 400)

    def test_response_shape_matches_what_board_js_reads(self):
        """Contract test. board.js reads these exact keys; renaming one would
        otherwise break the board silently, with no server-side error."""
        entry = make_entry(self.roster_season, self.isaac, 'Rashee Rice', 8)
        data = self.preview([entry]).json()

        self.assertEqual(set(data), {'valid', 'errors', 'warnings', 'burned'})
        self.assertEqual(
            set(data['burned'][0]),
            {'pick_id', 'round', 'cost_round', 'via', 'entry_id', 'player'},
        )

    def test_the_endpoint_never_writes(self):
        """It is a sandbox: declarations happen by text, not here."""
        entry = make_entry(self.roster_season, self.isaac, 'Rashee Rice', 8)
        self.preview([entry])

        self.assertEqual(KeeperSelection.objects.count(), 0)
        self.assertEqual(
            DraftPick.objects.filter(season=self.season, forfeited=True).count(), 0
        )


class RevealInterplayTests(TestCase):
    """Part E: what predictions become once the real declarations land."""

    @classmethod
    def setUpTestData(cls):
        cls.roster_season = Season.objects.create(year=2025)
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)
        cls.isaac = cls.teams['Isaac']
        cls.marcus = cls.teams['Marcus']

    def setUp(self):
        self.user = get_user_model().objects.create_user('isaac', password='test-pass-1234')
        self.isaac.user = self.user
        self.isaac.save(update_fields=['user'])
        self.client.force_login(self.user)

    def reveal(self):
        self.season.keepers_revealed = True
        self.season.save(update_fields=['keepers_revealed'])

    def declare(self, team, entry):
        KeeperSelection.objects.create(season=self.season, team=team, roster_entry=entry)
        engine.recompute_team_selections(team, self.season)

    def predict(self, entry):
        return KeeperPrediction.objects.create(
            user=self.user, season=self.season, roster_entry=entry
        )

    def cell_for(self, response, round_number, team):
        column = [s.team_id for s in response.context['slots']].index(team.pk)
        row = next(r for r in response.context['rows'] if r['round'] == round_number)
        return row['cells'][column]

    def test_a_correct_call_is_ticked(self):
        entry = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        self.predict(entry)
        self.declare(self.marcus, entry)
        self.reveal()

        response = self.client.get(reverse('board'))
        cell = self.cell_for(response, 4, self.marcus)

        self.assertIsNotNone(cell['keeper'])
        self.assertTrue(cell['called'])
        self.assertEqual(response.context['called_count'], 1)
        self.assertEqual(response.context['keeper_count'], 1)

    def test_a_keeper_nobody_predicted_is_not_ticked(self):
        entry = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        self.declare(self.marcus, entry)
        self.reveal()

        cell = self.cell_for(self.client.get(reverse('board')), 4, self.marcus)
        self.assertFalse(cell['called'])

    def test_a_wrong_call_no_longer_occupies_a_cell(self):
        """The real declarations own the revealed board; guesses step aside."""
        wrong = make_entry(self.roster_season, self.marcus, 'Wrong Guess', 6)
        actual = make_entry(self.roster_season, self.marcus, 'Actually Kept', 4)
        self.predict(wrong)
        self.declare(self.marcus, actual)
        self.reveal()

        response = self.client.get(reverse('board'))
        self.assertIsNone(self.cell_for(response, 6, self.marcus)['prediction'])
        self.assertEqual(response.context['called_count'], 0)
        self.assertEqual(response.context['prediction_count'], 1)

    def test_predictions_are_read_only_after_the_reveal(self):
        entry = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        self.predict(entry)
        self.declare(self.marcus, entry)
        self.reveal()

        html = self.client.get(reverse('board')).content.decode()
        self.assertNotIn('Unlock this pick', html)
        self.assertNotIn('name="lock"', html)

    def test_no_cell_is_predictable_after_the_reveal(self):
        entry = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        self.declare(self.marcus, entry)
        self.reveal()

        response = self.client.get(reverse('board'))
        every_cell = [c for row in response.context['rows'] for c in row['cells'] if c]
        self.assertFalse(any(cell['predictable'] for cell in every_cell))

    def test_the_scorecard_stays_private(self):
        """It counts only this user's calls -- another manager's are invisible."""
        entry = make_entry(self.roster_season, self.marcus, 'Jaxon Smith-Njigba', 4)
        rival = get_user_model().objects.create_user('rival', password='test-pass-1234')
        self.teams['Chris'].user = rival
        self.teams['Chris'].save(update_fields=['user'])
        KeeperPrediction.objects.create(user=rival, season=self.season, roster_entry=entry)

        self.declare(self.marcus, entry)
        self.reveal()

        response = self.client.get(reverse('board'))
        self.assertEqual(response.context['called_count'], 0)
        self.assertFalse(self.cell_for(response, 4, self.marcus)['called'])


class SimulateApiTests(TestCase):
    """POST /board/simulate/ -- the ADP autofill endpoint."""

    @classmethod
    def setUpTestData(cls):
        cls.roster_season = Season.objects.create(year=2025)
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)
        cls.isaac = cls.teams['Isaac']
        cls.marcus = cls.teams['Marcus']

        # A free-agent pool wide enough to fill all 160 cells, plus a clear
        # best-player-available at the top.
        cls.free_agents = [
            Player.objects.create(name=f'Free Agent {n}', position='RB', adp=float(n))
            for n in range(1, 200)
        ]
        # One real roster row, so latest_roster_season() has a season to find.
        # An unkept player goes back into the draft pool, which is why he is
        # not excluded from it here.
        make_entry(cls.roster_season, cls.teams['Luke'], 'Rostered Guy', 5)

    def setUp(self):
        self.user = get_user_model().objects.create_user('isaac', password='test-pass-1234')
        self.isaac.user = self.user
        self.isaac.save(update_fields=['user'])
        self.client.force_login(self.user)

    def simulate(self, entries=()):
        return self.client.post(
            reverse('simulate'),
            data=json.dumps({'entry_ids': [e.pk for e in entries]}),
            content_type='application/json',
        )

    def other_user(self, team):
        user = get_user_model().objects.create_user('rival', password='test-pass-1234')
        team.user = user
        team.save(update_fields=['user'])
        return user

    # -- access control -----------------------------------------------------

    def test_anonymous_users_are_rejected(self):
        self.client.logout()
        response = self.client.post(
            reverse('simulate'), data='{}', content_type='application/json'
        )
        self.assertIn(response.status_code, (302, 403))

    def test_get_is_not_allowed(self):
        """The sandbox must never reach a URL, a Referer header or an access log."""
        self.assertEqual(self.client.get(reverse('simulate')).status_code, 405)

    def test_another_teams_player_in_the_sandbox_is_refused(self):
        theirs = make_entry(self.roster_season, self.marcus, 'Their Guy', 3)
        self.assertEqual(self.simulate([theirs]).status_code, 403)

    def test_more_than_three_sandbox_players_is_rejected(self):
        entries = [
            make_entry(self.roster_season, self.isaac, f'Guy {n}', 3) for n in range(4)
        ]
        self.assertEqual(self.simulate(entries).status_code, 400)

    def test_malformed_json_is_rejected_cleanly(self):
        response = self.client.post(
            reverse('simulate'), data='not json', content_type='application/json'
        )
        self.assertEqual(response.status_code, 400)

    def test_the_endpoint_never_writes(self):
        entry = make_entry(self.roster_season, self.isaac, 'Rashee Rice', 8)
        before = (KeeperSelection.objects.count(), KeeperPrediction.objects.count(),
                  DraftPick.objects.filter(forfeited=True).count(), Player.objects.count())

        self.simulate([entry])

        after = (KeeperSelection.objects.count(), KeeperPrediction.objects.count(),
                 DraftPick.objects.filter(forfeited=True).count(), Player.objects.count())
        self.assertEqual(before, after)

    # -- privacy ------------------------------------------------------------

    def test_another_users_predictions_never_reach_this_simulation(self):
        rival_pick = make_entry(self.roster_season, self.marcus, 'Predicted Guy', 3)
        KeeperPrediction.objects.create(
            user=self.other_user(self.teams['Chris']),
            season=self.season,
            roster_entry=rival_pick,
        )

        data = self.simulate().json()

        # Their call burned Marcus's Round 3 and took that player off the board
        # -- for them. Isaac's simulation must be untouched by it.
        marcus_r3 = DraftPick.objects.get(
            season=self.season, round=3, original_team=self.marcus
        )
        self.assertNotIn(marcus_r3.pk, data['burned'])
        self.assertIn(marcus_r3.pk, [fill['pick_id'] for fill in data['fills']])

    def test_my_own_predictions_do_reach_it(self):
        rival_pick = make_entry(self.roster_season, self.marcus, 'Predicted Guy', 3)
        KeeperPrediction.objects.create(
            user=self.user, season=self.season, roster_entry=rival_pick
        )

        data = self.simulate().json()

        marcus_r3 = DraftPick.objects.get(
            season=self.season, round=3, original_team=self.marcus
        )
        self.assertIn(marcus_r3.pk, data['burned'])
        self.assertNotIn(marcus_r3.pk, [fill['pick_id'] for fill in data['fills']])
        self.assertNotIn('Predicted Guy', [fill['player'] for fill in data['fills']])

    # -- the simulation itself ----------------------------------------------

    def test_every_unburned_cell_gets_a_projection(self):
        data = self.simulate().json()
        self.assertEqual(len(data['fills']), 10 * ROUNDS)
        self.assertEqual(data['burned'], [])

    def test_the_first_overall_pick_takes_the_best_adp_available(self):
        fills = {f['pick_id']: f for f in self.simulate().json()['fills']}
        first = DraftPick.objects.get(
            season=self.season, round=1, original_team=self.teams['Ricky']
        )
        self.assertEqual(fills[first.pk]['player'], 'Free Agent 1')

    def test_a_sandbox_keeper_burns_its_cell_and_leaves_the_pool(self):
        entry = make_entry(self.roster_season, self.isaac, 'Ladd McConkey', 3)
        entry.player.adp = 0.5          # would otherwise be the 1.01
        entry.player.save(update_fields=['adp'])

        data = self.simulate([entry]).json()

        isaac_r3 = DraftPick.objects.get(
            season=self.season, round=3, original_team=self.isaac
        )
        self.assertEqual(data['burned'], [isaac_r3.pk])
        self.assertNotIn(isaac_r3.pk, [fill['pick_id'] for fill in data['fills']])
        self.assertNotIn('Ladd McConkey', [fill['player'] for fill in data['fills']])

    def test_a_kept_quarterback_stops_the_projection_drafting_another(self):
        entry = make_entry(
            self.roster_season, self.isaac, 'Josh Allen', 3, position='QB'
        )
        Player.objects.create(name='Backup QB', position='QB', adp=0.1)

        fills = self.simulate([entry]).json()['fills']
        picks = {p.pk: p for p in DraftPick.objects.filter(season=self.season)}
        isaac_early = [
            fill for fill in fills
            if picks[fill['pick_id']].current_team_id == self.isaac.pk
            and picks[fill['pick_id']].round <= 8
        ]

        self.assertNotIn('QB', [fill['position'] for fill in isaac_early])
        # ...and somebody else happily takes him first overall.
        self.assertIn('Backup QB', [fill['player'] for fill in fills])

    def test_declared_keepers_are_invisible_until_the_reveal(self):
        """A forfeited pick before the reveal would announce the declaration."""
        entry = make_entry(self.roster_season, self.marcus, 'Declared Guy', 3)
        KeeperSelection.objects.create(
            season=self.season, team=self.marcus, roster_entry=entry
        )
        engine.recompute_team_selections(self.marcus, self.season)

        data = self.simulate().json()
        marcus_r3 = DraftPick.objects.get(
            season=self.season, round=3, original_team=self.marcus
        )
        self.assertNotIn(marcus_r3.pk, data['burned'])

    def test_after_the_reveal_declared_keepers_burn_their_cells(self):
        entry = make_entry(self.roster_season, self.marcus, 'Declared Guy', 3)
        KeeperSelection.objects.create(
            season=self.season, team=self.marcus, roster_entry=entry
        )
        engine.recompute_team_selections(self.marcus, self.season)
        self.season.keepers_revealed = True
        self.season.save(update_fields=['keepers_revealed'])

        data = self.simulate().json()
        marcus_r3 = DraftPick.objects.get(
            season=self.season, round=3, original_team=self.marcus
        )
        self.assertIn(marcus_r3.pk, data['burned'])
        self.assertNotIn('Declared Guy', [fill['player'] for fill in data['fills']])

    def test_response_shape_matches_what_board_js_reads(self):
        data = self.simulate().json()

        self.assertEqual(set(data), {'fills', 'burned', 'done', 'your_pick'})
        self.assertEqual(
            set(data['fills'][0]),
            {'pick_id', 'player', 'position', 'nfl_team', 'source'},
        )
        # Full auto never pauses, so the pool is not shipped.
        self.assertTrue(data['done'])
        self.assertIsNone(data['your_pick'])


class MockDraftApiTests(TestCase):
    """POST /board/simulate/ with mock=true -- the step endpoint."""

    @classmethod
    def setUpTestData(cls):
        cls.roster_season = Season.objects.create(year=2025)
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)
        cls.isaac = cls.teams['Isaac']
        cls.marcus = cls.teams['Marcus']

        for n in range(1, 200):
            Player.objects.create(name=f'Free Agent {n}', position='RB', adp=float(n))
        make_entry(cls.roster_season, cls.teams['Luke'], 'Rostered Guy', 5)

    def setUp(self):
        self.user = get_user_model().objects.create_user('isaac', password='test-pass-1234')
        self.isaac.user = self.user
        self.isaac.save(update_fields=['user'])
        self.client.force_login(self.user)

    def step(self, manual=None, entries=(), mock=True):
        return self.client.post(
            reverse('simulate'),
            data=json.dumps({
                'mock': mock,
                'manual_picks': manual or {},
                'entry_ids': [e.pk for e in entries],
            }),
            content_type='application/json',
        )

    def my_pick(self, round_number):
        return DraftPick.objects.get(
            season=self.season, round=round_number, original_team=self.isaac
        )

    # -- access control -----------------------------------------------------

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(reverse('simulate')).status_code, 405)

    def test_anonymous_users_are_rejected(self):
        self.client.logout()
        response = self.step()
        self.assertIn(response.status_code, (302, 403))

    def test_a_user_without_a_team_cannot_mock_draft(self):
        """There is no 'my picks' without a team, and none may be named."""
        self.client.force_login(
            get_user_model().objects.create_user('commish', password='test-pass-1234')
        )
        self.assertEqual(self.step().status_code, 403)

    def test_the_team_is_derived_from_the_session(self):
        """Nothing in the body names a team, so nobody can draft for a rival."""
        data = self.step().json()
        self.assertEqual(data['your_pick']['pick_id'], self.my_pick(1).pk)

    # -- stepping -----------------------------------------------------------

    def test_the_first_step_stops_at_my_first_pick(self):
        data = self.step().json()

        self.assertFalse(data['done'])
        self.assertEqual(data['your_pick']['round'], 1)
        # Isaac drafts third, so two chalk picks are on the board already.
        self.assertEqual(len(data['fills']), 2)
        self.assertEqual(data['your_pick']['label'], '1.3')

    def test_the_step_ships_suggestions_and_the_available_pool(self):
        data = self.step().json()

        self.assertEqual(len(data['your_pick']['suggestions']), 5)
        self.assertEqual(
            set(data['your_pick']['suggestions'][0]),
            {'id', 'name', 'position', 'nfl_team', 'adp'},
        )
        # 199 free agents + the one rostered player, less the two already gone.
        self.assertEqual(data['available_count'], 198)
        self.assertEqual(data['available'][0]['name'], 'Free Agent 3')

    def test_my_choice_is_marked_manual_and_the_sim_moves_on(self):
        mine = self.my_pick(1)
        pick_me = Player.objects.get(name='Free Agent 50')

        data = self.step(manual={str(mine.pk): pick_me.pk}).json()

        chosen = next(f for f in data['fills'] if f['pick_id'] == mine.pk)
        self.assertEqual(chosen['player'], 'Free Agent 50')
        self.assertEqual(chosen['source'], draft_sim.SOURCE_MANUAL)
        self.assertEqual(data['your_pick']['round'], 2)

    def test_undo_is_just_a_shorter_dict(self):
        mine = self.my_pick(1)
        with_pick = self.step(manual={str(mine.pk): 50}).json()
        without = self.step().json()

        self.assertEqual(without['your_pick']['pick_id'], mine.pk)
        self.assertNotIn(mine.pk, [f['pick_id'] for f in without['fills']])
        self.assertNotEqual(with_pick['your_pick']['pick_id'], mine.pk)

    def test_choosing_every_pick_finishes_the_draft(self):
        manual = {}
        data = self.step().json()
        while not data['done']:
            manual[str(data['your_pick']['pick_id'])] = data['your_pick']['suggestions'][0]['id']
            data = self.step(manual=manual).json()

        self.assertEqual(len(data['fills']), 10 * ROUNDS)
        self.assertIsNone(data['your_pick'])
        self.assertNotIn('available', data)
        self.assertEqual(len(manual), ROUNDS)

    # -- refusals -----------------------------------------------------------

    def test_drafting_an_already_taken_player_is_refused(self):
        data = self.step().json()
        gone = data['fills'][0]['pick_id']
        taken_name = data['fills'][0]['player']
        taken = Player.objects.get(name=taken_name)

        response = self.step(manual={str(self.my_pick(1).pk): taken.pk})
        self.assertEqual(response.status_code, 400)
        self.assertIn('already been drafted', response.json()['error'])
        self.assertTrue(gone)

    def test_drafting_at_another_teams_pick_is_refused(self):
        theirs = DraftPick.objects.get(
            season=self.season, round=1, original_team=self.marcus
        )
        response = self.step(manual={str(theirs.pk): 50})

        self.assertEqual(response.status_code, 400)
        self.assertIn('not yours', response.json()['error'])

    def test_a_cell_burned_by_my_own_keeper_cannot_be_drafted_at(self):
        entry = make_entry(self.roster_season, self.isaac, 'Kept Guy', 3)
        mine_r3 = self.my_pick(3)

        response = self.step(manual={str(mine_r3.pk): 50}, entries=[entry])
        self.assertEqual(response.status_code, 400)
        self.assertIn('not yours', response.json()['error'])

    def test_the_sim_skips_over_my_burned_cell(self):
        entry = make_entry(self.roster_season, self.isaac, 'Kept Guy', 3)
        manual = {}
        data = self.step(entries=[entry]).json()
        stops = []
        while not data['done']:
            stops.append(data['your_pick']['round'])
            manual[str(data['your_pick']['pick_id'])] = data['your_pick']['suggestions'][0]['id']
            data = self.step(manual=manual, entries=[entry]).json()

        self.assertNotIn(3, stops)
        self.assertIn(self.my_pick(3).pk, data['burned'])

    def test_a_player_who_does_not_exist_is_refused(self):
        response = self.step(manual={str(self.my_pick(1).pk): 999999})
        self.assertEqual(response.status_code, 400)

    def test_suggestions_respect_the_caps_but_the_pool_does_not(self):
        """The caps shape the default list; search may reach past them."""
        Player.objects.create(name='Only Kicker', position='K', adp=0.1)
        data = self.step().json()

        offered = [s['name'] for s in data['your_pick']['suggestions']]
        self.assertNotIn('Only Kicker', offered)
        # ...but he is in the pool the modal searches, because drafting him is
        # my prerogative.
        self.assertIn('Only Kicker', [p['name'] for p in data['available']])

    def test_mock_mode_never_writes(self):
        before = (KeeperPrediction.objects.count(), KeeperSelection.objects.count(),
                  DraftPick.objects.filter(forfeited=True).count())
        self.step(manual={str(self.my_pick(1).pk): 50})
        after = (KeeperPrediction.objects.count(), KeeperSelection.objects.count(),
                 DraftPick.objects.filter(forfeited=True).count())

        self.assertEqual(before, after)

    def test_a_traded_pick_is_labelled_with_the_original_owners_slot(self):
        """Marcus's Round 4 to Isaac: the cell stays in Marcus's column."""
        traded = DraftPick.objects.get(
            season=self.season, round=4, original_team=self.marcus
        )
        traded.current_team = self.isaac
        traded.save(update_fields=['current_team'])

        manual = {}
        data = self.step().json()
        labels = []
        while not data['done']:
            labels.append(data['your_pick']['label'])
            manual[str(data['your_pick']['pick_id'])] = data['your_pick']['suggestions'][0]['id']
            data = self.step(manual=manual).json()

        # Isaac now picks twice in Round 4, and the two cells are far apart:
        # Round 4 is even so it runs backwards, putting Marcus's slot-10 pick
        # first in the round and Isaac's own slot-3 pick eighth.
        self.assertIn('4.1', labels)
        self.assertIn('4.8', labels)
        # His Round 3 is unaffected: odd round, slot 3, third pick.
        self.assertIn('3.3', labels)
        self.assertEqual(len(labels), ROUNDS + 1)      # one extra, from the trade


class StandingsTests(TestCase):
    """The league page is the previous season's final standings.

    Rules section 6: the 2026 draft order is the final 2025 standings reversed,
    which is the only reason this app can show standings it does not store.
    """

    @classmethod
    def setUpTestData(cls):
        cls.roster_season = Season.objects.create(year=2025)
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)
        # The standings hang off the *roster* season, so one real roster row is
        # what makes 2025 the season the page is about.
        make_entry(cls.roster_season, cls.teams['Isaac'], 'Rostered Guy', 5)

    def setUp(self):
        self.user = get_user_model().objects.create_user('isaac', password='test-pass-1234')
        self.teams['Isaac'].user = self.user
        self.teams['Isaac'].save(update_fields=['user'])
        self.client.force_login(self.user)

    def test_the_last_draft_slot_is_the_champion(self):
        standings = views.final_standings(self.roster_season)

        # OWNERS is the locked slot order: Ricky picks first, Marcus tenth.
        self.assertEqual(standings[self.teams['Marcus'].pk], 1)
        self.assertEqual(standings[self.teams['Ricky'].pk], 10)

    def test_teams_are_listed_in_finishing_order(self):
        response = self.client.get(reverse('league_overview'))
        owners = [team.owner_name for team in response.context['teams']]

        self.assertEqual(owners, list(reversed(OWNERS)))
        self.assertEqual(response.context['teams'][0].rank, 1)

    def test_the_page_and_nav_name_the_season(self):
        html = self.client.get(reverse('league_overview')).content.decode()

        self.assertIn('2025 Final Standings', html)
        self.assertIn('2025 Standings', html)          # the nav tab
        self.assertNotIn('>League<', html)

    def test_a_season_with_no_derivable_order_is_not_invented(self):
        """From 2027 the consolation bracket picks its own slots.

        The order stops being a reversal of anything then, so the derivation
        must return nothing rather than a plausible-looking lie.
        """
        later = Season.objects.create(year=2027)
        self.assertEqual(views.final_standings(later), {})

    def test_an_unranked_league_still_renders(self):
        DraftSlot.objects.all().delete()
        response = self.client.get(reverse('league_overview'))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['ranked'])
        self.assertEqual(len(response.context['teams']), 10)


class TeamDetailEligibilityTests(TestCase):
    """Another manager's roster shows eligibility, same as your own pages."""

    def setUp(self):
        self.roster_season = Season.objects.create(year=2025)
        self.user = get_user_model().objects.create_user('isaac', password='test-pass-1234')
        self.isaac = Team.objects.create(name='Zimbo Baggins', owner_name='Isaac', user=self.user)
        self.marcus = Team.objects.create(name='Shedeur for ROTY', owner_name='Marcus')
        self.client.force_login(self.user)

    def test_a_rivals_roster_shows_the_three_eligibility_states(self):
        make_entry(self.roster_season, self.marcus, 'Fine Player', 3)
        blocked = make_entry(self.roster_season, self.marcus, 'Hurt Player', 4, eligible=False)
        blocked.eligibility_note = 'added wk 14'
        blocked.save(update_fields=['eligibility_note'])
        pending = make_entry(self.roster_season, self.marcus, 'Unknown Player', 5)
        pending.eligible = None
        pending.save(update_fields=['eligible'])

        html = self.client.get(
            reverse('team_detail', args=[self.marcus.pk])
        ).content.decode()

        self.assertIn('pill-yes', html)
        self.assertIn('pill-no', html)
        self.assertIn('pill-pending', html)
        self.assertIn('added wk 14', html)


class ResetBoardTests(TestCase):
    """The reset button clears this user's locks and nobody else's."""

    @classmethod
    def setUpTestData(cls):
        cls.roster_season = Season.objects.create(year=2025)
        cls.season = Season.objects.create(year=2026)
        cls.teams = make_teams()
        make_draft(cls.season, cls.teams)
        cls.isaac = cls.teams['Isaac']
        cls.marcus = cls.teams['Marcus']

    def setUp(self):
        self.user = get_user_model().objects.create_user('isaac', password='test-pass-1234')
        self.isaac.user = self.user
        self.isaac.save(update_fields=['user'])
        self.client.force_login(self.user)
        self.entry = make_entry(self.roster_season, self.marcus, 'Locked Guy', 3)

    def lock_for(self, user):
        return KeeperPrediction.objects.create(
            user=user, season=self.season, roster_entry=self.entry
        )

    def test_reset_clears_my_locks(self):
        self.lock_for(self.user)
        self.client.post(reverse('reset_board'), {'next': reverse('board')})

        self.assertEqual(KeeperPrediction.objects.filter(user=self.user).count(), 0)

    def test_reset_leaves_another_users_locks_alone(self):
        rival = get_user_model().objects.create_user('rival', password='test-pass-1234')
        self.lock_for(rival)
        self.lock_for(self.user)

        self.client.post(reverse('reset_board'), {'next': reverse('board')})

        self.assertEqual(KeeperPrediction.objects.filter(user=rival).count(), 1)
        self.assertEqual(KeeperPrediction.objects.filter(user=self.user).count(), 0)

    def test_get_is_not_allowed(self):
        """A link would let any page wipe the board with an <img> tag."""
        self.assertEqual(self.client.get(reverse('reset_board')).status_code, 405)

    def test_anonymous_users_cannot_reset(self):
        self.client.logout()
        response = self.client.post(reverse('reset_board'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)

    def test_an_off_site_next_is_ignored(self):
        response = self.client.post(
            reverse('reset_board'), {'next': 'https://evil.example.com/'}
        )
        self.assertRedirects(response, reverse('board'))

    def test_the_board_offers_the_reset_control(self):
        html = self.client.get(reverse('board')).content.decode()
        self.assertIn('id="reset-board-form"', html)
        self.assertIn(reverse('reset_board'), html)

    def test_the_reset_control_is_gone_once_keepers_are_revealed(self):
        """Post-reveal the locks are history; there is nothing to edit."""
        self.season.keepers_revealed = True
        self.season.save(update_fields=['keepers_revealed'])

        html = self.client.get(reverse('board')).content.decode()
        self.assertNotIn('id="reset-board-form"', html)


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
            ('league_overview', []), ('my_team', []), ('team_switch', []),
            ('eligibility', []), ('rules', []), ('team_detail', [self.team.pk]),
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

    def test_the_team_page_shows_cost_and_eligibility(self):
        """The columns the old My Keepers page carried, now on every team."""
        player = Player.objects.create(name='Rashee Rice', position=Player.Position.WR)
        RosterEntry.objects.create(
            season=self.roster_season, team=self.team, player=player,
            draft_round=8, eligible=True,
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('team_detail', args=[self.team.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Rashee Rice')
        self.assertContains(response, 'Round 8')
        self.assertContains(response, 'Eligible')
        self.assertContains(response, 'Times kept')

    def test_the_team_page_marks_unreviewed_players_pending(self):
        player = Player.objects.create(name='Unknown Guy', position=Player.Position.RB)
        # eligible defaults to True, so "pending" now has to be set deliberately.
        RosterEntry.objects.create(
            season=self.roster_season, team=self.team, player=player,
            draft_round=5, eligible=None,
        )
        self.client.force_login(self.user)
        self.assertContains(
            self.client.get(reverse('team_detail', args=[self.team.pk])), 'Pending review'
        )

    def test_bare_teams_url_shows_my_own_team(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('team_switch'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['team'], self.team)
        self.assertTrue(response.context['is_own'])

    def test_the_picker_switches_teams_by_query_parameter(self):
        """What the dropdown submits -- a plain GET form, no JavaScript."""
        rival = Team.objects.create(name='Shedeur for ROTY', owner_name='Marcus')
        self.client.force_login(self.user)

        response = self.client.get(reverse('team_switch'), {'team': rival.pk})

        self.assertEqual(response.context['team'], rival)
        self.assertFalse(response.context['is_own'])
        self.assertContains(response, 'id="team-select"')

    def test_a_nonsense_team_parameter_falls_back_to_my_own(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('team_switch'), {'team': 'drop-table'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['team'], self.team)

    def test_the_board_is_the_front_page(self):
        self.client.force_login(self.user)
        self.assertEqual(reverse('board'), '/')

    def test_the_navigation_is_in_the_agreed_order(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse('rules')).content.decode()
        nav = html[html.index('<nav>'):html.index('</nav>')]

        order = ['Draft Board', 'Rules', 'My Team', 'Standings', 'Eligibility']
        found = [label for label in order if label in nav]
        self.assertEqual(found, order)
        self.assertEqual(sorted(nav.index(label) for label in order),
                         [nav.index(label) for label in order])
        self.assertNotIn('My Keepers', nav)


class EligibilityPageTests(TestCase):
    """The league-wide list of players nobody can keep."""

    def setUp(self):
        self.season = Season.objects.create(year=2025)
        self.user = get_user_model().objects.create_user('isaac', password='test-pass-1234')
        self.isaac = Team.objects.create(name='Zimbo Baggins', owner_name='Isaac', user=self.user)
        self.marcus = Team.objects.create(name='Shedeur for ROTY', owner_name='Marcus')
        self.client.force_login(self.user)

    def blocked(self, team, name, adp=None, eligible=False, position='RB'):
        entry = make_entry(self.season, team, name, 5, eligible=eligible, position=position)
        entry.player.adp = adp
        entry.player.save(update_fields=['adp'])
        return entry

    def test_only_players_who_cannot_be_kept_are_listed(self):
        self.blocked(self.marcus, 'Hurt Star', adp=12.0)
        make_entry(self.season, self.isaac, 'Perfectly Fine', 3)

        rows = self.client.get(reverse('eligibility')).context['rows']
        self.assertEqual([r.player.name for r in rows], ['Hurt Star'])

    def test_pending_players_are_listed_too(self):
        """The engine refuses an unreviewed keeper as firmly as a barred one."""
        pending = self.blocked(self.isaac, 'Unreviewed Guy', adp=40.0)
        pending.eligible = None
        pending.save(update_fields=['eligible'])

        response = self.client.get(reverse('eligibility'))
        self.assertEqual(response.context['pending_count'], 1)
        self.assertContains(response, 'Pending review')

    def test_rows_are_sorted_by_adp_with_the_unranked_last(self):
        self.blocked(self.marcus, 'Late Riser', adp=80.0)
        self.blocked(self.isaac, 'Nobody Knows Him')          # no ADP
        self.blocked(self.marcus, 'Early Star', adp=4.0)

        rows = self.client.get(reverse('eligibility')).context['rows']
        self.assertEqual(
            [r.player.name for r in rows],
            ['Early Star', 'Late Riser', 'Nobody Knows Him'],
        )

    def test_each_row_names_the_team_holding_him(self):
        self.blocked(self.marcus, 'Hurt Star', adp=12.0)
        html = self.client.get(reverse('eligibility')).content.decode()

        self.assertIn('Marcus', html)
        self.assertIn(reverse('team_detail', args=[self.marcus.pk]), html)

    def test_an_all_eligible_league_says_so(self):
        make_entry(self.season, self.isaac, 'Perfectly Fine', 3)
        self.assertContains(
            self.client.get(reverse('eligibility')), 'Every rostered player is keeper-eligible.'
        )

    def test_rules_page_renders_the_markdown_doc(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('rules'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Keeper Costs')
        # The tables extension must be on, or section 6's draft order is lost.
        self.assertContains(response, '<table>')
