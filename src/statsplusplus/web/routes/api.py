"""API routes — AJAX endpoints, CSV export, refresh trigger.

Handles /api/*, /refresh routes. These return JSON or trigger background tasks.

NOTE: Full route implementation remains in web/app.py during migration.
This blueprint will absorb those routes once the monolithic app.py is dismantled.
"""

from __future__ import annotations

from flask import Blueprint

bp = Blueprint("api", __name__)

# API routes will be migrated here from web/app.py in a future pass.
# For now, the monolithic app.py continues to serve these routes.
