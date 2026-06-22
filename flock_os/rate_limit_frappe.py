"""
Frappe adapter for the app-layer rate-limit primitive (FLO-319 / FLO-294 SEC-RL).

Bench-only counterpart to the pure :mod:`flock_os.rate_limit` module: the
Redis-backed :class:`ThrottleBackend` production adapter + the reusable
:func:`enforce_public` choke point the three public door surfaces call. This
module cannot run without a Frappe site (it touches ``frappe.cache()`` / Redis
+ raises Frappe exceptions), so it lives outside the import-clean pure module
and is omitted from the project-level coverage ratchet alongside the other
Frappe-only surfaces — the same hexagonal split as
:mod:`flock_os.engagement_frappe`.

The Redis sliding-window is the **single canonical implementation** (FLO-319
§What: "reuse throttle_allows"; ADR-0001 §5: no bespoke Redis client) — the
engagement runtime's :meth:`FrappeEngagementGateway.throttle_allows` (FLO-9 §6.6)
delegates to the same :func:`redis_sliding_window_allows` helper so the two
surfaces never drift. The only difference is the bucket-key namespace
(``engagement:throttle:`` vs ``public:throttle:``) so the in-flight participate
throttle and the public door throttle never share a counter.

All Redis access goes through ``frappe.cache()`` — the sanctioned abstraction
(keeps the D3 escape hatch viable). Best-effort: a Redis failure degrades to
allow, mirroring the engagement §6.6 throttle (the limit protects the door /
log, not correctness).
"""

from __future__ import annotations

import time
from typing import Any

from flock_os.rate_limit import (
	DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC,
	THROTTLE_REASON,
	build_throttle_key,
)


def redis_sliding_window_allows(
	cache: Any,
	bucket_key: str,
	*,
	now: float,
	max_per_second: int,
) -> bool:
	"""Canonical Redis 1s sliding-window throttle (FLO-9 §6.6 / FLO-319).

	The single implementation both the engagement runtime throttle
	(:meth:`FrappeEngagementGateway.throttle_allows`) and the public-door
	throttle (:meth:`FrappeThrottleBackend.throttle_allows`) delegate to, so the
	two surfaces never drift (DRY). ``bucket_key`` carries the caller's namespace
	(``engagement:throttle:…`` vs ``public:throttle:…``) so the counters stay
	independent. Best-effort: any Redis failure returns ``True`` (the throttle
	protects the door / log, not correctness — §6.6).
	"""
	cutoff = now - 1.0
	cache.zremrangebyscore(bucket_key, 0, cutoff)
	count = cache.zcard(bucket_key)
	if count and int(count) >= max_per_second:
		return False
	cache.zadd(bucket_key, {str(now): now})
	cache.expire(bucket_key, 2)
	return True


class FrappeThrottleBackend:
	"""Production :class:`flock_os.rate_limit.ThrottleBackend` adapter (FLO-319).

	Redis sliding-window via ``frappe.cache()``, namespaced under
	``public:throttle:`` so the public door counters stay independent of the
	engagement participate throttle. Lazily imports Frappe so this module stays
	importable in a no-bench environment (the adapter is only exercised inside a
	Frappe site).
	"""

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	def throttle_allows(self, key: str, *, now: float, max_per_second: int) -> bool:
		"""Per-``key`` 1s sliding-window check (best-effort: Redis failure → allow)."""
		frappe = self._frappe
		try:
			return redis_sliding_window_allows(
				frappe.cache(),
				f"public:throttle:{key}",
				now=now,
				max_per_second=max_per_second,
			)
		except Exception:  # noqa: BLE001 — Redis is best-effort (§6.6)
			return True


# ---------------------------------------------------------------------------- #
# Process-wide accessor (mirrors the other gateway singletons).
# ---------------------------------------------------------------------------- #
_backend: FrappeThrottleBackend | None = None


def get_backend() -> FrappeThrottleBackend:
	"""The process-wide public-door throttle backend (Redis via frappe.cache)."""
	global _backend
	if _backend is None:
		_backend = FrappeThrottleBackend()
	return _backend


def install_backend(backend: FrappeThrottleBackend) -> FrappeThrottleBackend:
	"""Install a custom backend (tests) and return it."""
	global _backend
	_backend = backend
	return _backend


# ---------------------------------------------------------------------------- #
# Reusable choke point — the one call every public door surface makes.
# ---------------------------------------------------------------------------- #
def _client_ip(frappe: Any) -> str | None:
	"""Best-effort request IP for the secondary throttle axis (per-IP fallback)."""
	try:
		return frappe.local.request_ip if getattr(frappe.local, "request_ip", None) else None
	except Exception:  # noqa: BLE001
		return None


def enforce_public(
	surface: str,
	*,
	device: str | None = None,
	ip: str | None = None,
	max_per_second: int = DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC,
	backend: FrappeThrottleBackend | None = None,
	now: float | None = None,
) -> None:
	"""Apply the public-door per-key throttle; raise 429 on over-limit (FLO-319).

	Builds the per-device/per-IP key via :func:`flock_os.rate_limit.build_throttle_key`
	(the primary axis is ``device`` — fingerprint / member / session user — with
	the request ``ip`` as the anonymous-socket fallback), checks the canonical
	Redis sliding-window, and on over-limit raises ``frappe.TooManyRequestsError``
	(429) with the uniform ``throttled`` reason. This is the *separate* concern
	from each surface's authorization/scope gate (FLO-319 §What): the caller still
	runs its own scope/eligibility check; the throttle neither grants nor denies
	authorization, it only bounds the request rate.

	``device`` should be the strongest stable identity the surface has:
	``device_fingerprint`` for ``join_session``, the resolved ``registrant`` for
	``register_for_event``, ``frappe.session.user`` for the realtime room join.
	When omitted, ``device`` degrades to ``None`` so :func:`build_throttle_key`
	falls back to the request IP (resolved here when not passed) — the anonymous-
	connect path.
	"""
	import frappe

	if ip is None:
		ip = _client_ip(frappe)
	key = build_throttle_key(surface, device=device, ip=ip)
	throttle_backend = backend or get_backend()
	if not throttle_backend.throttle_allows(
		key, now=now if now is not None else time.time(), max_per_second=max_per_second
	):
		frappe.throw(
			f"Too many requests — please slow down and retry ({surface}).",
			frappe.TooManyRequestsError,
			title=THROTTLE_REASON,
		)


__all__ = [
	"FrappeThrottleBackend",
	"enforce_public",
	"get_backend",
	"install_backend",
	"redis_sliding_window_allows",
]
