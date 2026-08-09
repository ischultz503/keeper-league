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
]
