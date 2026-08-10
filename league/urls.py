"""URL patterns for the league app.

Every path() gets a name. Templates and views then refer to routes by name
({% url 'team_detail' team.pk %}, redirect('team_detail', ...)) instead of
hard-coding "/teams/3/" -- the standard Django way, so URLs can be reshaped
without touching every reference.
"""

from django.urls import path

from . import views

urlpatterns = [
    path('', views.league_overview, name='league_overview'),
    path('teams/<int:pk>/', views.team_detail, name='team_detail'),
    path('my-team/', views.my_team, name='my_team'),
    path('my-keepers/', views.my_keepers, name='my_keepers'),
    path('board/', views.board, name='board'),
    path('rules/', views.rules, name='rules'),
    # JSON endpoint for the board's keeper sandbox. Namespaced under /api/ to
    # keep it obviously separate from the HTML pages.
    path('api/keeper-preview/', views.keeper_preview, name='keeper_preview'),
    # The draft simulation. POST only, and the sandbox selection travels in the
    # body -- a query string would leak the manager's keeper plan into browser
    # history and the server's access log. See views.simulate.
    path('board/simulate/', views.simulate, name='simulate'),
    # Predictions are a plain form POST + redirect, not JSON: the engine has to
    # re-place the whole team after every change.
    path('predictions/toggle/', views.toggle_prediction, name='toggle_prediction'),
    # Clears every lock this user has placed. POST for the same reason as any
    # other state change -- a link would let a page elsewhere wipe the board by
    # embedding an image.
    path('predictions/reset/', views.reset_board, name='reset_board'),
]
