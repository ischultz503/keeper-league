"""Load average draft position onto Player rows.

    python manage.py import_adp --csv data/raw/FantasyPros_ADP.csv   <- use this
    python manage.py import_adp                                      <- API, see below
    python manage.py import_adp --scoring PPR

PREFER THE CSV. FantasyPros has no ADP endpoint: ADP is folded into
consensus-rankings, and that resource returns expert consensus rank
(rank_ave/rank_ecr), not true ADP. Worse, on the free key tier the response
carries "public_api_limited": true and hard-caps at 10 players out of ~815;
limit and offset parameters are ignored. That is enough to prove the wiring
works and nowhere near enough to fill a draft board.

So the API path stays supported for a future paid tier, but a FantasyPros CSV
export is the practical source and gives real ADP rather than a rank proxy.

    GET https://api.fantasypros.com/public/v2/json/nfl/{season}/consensus-rankings
    header: x-api-key: <key>

The key is read from settings (which reads the environment, which is loaded from
the git-ignored .env). It is never printed, logged, or echoed in an error.

Idempotent: re-running overwrites adp/nfl_team on matched players and leaves
everything else alone, so a weekly refresh is safe.
"""

import csv
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from league.adp import build_index, find_player, normalize_position, split_player_and_team
from league.models import Player

API_URL = 'https://api.fantasypros.com/public/v2/json/nfl/{season}/consensus-rankings'
DEFAULT_SEASON = 2026

# Half-PPR is the default because CLAUDE.md does not record the league's
# scoring; pass --scoring STD or PPR if that is wrong. Scoring only reorders
# ADP, it does not change which players come back.
SCORING_CHOICES = ['STD', 'HALF', 'PPR']

# The API has changed field names over time, so read defensively and report
# which one was actually used rather than silently importing nothing.
NAME_KEYS = ['player_name', 'name', 'player']
POSITION_KEYS = ['player_position_id', 'position_id', 'position', 'pos']
TEAM_KEYS = ['player_team_id', 'team_id', 'player_team', 'team']
ADP_KEYS = ['adp', 'player_adp', 'rank_ave', 'avg', 'rank_avg']

CSV_NAME_KEYS = ['PLAYER NAME', 'Player (Bye)', 'Player', 'player', 'Name', 'PLAYER']
CSV_POSITION_KEYS = ['POS', 'Pos', 'Position', 'position']
CSV_TEAM_KEYS = ['TEAM', 'Team', 'team', 'Tm']

# FantasyPros publishes two different exports:
#   * an ADP export, with an AVG column holding true average draft position;
#   * a Rankings export, with only RK (expert consensus rank).
# Prefer real ADP. Fall back to the rank column ONLY when the file has no ADP
# column at all -- inside an ADP export, RK is the ordering and AVG is the
# measurement, so treating one as the other there would silently swap them.
CSV_ADP_KEYS = ['AVG', 'ADP', 'Avg', 'adp']
CSV_RANK_KEYS = ['RK', 'RANK', 'Rank', 'rk']


def first_value(row, keys):
    for key in keys:
        if key in row and row[key] not in (None, ''):
            return row[key]
    return None


def to_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


class Command(BaseCommand):
    help = 'Import average draft position from the FantasyPros API or a CSV export.'

    def add_arguments(self, parser):
        parser.add_argument('--season', type=int, default=DEFAULT_SEASON)
        parser.add_argument('--scoring', default='HALF', choices=SCORING_CHOICES)
        parser.add_argument('--csv', help='Import from a FantasyPros CSV export instead of the API.')
        parser.add_argument(
            '--dry-run', action='store_true', help='Report matches without saving.'
        )
        parser.add_argument(
            '--replace',
            action='store_true',
            help=(
                'Clear every existing ADP first, so this file becomes the only '
                'source. Use when switching between exports -- otherwise players '
                'absent from the new file keep values on a different scale.'
            ),
        )

    def handle(self, *args, **options):
        if options['csv']:
            rows = self.read_csv(Path(options['csv']))
            source = f"CSV {options['csv']}"
            value_keys = self.resolve_csv_value_keys(rows)
        else:
            rows = self.fetch_api(options['season'], options['scoring'])
            source = f"FantasyPros API ({options['season']}, {options['scoring']} scoring)"
            value_keys = ADP_KEYS

        if not rows:
            raise CommandError(f'No rows returned from {source}.')

        self.stdout.write(f'{len(rows)} rows from {source}')
        self.apply(
            rows, value_keys, dry_run=options['dry_run'], replace=options['replace']
        )

    def resolve_csv_value_keys(self, rows):
        """Decide once, from the header, which column carries the ordering."""
        if not rows:
            return CSV_ADP_KEYS

        columns = set(rows[0])

        if columns & set(CSV_ADP_KEYS):
            return CSV_ADP_KEYS

        if columns & set(CSV_RANK_KEYS):
            self.stdout.write(self.style.WARNING(
                '  This file has no ADP column, so expert consensus rank (RK) is '
                'being used as the draft ordering instead.\n'
                '  That is a fine "best available" order for the simulator, but it '
                'is not average draft position.\n'
                '  For true ADP, export from fantasypros.com/nfl/adp/'
                'half-point-ppr-overall.php\n'
            ))
            return CSV_RANK_KEYS

        raise CommandError(
            f'No ADP or rank column found. Columns were: {sorted(columns)}'
        )

    # -- sources ------------------------------------------------------------

    def fetch_api(self, season, scoring):
        key = settings.FANTASYPROS_API_KEY
        if not key:
            raise CommandError(
                'No API key found. Set FANTASYPROS_API_KEY in .env, or use '
                '--csv to import a FantasyPros export instead.'
            )

        try:
            response = requests.get(
                API_URL.format(season=season),
                headers={'x-api-key': key},
                params={'position': 'ALL', 'type': 'draft', 'scoring': scoring, 'week': 0},
                timeout=30,
            )
        except requests.RequestException as exc:
            # str(exc) can contain the request URL but never the header, so the
            # key cannot leak here.
            raise CommandError(f'Could not reach FantasyPros: {exc}')

        if response.status_code == 401:
            raise CommandError('FantasyPros rejected the API key (401).')
        if response.status_code != 200:
            raise CommandError(
                f'FantasyPros returned HTTP {response.status_code}. '
                f'Body starts: {response.text[:200]}'
            )

        payload = response.json()
        rows = payload.get('players') if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise CommandError(
                f'Unexpected response shape; top-level keys were: {list(payload)[:10]}'
            )

        if isinstance(payload, dict):
            self.warn_if_truncated(payload, len(rows))

        return rows

    def warn_if_truncated(self, payload, returned):
        """The free tier silently truncates. Say so loudly rather than importing
        a top-10 slice and letting it look like a complete refresh."""
        available = payload.get('count')
        if payload.get('public_api_limited') or (available and returned < available):
            self.stdout.write(self.style.ERROR(
                f'\n  FantasyPros returned {returned} players but reports {available} '
                f'available (tier: {payload.get("tier", "unknown")}).\n'
                f'  The public key tier caps this endpoint and ignores limit/offset, '
                f'and the values are expert consensus rank, not true ADP.\n'
                f'  Download a FantasyPros ADP export and use --csv for a full, real '
                f'dataset.\n'
            ))

    def read_csv(self, path):
        if not path.exists():
            raise CommandError(f'CSV not found: {path}')
        with path.open(newline='', encoding='utf-8-sig') as handle:
            return list(csv.DictReader(handle))

    # -- applying -----------------------------------------------------------

    @transaction.atomic
    def apply(self, rows, value_keys, dry_run=False, replace=False):
        if replace and not dry_run:
            # Inside the same atomic block as the writes, so a failure part-way
            # cannot leave the table wiped.
            cleared = Player.objects.filter(adp__isnull=False).update(adp=None)
            self.stdout.write(f'cleared {cleared} existing ADP values')

        players = list(Player.objects.all())
        index = build_index(players)
        now = timezone.now()

        matched, unmatched, skipped = [], [], 0

        for row in rows:
            raw_name = first_value(row, NAME_KEYS + CSV_NAME_KEYS)
            position = first_value(row, POSITION_KEYS + CSV_POSITION_KEYS)
            adp = to_float(first_value(row, value_keys))

            # Some exports carry a TEAM column; the ADP export instead packs the
            # team into the player cell. Prefer the explicit column when present.
            name, parsed_team = split_player_and_team(raw_name)
            team = first_value(row, TEAM_KEYS + CSV_TEAM_KEYS) or parsed_team

            if not name or adp is None:
                skipped += 1
                continue

            player = find_player(index, name, position)
            if player is None:
                # Only worth reporting for positions we actually roster.
                if normalize_position(position) in Player.Position.values:
                    unmatched.append(f'{name} ({position})')
                continue

            player.adp = adp
            player.nfl_team = (team or '').strip()[:4]
            player.adp_updated = now
            matched.append(player)

        if not dry_run:
            Player.objects.bulk_update(matched, ['adp', 'nfl_team', 'adp_updated'])

        self.report(matched, unmatched, skipped, dry_run)

    def report(self, matched, unmatched, skipped, dry_run):
        total = Player.objects.count()
        self.stdout.write(
            f'matched: {len(matched)} of {total} rostered players | '
            f'rows for players nobody rosters: {len(unmatched)} (expected -- the '
            f'file covers the whole NFL) | unusable rows: {skipped}'
        )

        # The actionable signal is the inverse of "unmatched source rows": a
        # player of OURS with no ADP is the one that suggests a name mismatch.
        # Reporting the 650-odd NFL players we don't roster would bury it.
        if dry_run:
            self.stdout.write(self.style.WARNING('\nDry run -- nothing saved.'))
            return

        stranded = list(Player.objects.filter(adp__isnull=True))
        if stranded:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                f'{len(stranded)} rostered player(s) got no ADP. Check for a name '
                f'mismatch against the source file, and fix ours in the admin:'
            ))
            for player in stranded[:30]:
                self.stdout.write(f'    {player.name} ({player.position})')
            if len(stranded) > 30:
                self.stdout.write(f'    ... and {len(stranded) - 30} more')

        self.stdout.write(self.style.SUCCESS('\nADP import complete.'))
