"""
App-layer sliding-window rate-limit primitive — FLO-319 (infra-independent
slice of [FLO-294](/FLO/issues/FLO-294), SEC-RL).

A single canonical per-key 1s sliding-window throttle reused across every
high-fan-out **public** entry point so each surface gets a uniform
``throttled`` / 429-style rejection without a bespoke Redis client. This is the
**same primitive** the engagement runtime already productionized for
``engagement.participate`` (FLO-9 §6.6, :meth:`EngagementGateway.throttle_allows`);
FLO-319 extends it to the three public door surfaces that had no app-layer limit:

* realtime Socket.IO ``connect`` / room ``join``
  (:func:`flock_os.realtime_views.can_join_event_room`),
* ``registrations.register_for_event``
  (:func:`flock_os.flock_os.doctype.flock_event_registration.flock_event_registration.register_for_event`),
* ``engagement.join_session`` — the signed-ticket **issue** path
  (:func:`flock_os.engagement_api.join_session`).

Layering (ADR-0001 §2 separation of concerns; rate-limit is a *separate* concern
from the authorization/scope gate — FLO-319 §What)::

    @frappe.whitelist() surface              (transport, bench-only)
      -> enforce_public(surface, device=, ip=)   (flock_os.rate_limit_frappe)
         |-> build_throttle_key(...)             (THIS module, pure)
         |-> backend.throttle_allows(...)        (ThrottleBackend port)
         -> frappe.throw(... TooManyRequests)     (429, on over-limit)

This module is **import-clean by design** (no top-level ``import frappe``, no
I/O) so the sliding-window semantics + key building + the ``enforce`` decision
run under plain ``pytest`` — the same hexagonal discipline as
:mod:`flock_os.registrations` / :mod:`flock_os.realtime`. The Redis-backed
production backend lives in :mod:`flock_os.rate_limit_frappe`; the in-memory
backend here is the unit-test double.

The :class:`ThrottleBackend` port is structurally satisfied by both the
engagement runtime's gateways (:class:`flock_os.engagement.InMemoryEngagementGateway`
/ :class:`flock_os.engagement_frappe.FrappeEngagementGateway`) — the
``throttle_allows`` signature is identical — so the primitive is reused, not
reinvented. No new Redis client is introduced (ADR-0001 §5); the production
backend routes through ``frappe.cache()`` (Redis), the sanctioned abstraction.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------- #
# Tunables (config-over-constants; ADR-0001 §7). The Frappe adapter may resolve
# org-level overrides from ``Flock Organization`` settings when those land; the
# value here is the service-layer default for public door surfaces.
# ---------------------------------------------------------------------------- #
DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC = 10
"""Per-key cap for public high-fan-out entry points (FLO-319).

A 1s sliding window of ≤10 calls/identity — generous for a legitimate human
action (register / join / connect happens once) but tight enough to smother a
script slamming the door. Distinct from the in-flight
:data:`flock_os.engagement.DEFAULT_PARTICIPATION_THROTTLE_PER_SEC` (5/s) which
bounds interaction *bursts* mid-session; this bounds *entry* attempts. The
coarse edge-layer limits stay in FLO-294 (Cloudflare, topology-dependent).
"""

THROTTLE_REASON = "throttled"
"""Uniform rejection reason surfaced across the public endpoints (429-style)."""

# ---------------------------------------------------------------------------- #
# Port — the I/O surface the sliding-window decision depends on. Structurally
# satisfied by the engagement gateways (same ``throttle_allows`` shape), so the
# primitive is reused rather than reinvented (FLO-319 §What: "reuse throttle_allows").
# ---------------------------------------------------------------------------- #


@runtime_checkable
class ThrottleBackend(Protocol):
	"""Per-``key`` 1s sliding-window rate-limit port (mirrors FLO-9 §6.6).

	Production adapter: :class:`flock_os.rate_limit_frappe.FrappeThrottleBackend`
	(Redis via ``frappe.cache()``). Unit tests:
	:class:`InMemoryThrottleBackend`. The engagement runtime's gateways satisfy
	this protocol structurally — the ``throttle_allows`` contract is identical.
	"""

	def throttle_allows(self, key: str, *, now: float, max_per_second: int) -> bool:
		"""Record one attempt at ``now`` and return whether it is allowed.

		Returns ``True`` if the call is within ``max_per_second`` for the trailing
		1s window (and records the timestamp), ``False`` if the cap is already
		reached (the over-limit attempt is *not* recorded — it does not push the
		window forward). Best-effort in the production adapter: a Redis failure
		degrades to allow (the throttle protects the log / door, not correctness).
		"""
		...


class InMemoryThrottleBackend:
	"""Reference in-memory adapter for unit tests (no Frappe / no Redis).

	The same sliding-window semantics as the production Redis backend: a per-key
	list of recent timestamps, pruned to the trailing 1s window, capped at
	``max_per_second``. Over-limit attempts are not recorded so they cannot
	extend the window.
	"""

	def __init__(self) -> None:
		self._buckets: dict[str, list[float]] = {}

	def throttle_allows(self, key: str, *, now: float, max_per_second: int) -> bool:
		bucket = self._buckets.setdefault(key, [])
		cutoff = now - 1.0
		bucket[:] = [ts for ts in bucket if ts >= cutoff]
		if len(bucket) >= max_per_second:
			return False
		bucket.append(now)
		return True


# ---------------------------------------------------------------------------- #
# Throttle decision — pure, transport-free, unit-testable.
# ---------------------------------------------------------------------------- #
class ThrottledError(Exception):
	"""Raised by :func:`enforce` when ``key`` exceeds its per-second cap.

	Carries the decision context so the transport layer can surface a uniform
	429-style response (``reason = THROTTLE_REASON``). The bench-side
	:func:`flock_os.rate_limit_frappe.enforce_public` catches this and maps it to
	``frappe.throw(..., frappe.TooManyRequestsError)``; unit tests assert it
	directly without a bench.
	"""

	reason = THROTTLE_REASON

	def __init__(self, *, surface: str, key: str, max_per_second: int) -> None:
		self.surface = surface
		self.key = key
		self.max_per_second = max_per_second
		super().__init__(
			f"{THROTTLE_REASON}: {surface} cap {max_per_second}/s exceeded (key={key!r})"
		)


def build_throttle_key(
	surface: str, *, device: str | None = None, ip: str | None = None
) -> str:
	"""Build the per-device/per-IP throttle key for a public ``surface``.

	The strongest stable identity wins so a reconnecting device is bounded
	consistently: an explicit ``device`` (fingerprint / member / session user)
	first, then the request ``ip`` (anonymous socket connect / guest), then a
	last-resort ``anonymous`` bucket so a keyless flood is still contained.
	Namespaced under ``surface`` so the three doors never share a bucket.
	"""
	identity = device or ip or "anonymous"
	return f"{surface}:{identity}"


def enforce(
	backend: ThrottleBackend,
	key: str,
	*,
	surface: str,
	now: float,
	max_per_second: int,
) -> None:
	"""Raise :class:`ThrottledError` if ``key`` is over its cap, else return.

	Pure decision over the injectable :class:`ThrottleBackend` port — the
	transport-free core the public surfaces reuse. ``surface`` is carried on the
	error purely for the 429 message; it does **not** participate in the key (the
	caller already baked it in via :func:`build_throttle_key`).
	"""
	if not backend.throttle_allows(key, now=now, max_per_second=max_per_second):
		raise ThrottledError(surface=surface, key=key, max_per_second=max_per_second)


__all__ = [
	"DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC",
	"InMemoryThrottleBackend",
	"THROTTLE_REASON",
	"ThrottleBackend",
	"ThrottledError",
	"build_throttle_key",
	"enforce",
]
