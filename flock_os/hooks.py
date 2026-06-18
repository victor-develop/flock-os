"""
Hooks for Flock OS (flock_os Frappe custom app).

In the Frappe event model, state changes emit domain events via hooks +
Redis pub/sub; downstream features subscribe rather than re-querying. This file
is the integration surface between the flock_os app and the Frappe framework.

Event catalog, DocType wiring, fixtures, and versioned patches land here as the
data model (FLO-3+) and live features (Phases 2-5) are built out.
"""

app_name = "flock_os"
app_title = "Flock OS"
app_description = "Multi-branch organization / mega-church management SaaS on Frappe."
app_publisher = "Flock OS"
app_email = "dev@flock.os"
app_license = "MIT"
app_url = "https://github.com/victor-develop/flock-os"

# App version is read from flock_os/__init__.py
app_version = __import__("flock_os").__version__

# ---------------------------------------------------------------------------- #
# DocTypes
# ---------------------------------------------------------------------------- #
# Default module for all Flock OS domain DocTypes.
default_app_module = "flock_os"

# ---------------------------------------------------------------------------- #
# Fixtures & data seeding
# ---------------------------------------------------------------------------- #
# Domain fixtures (roles, org-tree node types, etc.) are added here as the
# data model lands. Keeping them versioned makes every site reproducible.
fixtures = []

# ---------------------------------------------------------------------------- #
# Lifecycle hooks (event emission points)
# ---------------------------------------------------------------------------- #
# DocType events, scheduled jobs, and Redis pub/sub wiring are registered here
# as features are built. Convention: services emit events; UI/REST subscribe.
doc_events = {}
scheduled_jobs = []

# ---------------------------------------------------------------------------- #
# Migrations
# ---------------------------------------------------------------------------- #
# Versioned patches live in flock_os/patches/<semver>/ and are referenced here
# only when an explicit, ordered run is required. See flock_os/patches.txt.
# ---------------------------------------------------------------------------- #

# ---------------------------------------------------------------------------- #
# Realtime / notifications
# ---------------------------------------------------------------------------- #
# Redis pub/sub channels and Frappe realtime events for live features (games,
# questionnaires, scoped push notifications) are declared here in later phases.
# ---------------------------------------------------------------------------- #
