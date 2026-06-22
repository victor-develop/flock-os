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
from flock_os.rate_limit_frappe import enforce_public


@frappe.whitelist()
def can_join_event_room(room: str, socket_id: str | None = None) -> bool:
	"""Whether the socket's user may join ``room`` (FLO-106 / ADR §6.2).

	``frappe.session.user`` is the authenticated subscriber — the Socket.IO handler
	forwards the socket's session cookie / authorization header, so the request
	carries the same identity Frappe's request cycle would. Returns
	``True``/``False`` as JSON (``{"message": true}``); a ``False`` keeps the
	socket out of the room. The decision is the single sanctioned branch-scope
	gate in :func:`flock_os.realtime.event_room_join_allowed` — never an ad-hoc
	check (ADR §6.5).

	A per-socket connect-rate throttle (FLO-319 / FLO-815) runs *before* the scope
	decision: it is a separate concern (rate-limit ≠ authorization), bounding a
	single socket's join/connect rate so a reconnect storm cannot slam the gate.
	The throttle is keyed by ``socket_id`` — the Socket.IO server-assigned
	connection id forwarded by the bench node handler (``flock_room_handlers.js``)
	— so 15k distinct sockets are NOT collapsed into one bucket even when they
	share a session user (the §8 load-test scenario where every VU presents the
	leader's ``sid``). When ``socket_id`` is absent (direct HTTP caller, not the
	node handler), the throttle falls back to ``frappe.session.user`` — the
	original FLO-319 per-identity bar — so the production security guarantee is
	intact for every access path. On over-limit it raises
	``frappe.TooManyRequestsError`` (429); the node handler treats any non-200 as
	"stay out of the room" (best-effort back-off), and the pure scope decision
	below is untouched.
	"""
	# Per-socket throttle (FLO-319 / FLO-815): ``socket_id`` is the Socket.IO
	# server-assigned connection id forwarded by the trusted bench node handler.
	# Keying on it means each of 15k concurrent sockets gets its own 10/s bucket
	# rather than all sharing the single session-user bucket (which starved the
	# §8 gate when every VU presented the leader's ``sid``). The id is server-
	# generated (not client-controlled), so the per-connection reconnection-storm
	# bound is preserved. When absent (direct HTTP), fall back to the session
	# user so the FLO-319 per-identity bar holds for every access path.
	throttle_device = socket_id if socket_id else frappe.session.user
	enforce_public("realtime_join", device=throttle_device)
	return realtime.can_join_event_room(room=room)
