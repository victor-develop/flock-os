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
