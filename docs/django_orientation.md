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

## What to watch for while Claude Code scaffolds

1. `startproject` / `startapp` generate boilerplate — most files start nearly empty; don't be alarmed by the file count.
2. After models are written, watch the `makemigrations` → `migrate` two-step and peek at the generated migration file once.
3. Open `/admin` early and click around your data — it makes models feel real.
4. Trace ONE page end to end (URL → view → template) yourself. Once you can follow `team_detail`, you understand 80% of Django.
5. `settings.py` — you only care about `INSTALLED_APPS`, `DATABASES`, and (later) static files. Ignore the rest for now.
