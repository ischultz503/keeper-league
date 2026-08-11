# Django for a Streamlit Developer

Notes for Isaac. Read once before the scaffold, then come back as reference while reviewing what Claude Code builds.

## The core mental model shift

**Streamlit:** one Python script that reruns top to bottom every time the user touches anything. State is a hack (`st.session_state`). The script *is* the page.

**Django:** nothing reruns. The browser sends a **request** to a URL; Django looks up which **view function** owns that URL, the view fetches data and returns a **response** (usually HTML built from a **template**); then Django is done until the next request. Every page load or form submit is one clean request → response cycle.

This is why Django needs more files than Streamlit — the jobs your one script did are split into named parts:

| In Streamlit you... | In Django you... | File |
|---|---|---|
| load a CSV with pandas | define models; query with the ORM | `league/models.py` |
| write script logic inline | write a view function per page | `league/views.py` |
| — (URL is just the app) | map URLs to views | `league/urls.py` |
| use `st.write`/`st.dataframe` | write an HTML template | `league/templates/` |
| use `st.text_input` + rerun | use an HTML form + a POST view | templates + views |
| `st.session_state` | database + Django sessions | handled for you |
| — | configure the project | `keeper_site/settings.py` |

## The pieces, in the order you'll meet them

**`manage.py`** — the command-line entry point. `python manage.py runserver` (dev server, auto-reloads like Streamlit), `makemigrations`, `migrate`, `createsuperuser`, plus custom commands.

**Project vs app.** The *project* (`keeper_site/`) is configuration: settings, root URLs. An *app* (`league/`) is a feature module with its own models, views, templates. Small sites have one app; that's us.

**Models (`models.py`)** — Python classes that define your database tables. This replaces "load the CSV into a DataFrame at the top of the script." Data lives in the database once; every request queries it.

```python
class Team(models.Model):
    name = models.CharField(max_length=100)
    owner_name = models.CharField(max_length=50)
```

**Migrations** — when you change a model, `makemigrations` generates a script describing the schema change and `migrate` applies it to the database. Think of it as version control for your database schema. You never edit the DB by hand.

**The ORM** — how you query. `Team.objects.all()`, `RosterEntry.objects.filter(team=team, season__year=2025)`. It returns model instances, not rows. Where you'd reach for `df[df.Team == x]`, you write a `.filter()`. (pandas still has a home: one-off data imports.)

**Views (`views.py`)** — a function per page: takes a `request`, returns a `response`.

```python
def team_detail(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    roster = team.rosterentry_set.filter(season__year=2025)
    return render(request, "league/team_detail.html", {"team": team, "roster": roster})
```

**URLs (`urls.py`)** — the routing table: `path("teams/<int:team_id>/", views.team_detail)`. Streamlit never needed this because the whole app was one page.

**Templates** — HTML with a small placeholder language: `{{ team.name }}`, `{% for entry in roster %}...{% endfor %}`, and template inheritance (`base.html` holds the nav/layout; each page fills in a block). This replaces the implicit layout Streamlit gave you.

**The admin site** — Django's killer feature for a project like ours. Register a model in `admin.py` (two lines) and you get a full create/read/update/delete web UI for it at `/admin`, for free. Fixing a typo'd player name or entering a pick trade needs no custom page.

**Auth** — built in: users, passwords, login/logout views, `@login_required` on a view, `request.user` to know who's asking. That last part is the heart of our app — "show *this manager* *their* team" is just `request.user` → their Team. We start with Django auth; Cognito replaces the login step later without changing that idea.

**Management commands** — your own `manage.py` subcommands. Our CSV import will be `python manage.py import_rosters`, and it's where pandas is allowed.

## The request lifecycle (tape this to your monitor)

```
browser: GET /teams/3/
  → keeper_site/urls.py → league/urls.py: matches "teams/<int:team_id>/"
  → views.team_detail(request, team_id=3)
  → ORM queries SQLite
  → render() fills team_detail.html
  → HTML response → browser
```

Forms are the same with POST: the view checks `request.method == "POST"`, validates, saves via the ORM, redirects.

## `USE_TZ` vs `TIME_ZONE` (they are not two settings for one thing)

These get confused constantly. They do different jobs, and the draft poll is the
first feature where getting it wrong would have been visible to everyone.

**`USE_TZ = True` is about storage.** With it on, Django converts every datetime
to **UTC** before it reaches the database, and every datetime it reads back is a
timezone-*aware* object in UTC. This is the setting you leave alone. UTC has no
daylight-saving jump, so "is A before B?" and "how long between them?" are
always answerable. Turn it off and you store naive wall-clock times, and one
Sunday in November two different instants spell the same thing.

**`TIME_ZONE = 'America/Los_Angeles'` is about presentation.** It is the wall
clock Django renders datetimes on, and the clock it assumes when you type a
naive time into the admin. It changes nothing about what is in the database.

So the round trip for one candidate draft time is:

```
admin form:   8/23/2026 7:00 PM     ← you type the Pacific wall clock
              ↓ Django reads it as TIME_ZONE
database:     2026-08-24 02:00 UTC  ← USE_TZ decided this
              ↓ Django renders it back in TIME_ZONE
page:         Sun Aug 23, 7:00 PM PT
```

It was `TIME_ZONE = 'UTC'`, which broke the middle step in both directions: 7 PM
typed in the admin was *stored* as 7 PM UTC — actually noon Pacific — and then
rendered back as "7 PM" to everyone, so nothing on screen ever revealed the
error. A poll is exactly the wrong place for that class of bug.

Two rules that follow from this:

- Never print a bare datetime on a page people act on. `league.poll.format_slot`
  puts the zone in the string ("PT"), derived from the zone rather than typed,
  so it cannot drift from the setting.
- In tests, build the datetime in Pacific (`timezone.make_aware(naive,
  ZoneInfo(settings.TIME_ZONE))`) when you mean a wall clock someone typed.
  Writing the UTC value by hand in a test re-introduces the same confusion the
  setting exists to remove.

Out of scope on purpose: per-user timezones. Anyone on Eastern reads the "PT"
and does the arithmetic.

## What to watch for while Claude Code scaffolds

1. `startproject` / `startapp` generate boilerplate — most files start nearly empty; don't be alarmed by the file count.
2. After models are written, watch the `makemigrations` → `migrate` two-step and peek at the generated migration file once.
3. Open `/admin` early and click around your data — it makes models feel real.
4. Trace ONE page end to end (URL → view → template) yourself. Once you can follow `team_detail`, you understand 80% of Django.
5. `settings.py` — you only care about `INSTALLED_APPS`, `DATABASES`, and (later) static files. Ignore the rest for now.
