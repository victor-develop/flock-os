"""
Project-level tests for the app-layer rate-limit primitive — FLO-319.

Runs under plain ``pytest`` (no Frappe site / bench / Redis). The pure
:mod:`flock_os.rate_limit` module — the sliding-window backend, the key builder,
the ``enforce`` decision, and the ``ThrottledError`` — carries every branch of
logic; the bench-side :mod:`flock_os.rate_limit_frappe` adapter is a thin
Redis-backed wrapper (omitted from the coverage ratchet, exercised via the
contract tests in ``test_rate_limit_contract.py``).

Acceptance coverage (FLO-319):
* (a) under-limit calls pass,
* (b) over-limit calls are rejected,
* (c) the limit is independent of the authorization/scope gate — proven both
  structurally (``enforce`` takes no auth parameter) and behaviorally against
  the real realtime scope decision (:func:`event_room_join_allowed`).
"""

from __future__ import annotations

import pytest

from flock_os import permissions
from flock_os.rate_limit import (
	DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC,
	THROTTLE_REASON,
	InMemoryThrottleBackend,
	ThrottleBackend,
	ThrottledError,
	build_throttle_key,
	enforce,
)
from flock_os.realtime import (
	_MappingEventBranchResolver,
	event_room_join_allowed,
)


# --------------------------------------------------------------------------- #
# InMemoryThrottleBackend — the sliding-window semantics (criteria a + b)
# --------------------------------------------------------------------------- #
class TestInMemoryThrottleBackend:
	def test_under_limit_calls_are_allowed(self):
		backend = InMemoryThrottleBackend()
		# Up to the cap inside one 1s window all pass (criterion a).
		for _ in range(5):
			assert backend.throttle_allows("k", now=100.0, max_per_second=5) is True

	def test_over_limit_call_is_rejected(self):
		backend = InMemoryThrottleBackend()
		for _ in range(3):
			backend.throttle_allows("k", now=100.0, max_per_second=3)
		# The 4th call in the same 1s window is rejected (criterion b).
		assert backend.throttle_allows("k", now=100.4, max_per_second=3) is False

	def test_over_limit_attempt_is_not_recorded(self):
		# A rejected attempt must not extend the window — otherwise a sustained
		# flood would never recover (it would keep pushing the cutoff forward).
		backend = InMemoryThrottleBackend()
		for _ in range(2):
			backend.throttle_allows("k", now=100.0, max_per_second=2)
		# Spam 100 over-limit attempts at a later instant inside the window.
		for _ in range(100):
			backend.throttle_allows("k", now=100.5, max_per_second=2)
		# The window recovers 1s after the last *recorded* attempt (100.0), i.e.
		# just past 101.0. At 101.2 the bucket is pruned (100.0 < 100.2 cutoff) →
		# allowed. A buggy impl that recorded the flood would need >101.5 (1s
		# after the 100.5 flood) and would still be throttled here.
		assert backend.throttle_allows("k", now=101.2, max_per_second=2) is True

	def test_window_slides_after_one_second(self):
		backend = InMemoryThrottleBackend()
		for _ in range(3):
			backend.throttle_allows("k", now=100.0, max_per_second=3)
		assert backend.throttle_allows("k", now=100.5, max_per_second=3) is False
		# >1s later the old timestamps are pruned → allowed again.
		assert backend.throttle_allows("k", now=101.01, max_per_second=3) is True

	def test_keys_are_independent(self):
		backend = InMemoryThrottleBackend()
		for _ in range(3):
			backend.throttle_allows("device-a", now=100.0, max_per_second=3)
		# device-a is saturated; device-b is untouched.
		assert backend.throttle_allows("device-a", now=100.0, max_per_second=3) is False
		assert backend.throttle_allows("device-b", now=100.0, max_per_second=3) is True

	def test_max_per_second_is_respected_as_cap(self):
		backend = InMemoryThrottleBackend()
		results = [backend.throttle_allows("k", now=100.0, max_per_second=4) for _ in range(10)]
		assert results.count(True) == 4
		assert results.count(False) == 6


# --------------------------------------------------------------------------- #
# build_throttle_key — identity resolution + surface namespacing
# --------------------------------------------------------------------------- #
class TestBuildThrottleKey:
	def test_device_wins_over_ip(self):
		assert build_throttle_key("join_session", device="fp-1", ip="10.0.0.1") == "join_session:fp-1"

	def test_ip_fallback_when_no_device(self):
		assert build_throttle_key("realtime_join", ip="10.0.0.2") == "realtime_join:10.0.0.2"

	def test_anonymous_when_no_identity(self):
		assert build_throttle_key("register_for_event") == "register_for_event:anonymous"

	def test_empty_strings_fall_through(self):
		# Falsy device/ip (empty fingerprint on a member-only join) degrade.
		assert build_throttle_key("join_session", device="", ip="") == "join_session:anonymous"

	def test_surfaces_are_namespaced(self):
		# Three doors never share a bucket even for the same identity.
		join = build_throttle_key("join_session", device="d")
		register = build_throttle_key("register_for_event", device="d")
		realtime = build_throttle_key("realtime_join", device="d")
		assert len({join, register, realtime}) == 3
		assert all(k.split(":", 1)[0] != k for k in (join, register, realtime))


# --------------------------------------------------------------------------- #
# enforce — the decision + ThrottledError contract
# --------------------------------------------------------------------------- #
class TestEnforce:
	def test_under_limit_does_not_raise(self):
		backend = InMemoryThrottleBackend()
		for _ in range(DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC):
			# No exception means allowed (criterion a).
			enforce(backend, "k", surface="join_session", now=100.0, max_per_second=10)

	def test_over_limit_raises_throttled_error(self):
		backend = InMemoryThrottleBackend()
		for _ in range(2):
			enforce(backend, "k", surface="join_session", now=100.0, max_per_second=2)
		with pytest.raises(ThrottledError) as exc_info:
			enforce(backend, "k", surface="join_session", now=100.0, max_per_second=2)
		# The error carries the decision context for the 429 message.
		err = exc_info.value
		assert err.surface == "join_session"
		assert err.key == "k"
		assert err.max_per_second == 2
		assert err.reason == THROTTLE_REASON
		assert THROTTLE_REASON in str(err)

	def test_independent_keys_do_not_interfere(self):
		backend = InMemoryThrottleBackend()
		for _ in range(2):
			enforce(backend, "device-a", surface="join_session", now=100.0, max_per_second=2)
		# device-a saturated; device-b is unaffected.
		with pytest.raises(ThrottledError):
			enforce(backend, "device-a", surface="join_session", now=100.0, max_per_second=2)
		enforce(backend, "device-b", surface="join_session", now=100.0, max_per_second=2)


# --------------------------------------------------------------------------- #
# Protocol reuse — the primitive is the same shape the engagement runtime uses
# (FLO-319: "reuse throttle_allows"), not a reinvention.
# --------------------------------------------------------------------------- #
class TestThrottleBackendProtocolReuse:
	def test_in_memory_backend_satisfies_protocol(self):
		assert isinstance(InMemoryThrottleBackend(), ThrottleBackend)

	def test_engagement_gateway_satisfies_protocol(self):
		# The engagement runtime's throttle (FLO-9 §6.6) is structurally the same
		# primitive — ``throttle_allows(key, *, now, max_per_second) -> bool`` —
		# so FLO-319 reuses it rather than introducing a second throttle shape.
		from flock_os.engagement import InMemoryEngagementGateway

		assert isinstance(InMemoryEngagementGateway(), ThrottleBackend)


# --------------------------------------------------------------------------- #
# Criterion (c): the limit is independent of the authorization/scope gate.
# Proven both structurally (enforce has no auth parameter) and behaviorally
# against the real realtime branch-scope decision (event_room_join_allowed):
# an authorized user over the rate is still throttled; a denied user under the
# rate is still allowed by the throttle (the throttle never grants access).
# --------------------------------------------------------------------------- #
class _StubPermissionGateway:
	"""Minimal permission gateway: fixed roles + materialized branch set."""

	def __init__(self, *, roles=(), allowed=()) -> None:
		self._roles = frozenset(roles)
		self._allowed = tuple(allowed)

	def get_user_roles(self, user: str) -> frozenset[str]:  # noqa: ARG002
		return self._roles

	def list_branch_user_permissions(self, user: str) -> tuple[str, ...]:  # noqa: ARG002
		return self._allowed


class TestIndependenceFromScopeGate:
	gathering_branches = {"g-a": "branch-a", "g-b": "branch-b"}

	def test_authorized_user_is_still_throttled_over_rate(self):
		# Scope ALLOWS (org admin over their branch) yet the over-rate connect
		# is rejected by the throttle — rate-limit is an independent gate.
		gw = _StubPermissionGateway(roles=[permissions.ROLE_ORG_ADMIN], allowed=[])
		allowed = event_room_join_allowed(
			room="flock_os:event:g-a:broadcast",
			user="org@flock.os",
			gateway=gw,
			resolver=_MappingEventBranchResolver(self.gathering_branches),
		)
		assert allowed is True  # authorization layer passes

		backend = InMemoryThrottleBackend()
		key = build_throttle_key("realtime_join", device="org@flock.os")
		for _ in range(2):
			enforce(backend, key, surface="realtime_join", now=100.0, max_per_second=2)
		# Same authorized user, 3rd connect in the window → throttled regardless.
		with pytest.raises(ThrottledError):
			enforce(backend, key, surface="realtime_join", now=100.0, max_per_second=2)

	def test_denied_user_under_rate_is_still_allowed_by_throttle(self):
		# Scope DENIES (no branch scope) yet the under-rate throttle still allows
		# — the throttle does not grant authorization; the two layers are orthogonal.
		gw = _StubPermissionGateway(roles=[], allowed=[])
		allowed = event_room_join_allowed(
			room="flock_os:event:g-a:broadcast",
			user="outsider@flock.os",
			gateway=gw,
			resolver=_MappingEventBranchResolver(self.gathering_branches),
		)
		assert allowed is False  # authorization layer denies

		backend = InMemoryThrottleBackend()
		key = build_throttle_key("realtime_join", device="outsider@flock.os")
		# The throttle permits the under-limit attempt even though scope denies.
		enforce(backend, key, surface="realtime_join", now=100.0, max_per_second=2)
		enforce(backend, key, surface="realtime_join", now=100.0, max_per_second=2)

	def test_throttle_decision_ignores_scope_outcome_entirely(self):
		# Structural proof: enforce() takes no authorization parameter, so its
		# verdict is a pure function of (backend, key, now, max_per_second). The
		# same key + rate yields the same sequence under either scope verdict.
		for _scope_outcome in (True, False):
			backend = InMemoryThrottleBackend()
			verdicts = []
			for _ in range(3):
				try:
					enforce(backend, "k", surface="join_session", now=100.0, max_per_second=2)
					verdicts.append("allowed")
				except ThrottledError:
					verdicts.append("throttled")
			# Identical regardless of the (irrelevant) scope outcome.
			assert verdicts == ["allowed", "allowed", "throttled"]


# --------------------------------------------------------------------------- #
# FLO-815: per-socket throttle key — the realtime_join surface keys by the
# Socket.IO server-assigned connection id, so 15k distinct sockets sharing one
# session user (the §8 load-test bar) are NOT collapsed into a single 10/s
# bucket. Before the fix, all 15k VUs keyed on ``frappe.session.user`` (the
# shared leader identity) and 14,990/s were throttled — starving the gate.
# --------------------------------------------------------------------------- #
class TestPerSocketThrottleKeyFLO815:
	GATE_VUS = 15_000

	def test_15k_distinct_socket_ids_each_get_their_own_bucket(self):
		# The §8 scenario: every VU presents the same leader session but a
		# distinct socket_id (server-assigned). Before FLO-815 the key was the
		# session user → one bucket → 14,990/s throttled. With per-socket keying
		# each socket does ≤2 joins (shard + broadcast) — all allowed.
		backend = InMemoryThrottleBackend()
		throttled = 0
		for i in range(self.GATE_VUS):
			key = build_throttle_key("realtime_join", device=f"socket#{i}")
			for _ in range(2):  # shard + broadcast
				if not backend.throttle_allows(
					key, now=100.0, max_per_second=DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC
				):
					throttled += 1
		assert throttled == 0, f"per-socket key must not throttle legit joins (got {throttled})"

	def test_collapsed_session_user_key_still_throttles_starvation_scenario(self):
		# Regression guard: the OLD behavior (keying on the shared session user)
		# IS the bug. Prove it would have throttled, so the per-socket fix is
		# load-bearing — not just a no-op change.
		backend = InMemoryThrottleBackend()
		key = build_throttle_key("realtime_join", device="leader@flock.os")
		results = [
			backend.throttle_allows(key, now=100.0, max_per_second=DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC)
			for _ in range(self.GATE_VUS)
		]
		assert results.count(True) == DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC
		assert results.count(False) == self.GATE_VUS - DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC

	def test_reconnect_storm_on_one_socket_is_still_bounded(self):
		# The production security guarantee is preserved: a single socket
		# reconnect-storming room joins (>> 10/s) is still throttled. The
		# per-socket key doesn't weaken the per-connection bound.
		backend = InMemoryThrottleBackend()
		key = build_throttle_key("realtime_join", device="socket#storm")
		allowed = sum(
			backend.throttle_allows(key, now=100.0, max_per_second=DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC)
			for _ in range(100)
		)
		assert allowed == DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC
