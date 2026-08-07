"""Settings and onboarding routes.

Handles /settings, /onboard/*, /switch-league routes.
These routes modify configuration and manage league setup.

NOTE: Full route implementation remains in web/app.py during migration.
This blueprint will absorb those routes once the monolithic app.py is dismantled.
"""

from __future__ import annotations

from flask import Blueprint

bp = Blueprint("settings", __name__)

# Settings routes will be migrated here from web/app.py in a future pass.
# For now, the monolithic app.py continues to serve these routes.
