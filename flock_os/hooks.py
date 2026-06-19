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
# Domain fixtures are authored in flock_os.fixtures (pure data) and materialized
# idempotently by the versioned patch flock_os.patches.v0_1.seed_core_fixtures
# (see flock_os/patches.txt). The export config below mirrors the same records so
# `bench export-fixtures` keeps shipped fixtures in sync with the patch.
from flock_os.fixtures import (
	FLOCK_GATHERING_TYPE_NAMES,
	FLOCK_GROUP_TYPE_NAMES,
	FLOCK_ROLES,
)

fixtures = [
	{"doctype": "Role", "filters": [["name", "in", list(FLOCK_ROLES)]]},
	{
		"doctype": "Flock Group Type",
		"filters": [["name", "in", list(FLOCK_GROUP_TYPE_NAMES)]],
	},
	{
		"doctype": "Flock Gathering Type",
		"filters": [["name", "in", list(FLOCK_GATHERING_TYPE_NAMES)]],
	},
]

# ---------------------------------------------------------------------------- #
# Lifecycle hooks (event emission points)
# ---------------------------------------------------------------------------- #
# DocTypes delegate to the single sanctioned emitter `flock_os.events` (ADR §5,
# shipped by FLO-14). The (doctype, Frappe hook) → canonical-event map below
# connects the core DocTypes to the catalog; the dispatcher reuses the public
# `flock_os.events.on_doc_event` so payload/scope derivation stays in one place.
# Realtime fan-out + the Redis/outbox sink are wired further down by FLO-14.
from flock_os import events as flock_events

_FLOCK_DOC_EVENTS: dict[tuple[str, str], str] = {
	("Flock Branch", "after_insert"): flock_events.BRANCH_CREATED,
	("Flock Group", "after_insert"): flock_events.GROUP_CREATED,
	("Flock Group Member", "on_update"): flock_events.GROUP_MEMBER_ADDED,
	("Flock Member", "after_insert"): flock_events.MEMBER_CREATED,
	("Flock Gathering", "after_insert"): flock_events.GATHERING_CREATED,
}


def _dispatch_flock_doc_event(doc, method: str | None = None) -> None:
	"""Frappe doc-event bridge → canonical `flock_os.events` (ADR §5.1).

	Wired from ``hooks.doc_events``. Looks up the canonical event for the fired
	(doctype, hook) and forwards to the public ``flock_os.events.on_doc_event``,
	which derives payload + row-level scope (branch/group/organization).
	"""
	event_name = _FLOCK_DOC_EVENTS.get((doc.doctype, method))
	if not event_name:
		return
	flock_events.on_doc_event(doc, event_name)


doc_events = {
	"Flock Branch": {"after_insert": "flock_os.hooks._dispatch_flock_doc_event"},
	"Flock Group": {"after_insert": "flock_os.hooks._dispatch_flock_doc_event"},
	"Flock Group Member": {"on_update": "flock_os.hooks._dispatch_flock_doc_event"},
	"Flock Member": {"after_insert": "flock_os.hooks._dispatch_flock_doc_event"},
	"Flock Gathering": {"after_insert": "flock_os.hooks._dispatch_flock_doc_event"},
}
scheduled_jobs = []

# ---------------------------------------------------------------------------- #
# Row-level permission query conditions (FLO-20, ADR-0001 §6.3)
# ---------------------------------------------------------------------------- #
# The **one** custom group-axis mechanism: a single `permission_query_conditions`
# hook registered for every DocType in `flock_os.permissions.SCOPED_DOCTYPES`.
# Frappe appends the returned SQL fragment to the WHERE clause of every list/get
# query, narrowing a leader/member to their led subtree + self/joined groups.
# The branch axis rides native Frappe User Permissions (§6.2) — no hook needed
# there. Built from SCOPED_DOCTYPES so adding a group-level DocType = appending
# its name in `flock_os.permissions` (one list, not per-DocType wiring — §6.5).
from flock_os.permissions import SCOPED_DOCTYPES as _FLOCK_SCOPED_DOCTYPES

permission_query_conditions = {
	doctype: "flock_os.permissions.get_group_scoped_conditions" for doctype in _FLOCK_SCOPED_DOCTYPES
}

# ---------------------------------------------------------------------------- #
# Migrations
# ---------------------------------------------------------------------------- #
# Versioned patches live in flock_os/patches/<semver>/ and are referenced here
# only when an explicit, ordered run is required. See flock_os/patches.txt.
# ---------------------------------------------------------------------------- #

# ---------------------------------------------------------------------------- #
# Realtime-handler + auth-cache auto-wiring (FLO-109 / FLO-116)
# ---------------------------------------------------------------------------- #
# A `bench update` rewrites apps/frappe/realtime/index.js and would silently
# drop two flock_os wirings:
#   * the join handler inserted by scripts/dev/wire-socketio-handler.sh
#     (FLO-107) — joins then no-op and broadcasts reach zero clients; and
#   * the auth-cache `.wrap(authenticate)` swap inserted by
#     scripts/dev/wire-socketio-auth-cache.sh (FLO-116) — the per-connection
#     `get_user_info` HTTP returns, bringing back the §8 15k WS auth wall
#     (connect p95 > 2 s, flock_ws_receive_errors > 0).
# Re-wire both automatically on every migrate (which `bench update` performs)
# and on install, so neither line is ever missing without a manual runbook step.
# Both wire scripts are idempotent + marker-guarded; the hooks fail loud on a
# missing marker (RealtimeWiringError) rather than shipping a silent regression.
after_migrate = [
	"flock_os.utils.realtime_setup.rewire_socketio_handler",
	"flock_os.utils.realtime_setup.rewire_socketio_auth_cache",
]
after_install = [
	"flock_os.utils.realtime_setup.rewire_socketio_handler",
	"flock_os.utils.realtime_setup.rewire_socketio_auth_cache",
]


# ---------------------------------------------------------------------------- #
# Realtime fan-out — sharded event-room channels + event projector (FLO-14)
# ---------------------------------------------------------------------------- #
# Wires the single sanctioned emitter (``flock_os.events``) to the realtime
# projector (``flock_os.realtime``) per FLO-10 ADR §5. The projector subscribes
# to the domain-event catalog and fans each event out to the sharded event-room
# channels + broadcast via ``frappe.publish_realtime`` — no bespoke Redis
# clients (keeps the D3 cluster escape hatch from FLO-10 §6 viable).
#
# Realtime is best-effort (§5.3): reporter acks are cheap + synchronous
# (FLO-15); room updates are best-effort. Correctness stays in the queue.
#
# The projector registration is deferred to ``ready`` hook so it runs once the
# app + Frappe bootstrapped inside a bench; it stays a no-op under plain pytest
# (the unit suite wires the projector + a recording publisher directly).
# ---------------------------------------------------------------------------- #
def _register_realtime_projector():
	"""Subscribe the realtime projector to the event bus + install the Frappe publisher.

	Guarded so the import-time wiring never breaks CI (no bench): Frappe is only
	touched inside :class:`FrappeRealtimePublisher`, which lazy-imports it.
	"""
	try:
		from flock_os import realtime

		realtime.register_projector()
	except Exception:  # noqa: BLE001 - realtime is best-effort (FLO-10 §5.3)
		# Outside a bench (CI) Frappe is unavailable; the projector's routing
		# contract is still fully covered by the project-level unit suite.
		pass


def _install_event_sink():
	"""Install the production Redis-pub/sub + outbox sink on the event bus."""
	try:
		from flock_os.events import FrappeEventSink, install_sink

		install_sink(FrappeEventSink())
	except Exception:  # noqa: BLE001 - no Frappe outside a bench
		pass


def _install_telemetry_sources():
	"""Install the D3/D5 production telemetry sources (FLO-49 / FLO-10 §8).

	Best-effort: telemetry must never break the runtime, so a failure outside a
	bench (no Frappe) is swallowed and the static defaults stay in place.
	"""
	try:
		from flock_os.telemetry import install_frappe_sources

		install_frappe_sources()
	except Exception:  # noqa: BLE001 - no Frappe outside a bench
		pass


def _register_branch_scope_sync():
	"""Subscribe the branch-axis User-Permission re-syncer (ADR §6.2, FLO-20).

	On ``flock.branch.moved`` the moved subtree's membership in an admin's
	allowed set can change, so each affected ``Flock Branch Admin Scope`` is
	re-materialized. The native Frappe User-Permission path stays authoritative;
	this only keeps the equality rows Frappe's filter needs in sync. Best-effort
	(failure never breaks the originating request — a queued re-sync covers it).
	"""
	try:
		import frappe

		from flock_os.events import BRANCH_MOVED, subscribe

		def _on_branch_moved(event):
			moved = event.payload.get("branch")
			if not moved:
				return
			# Re-sync every active scope whose root is an ancestor of (or is) the
			# moved branch — their allowed set may have gained/lost descendants.
			scopes = frappe.get_all("Flock Branch Admin Scope", filters={"is_active": 1}, pluck="name")
			for name in scopes:
				frappe.enqueue(
					"flock_os.hooks._resync_branch_admin_scope",
					queue="default",
					scope_name=name,
				)

		subscribe(BRANCH_MOVED, _on_branch_moved)
	except Exception:  # noqa: BLE001 - no Frappe outside a bench
		pass


def _resync_branch_admin_scope(scope_name: str) -> None:
	"""Re-materialize one ``Flock Branch Admin Scope``'s User Permissions.

	Enqueued by the ``flock.branch.moved`` subscriber so the re-sync runs on an
	RQ worker (ADR §6.2 — "queued UP re-sync") and never blocks the move.
	"""
	import frappe

	doc = frappe.get_doc("Flock Branch Admin Scope", scope_name)
	doc.save(ignore_permissions=True)


def on_doc_event(doctype: str | None = None, event: str | None = None):
	"""Frappe doc-event hook bridge → the single sanctioned emitter (ADR §5.1).

	``hooks.doc_events`` maps ``(doctype, lifecycle)`` → this entry point, which
	forwards to ``flock_os.events.on_doc_event``. Keeping the indirection here
	lets the hook table stay declarative and Frappe-free at import time.
	"""
	from flock_os.events import on_doc_event as _emit_doc_event

	_emit_doc_event(doctype, event)


# ---------------------------------------------------------------------------- #
# App-load wiring (runs once when Frappe loads this app's hooks).
#
# Frappe imports ``hooks.py`` once per worker boot; this is the single, reliable
# moment to install the production event sink + subscribe the realtime
# projector. The guard makes it a safe no-op outside a bench (plain pytest):
# Frappe is only touched through the lazy adapters in ``flock_os.events`` /
# ``flock_os.realtime``, never at import time.
# ---------------------------------------------------------------------------- #
def _frappe_available() -> bool:
	try:
		import frappe  # noqa: F401

		return True
	except Exception:  # noqa: BLE001 - no bench under CI
		return False


if _frappe_available():
	_install_event_sink()
	_register_realtime_projector()
	_install_telemetry_sources()
	_register_branch_scope_sync()
