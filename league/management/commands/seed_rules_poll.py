"""Write the 2026 rules ballot into the database.

    python manage.py seed_rules_poll
    python manage.py seed_rules_poll --season 2026

A management command rather than a data migration, and the distinction matters:
this is CONTENT. Content baked into migration history is content you cannot fix
a typo in without writing another migration, and a migration that edits prose is
a migration nobody can review. The commissioner can also just edit any of this
in the admin afterwards.

Which is why the command only ever CREATES. Re-running it fills in whatever is
missing and leaves everything that already exists alone, so a wording tightened
in the admin survives the next run. To genuinely re-seed a proposal, delete it
and run this again.

The ballot itself -- the three proposals, the arguments on both sides -- is
specified in docs/claude_code_prompts/07_rules_poll.md and tracked in
docs/league_votes.md. Nothing here touches docs/keeper_rules_v3.md: a passed
change is applied by hand-editing that file. See CLAUDE.md, "Rules votes".
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from league.models import RulesPoll, RulesProposal, Season
from league.views import keeper_season

INTRO = (
    'Three changes to Section 4, the roster-composition rules. Vote on each one '
    'separately — they are related but they stand or fall on their own. Your '
    'vote is hidden from everyone, including me, until the poll closes; then all '
    'of it becomes visible. Nothing changes for the 2026 draft unless it passes '
    'here first, and the ballot closes before the keeper declaration deadline so '
    'nobody is trading picks under rules that might move.'
)

EFFECTIVE_NOTE = 'If passed, these take effect for the 2026 draft.'

# Not a proposal, and deliberately the last thing on the page: it is about what
# the three of them add up to, which is not a question you can vote on.
CLOSING_NOTE = (
    'One last thing, about all three together. With all three passed, the only '
    'keeper shape the rules forbid is two cheap players — it becomes easier to '
    'keep three premium players than three sleepers. That sounds backwards until '
    'you price it: three premium keepers cost picks 1, 2 and 3, and three '
    'sleepers cost 8, 7 and 6. Saying it here because someone will raise it, and '
    'it is better raised by the ballot than in the group chat.'
)

# Each proposal argues BOTH sides. The commissioner is proposing all three of
# these and also votes on them; a ballot that only argues one side is a leaflet.
# `note` is neither side -- an interaction with another proposal, or a definition
# that stops an argument in 2027.
PROPOSALS = [
    {
        'order': 1,
        'title': 'Allow more than one keeper costing Rounds 1–2',
        'rule_reference': 'Section 4, first bullet',
        'current_text': (
            'Only 1 keeper may have a current-year keeper cost in Rounds 1–2.'
        ),
        'proposed_text': (
            '(deleted — no limit on how many keepers carry a Rounds 1–2 cost.)'
        ),
        'case_for': (
            'The price is already the price. Two Round-2-cost keepers cost you '
            'your Round 2 and — by the same-round collision rule in Section 3 — '
            'your Round 1 as well. You do not pick until Round 3. Nobody is '
            'getting premium players for free here, and the limit is doing work '
            'the cost rules already do.\n\n'
            'Escalation makes it self-limiting: a Round 1 cost cannot be kept '
            'again at all, and a Round 2 cost becomes a Round 1 the next year and '
            'then dies. This is not something that compounds year after year.'
        ),
        'case_against': (
            'It concentrates keeper value at the top. With this and Proposal 2 '
            'both passed, the most aggressive legal set becomes a Round 1 + a '
            'Round 2 + a Round 3 cost — a team returning three high picks and not '
            'drafting until Round 4.\n\n'
            'And the current rule is what stops the commissioner keeping both '
            'Jeanty and Bowers this year, so he benefits from it passing. That is '
            'disclosure, not an argument either way, but it belongs on the ballot.'
        ),
        'note': (
            'Worth knowing: two Round-1-cost keepers stay impossible whatever '
            'happens here. The second one would need a pick earlier than Round 1, '
            'and Section 3 says if you own no pick in the cost round or any '
            'earlier round, you cannot keep that player.'
        ),
    },
    {
        'order': 2,
        'title': 'Drop the requirement that a 3-keeper set include a Round 8+ keeper',
        'rule_reference': 'Section 4, second bullet',
        'current_text': (
            'If keeping 3 players, at least one keeper must have a current-year '
            'cost of Round 8 or later.'
        ),
        'proposed_text': '(deleted.)',
        'case_for': (
            'It punishes teams that drafted well in the middle rounds by '
            'requiring them to hold a scrub they don\'t want in order to keep two '
            'players they do. A Round 3 + Round 4 + Round 5 set costs picks 3, 4 '
            'and 5 — that is a real price, and there is no reason the rules should '
            'refuse it.'
        ),
        'case_against': (
            'It is currently the only thing preventing an all-premium trio. '
            'Remove it and the ceiling on how top-heavy a keeper set can be is '
            'set entirely by what you are willing to pay.'
        ),
        'note': (
            'Worth knowing: this and Proposal 3 are not opposites. If Proposal 2 '
            'fails and Proposal 3 passes, a 3-keeper set must contain exactly one '
            'Round-8-or-later keeper — at least one from the rule that stayed, at '
            'most one from the rule that arrived. That is a coherent outcome, and '
            'possibly a good one, but vote for it on purpose rather than by '
            'accident.'
        ),
    },
    {
        'order': 3,
        'title': 'At most one keeper with a current-year cost of Round 8 or later',
        'rule_reference': 'Section 4, new bullet',
        'current_text': (
            '(no such limit — three Round-8-cost keepers are legal today, costing '
            'picks 8, 7 and 6 under the collision rule.)'
        ),
        'proposed_text': (
            'No more than 1 keeper may have a current-year keeper cost of Round 8 '
            'or later.'
        ),
        'case_for': (
            'Three sleepers for the tail of your draft is the best value on the '
            'board. Someone with two or three waiver-wire hits keeps all of them, '
            'pays picks 8, 7 and 6, and starts three quality players their draft '
            'never spent anything real on. Nothing in the rules currently prevents '
            'it and eventually someone will do it.'
        ),
        'case_against': (
            'It catches a legitimate Round 8 draft pick exactly as hard as a '
            'waiver pickup, which is not what the proposal is aimed at. And the '
            'Round 8 line is arbitrary — two Round-6-cost keepers cost picks 6 and '
            '5 and stay perfectly legal, which is nearly as cheap for nearly as '
            'much value.\n\n'
            'docs/league_votes.md has an Option C on this (tax the second late '
            'keeper at a Round 4 pick instead of Round 7) that is not on this '
            'ballot; if you prefer it, say so in the box at the bottom.'
        ),
        'note': (
            'Definition, so this doesn\'t become an argument in 2027: '
            '"current-year cost" means after escalation, the same convention '
            'Section 4 already uses. A waiver pickup on his second keep costs '
            'Round 7, so he no longer counts against this cap. That is intentional '
            '— he isn\'t cheap any more.'
        ),
    },
]


class Command(BaseCommand):
    help = 'Seed the rules ballot for the season being drafted. Safe to re-run.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--season', type=int, default=None,
            help='Year to seed. Defaults to the season being drafted.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        year = options['season']
        if year is None:
            season = keeper_season()
            if season is None:
                raise CommandError(
                    'No season is being drafted yet. Run seed_draft_order first, '
                    'or pass --season.'
                )
        else:
            season = Season.objects.filter(year=year).first()
            if season is None:
                raise CommandError(f'No season {year}. Run seed_draft_order first.')

        poll, created = RulesPoll.objects.get_or_create(
            season=season,
            defaults={
                'intro': INTRO,
                'effective_note': EFFECTIVE_NOTE,
                'closing_note': CLOSING_NOTE,
            },
        )
        self.stdout.write(
            f'  ballot for {season.year}: ' + ('created' if created else 'already there')
        )

        added = 0
        for spec in PROPOSALS:
            # Keyed on (poll, order), which is also the unique constraint -- so a
            # second run finds the row rather than colliding with it.
            _, made = RulesProposal.objects.get_or_create(
                poll=poll, order=spec['order'],
                defaults={key: value for key, value in spec.items() if key != 'order'},
            )
            added += made
            # ASCII only: the titles carry en dashes, and a Windows console in
            # its default code page cannot print one.
            self.stdout.write(
                f'  proposal {spec["order"]}: ' + ('written' if made else 'already there')
            )

        self.stdout.write(self.style.SUCCESS(
            f'{season.year} rules ballot ready ({len(PROPOSALS)} proposals, '
            f'{added} newly written). Open the admin to review before telling '
            f'anyone it is live.'
        ))
