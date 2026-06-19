"""``@frappe.whitelist()`` HTTP surface for the realtime room-join scope gate (FLO-112).

Exposes :func:`flock_os.realtime.can_join_event_room` at
``POST /api/method/flock_os.realtime_views.can_join_event_room`` so the bench
Socket.IO ``join`` handler (``realtime/handlers/flock_room_handlers.js``) can
authorize a room join over the socket's session — the same shape as Frappe's own
``can_subscribe_doc`` (FLO-10 ADR §8 WS gate, FLO-106 branch scope).

:mod:`flock_os.realtime` is **import-clean by design** (no top-level
``import frappe``; the no-bench CI gate imports it under plain pytest), so the
``@frappe.whitelist()`` decorator cannot live there — ``ruff`` would flag an
undefined ``frappe``. This thin bench-only module holds the decorator and
delegates the pure scope decision to :mod:`flock_os.realtime`, the same
hexagonal split as :mod:`flock_os.telemetry_scrape` (FLO-53): the decorator +
HTTP transport stay on the bench side, the decision logic stays pure + tested.
"""

from __future__ import annotations

import frappe

from flock_os import realtime


@frappe.whitelist()
def can_join_event_room(room: str) -> bool:
	"""Whether the socket's user may join ``room`` (FLO-106 / ADR §6.2).

	``frappe.session.user`` is the authenticated subscriber — the Socket.IO handler
	forwards the socket's session cookie / authorization header, so the request
	carries the same identity Frappe's request cycle would. Returns
	``True``/``False`` as JSON (``{"message": true}``); a ``False`` keeps the
	socket out of the room. The decision is the single sanctioned branch-scope
	gate in :func:`flock_os.realtime.event_room_join_allowed` — never an ad-hoc
	check (ADR §6.5).
	"""
	return realtime.can_join_event_room(room=room)
