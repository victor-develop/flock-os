"""
Realtime room-subscribe scope gate unit tests — FLO-106.

Pins the subscribe-side gate of FLO-14's realtime fan-out (FLO-10 ADR §5.1, §8)
under plain ``pytest`` (no Frappe site / Redis / bench), mirroring the SQL-light
project-level pattern. Two surfaces:

* :func:`flock_os.realtime.parse_event_room` — only ``flock_os:event:*`` rooms
  route through the gate; malformed / foreign rooms are rejected so they can
  never become unscoped subscriptions.
* :func:`flock_os.realtime.event_room_join_allowed` — the branch-scope decision
  reused from :mod:`flock_os.permissions` (``can_access_branch``). Global roles
  pass; a Branch Admin / Group Leader passes iff the gathering's branch is in
  their materialized allowed set; an unknown gathering fails closed.

The pure decision is exercised with injected fakes (a mapping branch resolver +
a stub permission gateway), so the gate's allow / deny / bypass / not-found
branches are covered with zero bench coupling.
"""

from __future__ import annotations

import pytest

from flock_os import permissions
from flock_os.realtime import (
	EventRoomRef,
	_MappingEventBranchResolver,
	event_room_join_allowed,
	parse_event_room,
)


class _StubGateway:
	"""Minimal permission gateway: fixed roles + materialized branch set."""

	def __init__(self, *, roles=(), allowed=()) -> None:
		self._roles = frozenset(roles)
		self._allowed = tuple(allowed)

	def get_user_roles(self, user: str) -> frozenset[str]:  # noqa: ARG002
		return self._roles

	def list_branch_user_permissions(self, user: str) -> tuple[str, ...]:  # noqa: ARG002
		return self._allowed


def _resolver(branches: dict[str, str]) -> _MappingEventBranchResolver:
	return _MappingEventBranchResolver(branches)


# --------------------------------------------------------------------------- #
# parse_event_room
# --------------------------------------------------------------------------- #
class TestParseEventRoom:
	def test_broadcast_room(self):
		ref = parse_event_room("flock_os:event:gathering-smoke:broadcast")
		assert ref == EventRoomRef(event_id="gathering-smoke", is_broadcast=True)

	def test_shard_room(self):
		ref = parse_event_room("flock_os:event:g1:shard:7")
		assert ref == EventRoomRef(event_id="g1", shard=7)

	def test_shard_zero(self):
		ref = parse_event_room("flock_os:event:g1:shard:0")
		assert ref == EventRoomRef(event_id="g1", shard=0)

	@pytest.mark.parametrize(
		"room",
		[
			"",  # empty
			"doc:Flock Gathering/x",  # a Frappe doc room, not ours
			"doctype:Flock Gathering",
			"user:leader@flock.os",
			"flock_os:event:no-segment",  # missing :broadcast|:shard:<k>
			"flock_os:event:g1:shard",  # shard without index
			"flock_os:event:g1:whitelist",  # unknown segment
			"other-app:event:g1:broadcast",  # foreign prefix
			"flock_os:event::broadcast",  # empty event id
		],
	)
	def test_rejects_non_flock_or_malformed(self, room):
		assert parse_event_room(room) is None


# --------------------------------------------------------------------------- #
# event_room_join_allowed
# --------------------------------------------------------------------------- #
class TestEventRoomJoinAllowed:
	gathering_branches = {"g-a": "branch-a", "g-b": "branch-b"}

	def test_branch_admin_allowed_for_own_branch(self):
		gw = _StubGateway(roles=[permissions.ROLE_BRANCH_ADMIN], allowed=["branch-a"])
		assert event_room_join_allowed(
			room="flock_os:event:g-a:broadcast",
			user="leader@flock.os",
			gateway=gw,
			resolver=_resolver(self.gathering_branches),
		)

	def test_branch_admin_denied_for_other_branch(self):
		gw = _StubGateway(roles=[permissions.ROLE_BRANCH_ADMIN], allowed=["branch-a"])
		assert not event_room_join_allowed(
			room="flock_os:event:g-b:broadcast",
			user="leader@flock.os",
			gateway=gw,
			resolver=_resolver(self.gathering_branches),
		)

	def test_subtree_scope_uses_materialized_allowed_set(self):
		# A regional admin's materialized allowed set already contains the
		# descendant branches (ADR §6.2), so membership == subtree scope.
		gw = _StubGateway(
			roles=[permissions.ROLE_BRANCH_ADMIN],
			allowed=["branch-north", "branch-north-child"],
		)
		resolver = _resolver({"g-child": "branch-north-child"})
		assert event_room_join_allowed(
			room="flock_os:event:g-child:shard:3",
			user="regional@flock.os",
			gateway=gw,
			resolver=resolver,
		)

	def test_shard_room_uses_same_branch_scope_as_broadcast(self):
		gw = _StubGateway(roles=[permissions.ROLE_BRANCH_ADMIN], allowed=["branch-a"])
		assert event_room_join_allowed(
			room="flock_os:event:g-a:shard:0",
			user="leader@flock.os",
			gateway=gw,
			resolver=_resolver(self.gathering_branches),
		)
		assert not event_room_join_allowed(
			room="flock_os:event:g-b:shard:0",
			user="leader@flock.os",
			gateway=gw,
			resolver=_resolver(self.gathering_branches),
		)

	@pytest.mark.parametrize("role", [permissions.ROLE_ORG_ADMIN, permissions.ROLE_AUDITOR])
	def test_global_branch_role_passes_any_branch(self, role):
		# Global-branch roles see every branch — empty allowed list on purpose.
		gw = _StubGateway(roles=[role], allowed=[])
		assert event_room_join_allowed(
			room="flock_os:event:g-b:broadcast",
			user="org@flock.os",
			gateway=gw,
			resolver=_resolver(self.gathering_branches),
		)

	def test_unknown_gathering_fails_closed(self):
		# A real gathering always carries a branch (branch-bound, ADR §4.2), so a
		# None branch == not found → deny (never falls through to the org-wide
		# null-branch allow in can_access_branch).
		gw = _StubGateway(roles=[permissions.ROLE_BRANCH_ADMIN], allowed=["branch-a"])
		assert not event_room_join_allowed(
			room="flock_os:event:does-not-exist:broadcast",
			user="leader@flock.os",
			gateway=gw,
			resolver=_resolver({}),  # gathering not seeded
		)

	def test_invalid_room_denied(self):
		gw = _StubGateway(roles=[permissions.ROLE_ORG_ADMIN], allowed=[])
		assert not event_room_join_allowed(
			room="doc:Flock Gathering/x",
			user="org@flock.os",
			gateway=gw,
			resolver=_resolver(self.gathering_branches),
		)

	def test_no_branch_scope_denies(self):
		# A user with no global role and an empty allowed set cannot join any
		# branch-scoped gathering room (tenant isolation, ADR §6.2).
		gw = _StubGateway(roles=[], allowed=[])
		assert not event_room_join_allowed(
			room="flock_os:event:g-a:broadcast",
			user="outsider@flock.os",
			gateway=gw,
			resolver=_resolver(self.gathering_branches),
		)
