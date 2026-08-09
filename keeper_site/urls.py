"""Root URLconf. Django starts here and delegates to app URLconfs via include()."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    # Django's built-in auth views: /accounts/login/, /accounts/logout/,
    # password change/reset. Using these rather than hand-rolled auth is what
    # keeps the eventual Cognito swap to one place.
    path('accounts/', include('django.contrib.auth.urls')),
    path('', include('league.urls')),
]
