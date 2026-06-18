"""
Canonical event-emitter + naming convention lock (ADR-0001 §5, FLO-43).

These tests pin the Architect ruling that [FLO-43](/FLO/issues/FLO-43) locks:

* **One emitter.** ``flock_os.events`` (app root) is the *only* sanctioned
  publish path. The inner ``flock_os.flock_os`` package must NOT carry a rival
  ``events`` emitter (the divergence FLO-43 was opened against). Importing it
  must fail so a second stub can never silently regress.
* **One naming convention.** Every catalog event name is ``flock.``-prefixed and
  follows ``flock.<aggregate>.<verb[-past]>`` — the ADR §5.4 table that the
  realtime projector ([FLO-14](/FLO/issues/FLO-14)) subscribes to and that the
  reporting write path ([FLO-15](/FLO/issues/FLO-15)) emits.
* **One channel derivation.** ``pubsub_channel(name) == "flock:" + name``.

Runs under plain ``pytest`` (no bench) so the convention stays enforced in CI.
"""

from __future__ import annotations

import importlib

import pytest

import flock_os.events as flock_events

# The full catalog the Architect owns (ADR-0001 §5.4). Enumerated so a renamed
# or dropped constant surfaces as a reviewable diff rather than a silent drift.
_CATALOG = [
	flock_events.BRANCH_CREATED,
	flock_events.BRANCH_MOVED,
	flock_events.GROUP_CREATED,
	flock_events.GROUP_MOVED,
	flock_events.GROUP_MEMBER_ADDED,
	flock_events.MEMBER_CREATED,
	flock_events.GATHERING_CREATED,
	flock_events.GATHERING_CANCELLED,
	flock_events.GATHERING_SUBMITTED,
	flock_events.GATHERING_APPROVED,
	flock_events.ATTENDANCE_RECORDED,
	flock_events.ATTENDANCE_BULK_RECORDED,
	flock_events.ATTENDANCE_BATCH_REJECTED,
	flock_events.ATTENDANCE_IMPORT_FAILED,
	flock_events.ENGAGEMENT_SESSION_OPENED,
	flock_events.ENGAGEMENT_SESSION_CLOSED,
	flock_events.ANNOUNCEMENT_SCHEDULED,
	flock_events.ANNOUNCEMENT_PUBLISHED,
	flock_events.NOTIFICATION_SENT,
	flock_events.APPROVAL_REQUESTED,
	flock_events.APPROVAL_DECIDED,
]


# --------------------------------------------------------------------------- #
# Single-emitter invariant (the core of FLO-43).
# --------------------------------------------------------------------------- #
def test_inner_package_has_no_events_emitter():
	"""The inner ``flock_os.flock_os`` package must not ship a rival emitter.

	FLO-43 reconciled a putative dual-emitter split. The canonical ruling is
	Option A: ``flock_os.events`` (app root) is the only emitter. This guard
	keeps a second ``events`` module from regressing into the inner package.
	"""
	inner = importlib.import_module("flock_os.flock_os")
	assert not hasattr(inner, "emit"), (
		"flock_os.flock_os must not expose an emit() — use flock_os.events.emit"
	)
	assert not hasattr(inner, "on_doc_event"), (
		"flock_os.flock_os must not expose on_doc_event — use flock_os.events"
	)
	with pytest.raises(ImportError):
		importlib.import_module("flock_os.flock_os.events")


# --------------------------------------------------------------------------- #
# Naming convention (ADR-0001 §5.4).
# --------------------------------------------------------------------------- #
def test_catalog_is_non_empty_and_unique():
	assert _CATALOG, "the domain event catalog must be populated"
	assert len(_CATALOG) == len(set(_CATALOG)), "duplicate catalog event names"


@pytest.mark.parametrize("name", _CATALOG)
def test_every_catalog_event_is_flock_prefixed(name):
	assert name.startswith("flock."), f"{name!r} must be 'flock.'-prefixed (ADR §5.4)"


@pytest.mark.parametrize("name", _CATALOG)
def test_every_catalog_event_has_aggregate_and_verb(name):
	# Shape: flock.<aggregate>.<verb[-past]>  → at least three dot-segments.
	segments = name.split(".")
	assert len(segments) >= 3, f"{name!r} must follow flock.<aggregate>.<verb> (got {segments})"


def test_pubsub_channel_derivation_is_stable():
	# The realtime projector + Redis subscribers key off this derivation
	# (FLO-14). Pinning it keeps the channel-name contract from drifting.
	for name in _CATALOG:
		assert flock_events.pubsub_channel(name) == f"flock:{name}"


# --------------------------------------------------------------------------- #
# Catalog single-source-of-truth (FLO-47).
#
# The reporting write path ([FLO-15](/FLO/issues/FLO-15)) emits these events via
# its gateway port under the ``EVENT_*`` vocabulary. They must alias the
# canonical catalog constants (ADR-0001 §5.3/§5.4) — never duplicate the string
# literals — so a rename in one place propagates and the catalog stays the only
# source of truth. ``import_failed`` was the gap that prompted FLO-47; this guard
# keeps all three from regressing back to duplicated literals.
# --------------------------------------------------------------------------- #
def test_reporting_event_names_alias_the_catalog():
	from flock_os import reporting

	assert reporting.EVENT_BULK_RECORDED is flock_events.ATTENDANCE_BULK_RECORDED
	assert reporting.EVENT_BATCH_REJECTED is flock_events.ATTENDANCE_BATCH_REJECTED
	assert reporting.EVENT_IMPORT_FAILED is flock_events.ATTENDANCE_IMPORT_FAILED
