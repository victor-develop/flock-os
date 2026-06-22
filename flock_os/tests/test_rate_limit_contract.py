"""
Contract tests for the public-endpoint rate-limit wiring — FLO-319.

The three public door surfaces live in bench-only modules (they
``@frappe.whitelist()`` + raise Frappe exceptions), so they cannot be exercised
under the no-bench ``pytest`` gate. These tests statically verify the *contract*
by parsing each surface's source — the same approach
:mod:`flock_os.tests.test_registrations` uses to pin ``register_for_event``'s
ordering invariants without a site.

Pinned (FLO-319 acceptance):
* each of the 3 endpoints invokes :func:`enforce_public` — i.e. the throttle is
  actually wired in, not just importable;
* the throttle runs **before** the surface's authorization/scope gate (the
  rate-limit is a separate concern layered on top — FLO-319 §What);
* the production Redis throttle is the single canonical implementation (no
  bespoke Redis client — ADR-0001 §5), and the public-door namespace is
  independent of the engagement participate namespace.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_FLOCK_OS = pathlib.Path(__file__).resolve().parent.parent
_API = _FLOCK_OS / "engagement_api.py"
_REALTIME_VIEWS = _FLOCK_OS / "realtime_views.py"
_CONTROLLER = (
	_FLOCK_OS / "flock_os" / "doctype" / "flock_event_registration" / "flock_event_registration.py"
)
_RATE_LIMIT_FRAPPE = _FLOCK_OS / "rate_limit_frappe.py"
_ENGAGEMENT_FRAPPE = _FLOCK_OS / "engagement_frappe.py"


def _function_body(src: str, fn: str) -> str:
	"""Slice the source from ``def fn`` to the next top-level ``def ``/``class ``."""
	start = src.index(f"def {fn}")
	rest = src[start:]
	# The next top-level def/class marker after the first line.
	match = re.search(r"\n(?:def |class )", rest[1:])
	end = (1 + match.start() + 1) if match else len(rest)
	return rest[:end]


# --------------------------------------------------------------------------- #
# join_session — the signed-ticket issue path (FLO-9 §6.4)
# --------------------------------------------------------------------------- #
def test_join_session_enforces_throttle_before_ticket_issue():
	src = _API.read_text()
	body = _function_body(src, "join_session")
	assert "enforce_public(" in body, "join_session must apply the public throttle"
	throttle_idx = body.index("enforce_public(")
	ticket_idx = body.index("get_service().join(")
	# Throttle runs before the ticket is issued (FLO-319: "on top of" the path).
	assert throttle_idx < ticket_idx
	# The throttle key uses the join_session surface namespace.
	assert '"join_session"' in body


# --------------------------------------------------------------------------- #
# register_for_event — the scoped-registration door (FLO-7 §5)
# --------------------------------------------------------------------------- #
def test_register_for_event_enforces_throttle_before_window_gate():
	src = _CONTROLLER.read_text()
	body = _function_body(src, "register_for_event")
	assert "enforce_public(" in body, "register_for_event must apply the public throttle"
	throttle_idx = body.index("enforce_public(")
	# The throttle runs before the eligibility/window/capacity gates (FLO-319).
	window_idx = body.index("is_registration_window_open(")
	scope_idx = body.index("is_member_in_scope(")
	assert throttle_idx < window_idx
	assert throttle_idx < scope_idx
	assert '"register_for_event"' in body


# --------------------------------------------------------------------------- #
# can_join_event_room — the realtime room-join scope gate (FLO-106)
# --------------------------------------------------------------------------- #
def test_realtime_can_join_enforces_throttle_before_scope_gate():
	src = _REALTIME_VIEWS.read_text()
	body = _function_body(src, "can_join_event_room")
	assert "enforce_public(" in body, "can_join_event_room must apply the public throttle"
	throttle_idx = body.index("enforce_public(")
	# Rate-limit is a *separate* concern from the scope gate (FLO-319 §What) and
	# runs before the pure scope decision is delegated to realtime.can_join_event_room.
	scope_idx = body.index("realtime.can_join_event_room(")
	assert throttle_idx < scope_idx
	assert '"realtime_join"' in body


# --------------------------------------------------------------------------- #
# FLO-815: per-socket throttle key — the realtime_join surface keys by the
# Socket.IO server-assigned connection id (forwarded by the node handler) so
# 15k distinct sockets sharing one session user are NOT collapsed into a single
# 10/s bucket. The production per-identity bar is preserved for direct HTTP
# callers (no socket_id → falls back to frappe.session.user).
# --------------------------------------------------------------------------- #
def test_realtime_can_join_accepts_socket_id_parameter():
	src = _REALTIME_VIEWS.read_text()
	body = _function_body(src, "can_join_event_room")
	assert "socket_id" in body, "can_join_event_room must accept socket_id (FLO-815)"


def test_realtime_can_join_keys_throttle_per_socket_when_socket_id_present():
	src = _REALTIME_VIEWS.read_text()
	body = _function_body(src, "can_join_event_room")
	# socket_id takes precedence as the throttle device when the node handler
	# forwards it (the trusted server-assigned connection id).
	assert "socket_id if socket_id" in body, "throttle must prefer socket_id (FLO-815)"


def test_realtime_can_join_falls_back_to_session_user_for_direct_http():
	src = _REALTIME_VIEWS.read_text()
	body = _function_body(src, "can_join_event_room")
	# When socket_id is absent (direct HTTP caller, not the node handler), the
	# FLO-319 per-identity bar holds — fall back to frappe.session.user.
	assert "frappe.session.user" in body, "direct-HTTP path must fall back to per-user throttle (FLO-319 bar)"


# --------------------------------------------------------------------------- #
# DRY / single-implementation contract (FLO-319: "reuse throttle_allows";
# ADR-0001 §5: no bespoke Redis client).
# --------------------------------------------------------------------------- #
def test_rate_limit_frappe_routes_through_frappe_cache_not_bespoke_redis():
	src = _RATE_LIMIT_FRAPPE.read_text()
	# No direct redis client — the sanctioned frappe.cache() abstraction only.
	assert "import redis" not in src
	assert "frappe.cache()" in src


def test_frappe_throttle_backend_reuses_canonical_redis_helper():
	src = _RATE_LIMIT_FRAPPE.read_text()
	# FrappeThrottleBackend delegates to the single redis_sliding_window_allows
	# implementation rather than inlining its own Redis sliding-window.
	assert "redis_sliding_window_allows(" in src
	assert "public:throttle:" in src


def test_engagement_gateway_throttle_reuses_canonical_redis_helper():
	src = _ENGAGEMENT_FRAPPE.read_text()
	# The engagement §6.6 throttle and the public-door throttle share ONE Redis
	# implementation (no copied logic); only the namespace differs.
	assert "redis_sliding_window_allows(" in src
	# The actual bucket-key f-string (not the docstring cross-reference).
	assert 'f"engagement:throttle:{key}"' in src
	# The public-door bucket must NOT be constructed in the engagement gateway
	# (the two counters stay independent).
	assert 'f"public:throttle:{key}"' not in src


def test_public_and_engagement_namespaces_are_independent():
	# The two surfaces never share a Redis bucket, so a flooding device on the
	# public door cannot starve the in-flight participate throttle (or vice versa).
	rlf = _RATE_LIMIT_FRAPPE.read_text()
	ef = _ENGAGEMENT_FRAPPE.read_text()
	assert 'f"public:throttle:{key}"' in rlf
	assert 'f"public:throttle:{key}"' not in ef
	assert 'f"engagement:throttle:{key}"' in ef
	assert 'f"engagement:throttle:{key}"' not in rlf


def test_enforce_public_raises_too_many_requests():
	src = _RATE_LIMIT_FRAPPE.read_text()
	# The 429-style rejection surfaces via frappe.TooManyRequestsError.
	assert "TooManyRequestsError" in src
	assert "throttled" in src.lower() or "THROTTLE_REASON" in src


@pytest.mark.parametrize(
	"endpoint,marker",
	[
		("join_session", "enforce_public"),
		("can_join_event_room", "enforce_public"),
	],
)
def test_each_surface_imports_enforce_public(endpoint, marker):
	# Sanity: the import is present in each bench surface that uses it.
	src = _API.read_text() if endpoint == "join_session" else _REALTIME_VIEWS.read_text()
	assert "enforce_public" in src
