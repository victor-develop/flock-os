"""
Seed fixture data for Flock OS (ADR-0001 §6.4, FLO-5 §4.1/§3.3).

This module is **pure data** — no Frappe import — so it is safe to import from
:mod:`flock_os.hooks` (for the ``fixtures`` export config) and from the versioned
patch that materializes these records on ``bench migrate``. The patch is the
authoritative, idempotent seeder; the ``hooks.py`` ``fixtures`` list mirrors the
same names so the seeded records travel with the app when exported.

Roles (FLO-5 §4.1 catalog):

    Flock Org Admin    — all branches, all groups (root).
    Flock Branch Admin — their branch + descendant branches.
    Flock Group Leader — branch + subtree of led groups.
    Flock Member       — self + groups they belong to (read).
    Flock Visitor      — self only.
    Flock Auditor      — read-only across all branches (compliance).

Group Types (FLO-5 §3.3 seed values): Ministry, Cell Group, Service Team,
Campus, Youth, Choir, Other (extensible — no hard-coding beyond the seed).
"""

from __future__ import annotations

FLOCK_ROLES: tuple[str, ...] = (
	"Flock Org Admin",
	"Flock Branch Admin",
	"Flock Group Leader",
	"Flock Member",
	"Flock Visitor",
	"Flock Auditor",
)

FLOCK_GROUP_TYPES: tuple[dict[str, str | int], ...] = (
	{"group_type_name": "Ministry", "is_active": 1},
	{"group_type_name": "Cell Group", "is_active": 1},
	{"group_type_name": "Service Team", "is_active": 1},
	{"group_type_name": "Campus", "is_active": 1},
	{"group_type_name": "Youth", "is_active": 1},
	{"group_type_name": "Choir", "is_active": 1},
	{"group_type_name": "Other", "is_active": 1},
)

FLOCK_GROUP_TYPE_NAMES: tuple[str, ...] = tuple(t["group_type_name"] for t in FLOCK_GROUP_TYPES)


FLOCK_GATHERING_TYPES: tuple[dict[str, str | int], ...] = (
	{"gathering_type_name": "Sunday Service", "is_active": 1, "requires_confirmation": 1},
	{"gathering_type_name": "Cell Group", "is_active": 1},
	{"gathering_type_name": "Bible Study", "is_active": 1},
	{"gathering_type_name": "Prayer Meeting", "is_active": 1},
	{"gathering_type_name": "Youth Gathering", "is_active": 1},
	{"gathering_type_name": "Special Event", "is_active": 1},
)

FLOCK_GATHERING_TYPE_NAMES: tuple[str, ...] = tuple(t["gathering_type_name"] for t in FLOCK_GATHERING_TYPES)


# ---------------------------------------------------------------------------- #
# Smoke fixtures for the FLO-10 §8 load / WS gate (load/README.md -> Runtime
# fixtures, [FLO-112](/FLO/issues/FLO-112), [FLO-114](/FLO/issues/FLO-114)).
#
# These are **runtime smoke data, not canonical catalog fixtures** — they are
# NOT auto-seeded on `bench migrate` (that would pollute every production site
# with test rows). They are materialized on demand against a bench by
# `flock_os.utils.smoke_fixtures` so the §8 WS room-join scope gate resolves
# `gathering-smoke` -> `branch-smoke` and the leader can join (the same chain
# the k6 smoke + the broadcast producer assume). Kept here (pure data, no
# Frappe import) so the seed shape is unit-testable under plain pytest.
#
# Org note (FLO-114): ``Flock Organization`` is a singleton (1 per site, FLO-5
# §8.1). The smoke does NOT create a parallel ``org-smoke`` — that violates the
# 1-org-per-site invariant (the stale runbook's failure). The seeder reuses the
# site's existing single org, falling back to creating the singleton (labeled
# below) only on a completely empty site. So there is no smoke-owned org PK;
# branch/group/gathering attach to the site's real tenant org.
# ---------------------------------------------------------------------------- #
#: The ``organization_name`` label for the smoke org, used ONLY when the seeder
#: must create the singleton on an empty site (FLO-114). NOT a row PK — the
#: smoke reuses the site's existing single ``Flock Organization``.
FLOCK_SMOKE_ORG_NAME = "Flock Smoke Org"
#: The branch the leader is scoped to (a Flock Branch row -> the site org).
FLOCK_SMOKE_BRANCH = "branch-smoke"
#: A branch-bound group the gathering attaches to (Flock Group -> branch).
FLOCK_SMOKE_GROUP = "group-smoke"
#: The gathering whose broadcast room the smoke joins (Flock Gathering -> branch+group).
FLOCK_SMOKE_GATHERING = "gathering-smoke"
#: The smoke user (Flock Group Leader) holding a single Flock Branch User
#: Permission = branch-smoke so the scope gate resolves (load/config.js).
FLOCK_SMOKE_USER = "leader@flock.os"
FLOCK_SMOKE_USER_PASSWORD = "flock"
FLOCK_SMOKE_GATHERING_TITLE = "Smoke Gathering"
