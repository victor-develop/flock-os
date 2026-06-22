#!/usr/bin/env python3
"""Extract the complete Flock OS doctype permission matrix for the FLO-896 audit.

Reads every custom doctype JSON under flock_os/flock_os/doctype/, emits:
  1. per-doctype role -> capability map (permlevel-aware)
  2. branch-bearing doctypes (the branch-scoping anchor) -> row-level axis coverage
  3. cross-check vs permissions.SCOPED_DOCTYPES (group-axis hook) +
     MEMBER_ANCHORED_DOCTYPES (self-membership axis).

Pure read-only; no Frappe needed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCTYPE_DIR = ROOT / "flock_os" / "flock_os" / "doctype"
PERM_MOD = ROOT / "flock_os" / "permissions.py"

CAPS = (
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"report",
	"import",
	"export",
	"print",
	"email",
	"share",
	"set_user_permissions",
)


def load_doctypes() -> list[dict]:
	out = []
	for child in sorted(p.name for p in DOCTYPE_DIR.iterdir() if p.is_dir() and not p.name.startswith("__")):
		path = DOCTYPE_DIR / child / f"{child}.json"
		if not path.exists():
			continue
		with path.open() as f:
			out.append(json.load(f))
	return out


def extract_scoped_doctypes() -> tuple[tuple[str, ...], dict[str, str]]:
	"""Parse SCOPED_DOCTYPES + MEMBER_ANCHORED_DOCTYPES from permissions.py source."""
	src = PERM_MOD.read_text()
	scoped: tuple[str, ...] = ()
	member_anchored: dict[str, str] = {}
	# crude but safe: pull the tuple literal
	import ast

	# SCOPED_DOCTYPES
	i = src.find("SCOPED_DOCTYPES: tuple[str, ...] = (")
	if i != -1:
		j = src.find(")", i)
		scoped = tuple(ast.literal_eval(src[i + len("SCOPED_DOCTYPES: tuple[str, ...] = ") : j + 1]))
	# MEMBER_ANCHORED_DOCTYPES
	i = src.find("MEMBER_ANCHORED_DOCTYPES: dict[str, str] = {")
	if i != -1:
		j = src.find("}", i)
		lit = src[i + len("MEMBER_ANCHORED_DOCTYPES: dict[str, str] = ") : j + 1]
		member_anchored = dict(ast.literal_eval(lit))
	return scoped, member_anchored


def main() -> int:
	doctypes = load_doctypes()
	scoped, member_anchored = extract_scoped_doctypes()
	scoped_set = set(scoped)

	print("=" * 78)
	print(f"FLOCK OS PERMISSION MATRIX — {len(doctypes)} custom doctypes")
	print("=" * 78)

	branch_doctypes: list[str] = []
	member_doctypes: list[str] = []

	for dt in doctypes:
		name = dt["name"]
		perms = dt.get("permissions", [])
		fields = dt.get("fields", [])
		field_names = {f["fieldname"] for f in fields}
		has_branch = "branch" in field_names
		has_member = "member" in field_names
		has_group = "group" in field_names
		if has_branch:
			branch_doctypes.append(name)
		if has_member:
			member_doctypes.append(name)

		print(f"\n### {name}")
		meta = []
		if dt.get("is_tree"):
			meta.append("is_tree")
		if dt.get("autoname"):
			meta.append(f"autoname={dt['autoname']}")
		meta.append(f"fields={len(fields)}")
		if has_branch:
			meta.append("HAS_BRANCH")
		if has_group:
			meta.append("has_group")
		if has_member:
			meta.append("has_member")
		if name in scoped_set:
			meta.append("GROUP-SCOPED-HOOK")
		if name in member_anchored:
			meta.append(f"member-anchor({member_anchored[name]})")
		print("  " + " | ".join(meta))

		if not perms:
			print("  (no permissions[])")
			continue
		# collapse permlevels
		for p in sorted(perms, key=lambda r: (r.get("permlevel", 0), r.get("role", ""))):
			role = p.get("role", "?")
			pl = p.get("permlevel", 0)
			on = [c for c in CAPS if p.get(c) == 1]
			print(f"    pl{pl} {role:24s} -> {', '.join(on) if on else '(read-neg-only)'}")

	print("\n" + "=" * 78)
	print("BRANCH-SCOPED DOCTYPES (carry a `branch` field)")
	print("=" * 78)
	for n in branch_doctypes:
		flags = []
		if n in scoped_set:
			flags.append("group-axis-hook")
		if n in member_anchored:
			flags.append(f"member-anchor:{member_anchored[n]}")
		print(f"  {n:40s} {'[' + ', '.join(flags) + ']' if flags else '[branch-axis ONLY]'}")

	print("=" * 78)
	print("AXES COVERAGE GAP CHECK")
	print("=" * 78)
	# Branch axis is native (User Permissions) -> applies to anything with a
	# branch link field automatically. Group axis (SCOPED_DOCTYPES) is explicit.
	# We flag doctypes missing from BOTH (no branch field AND not group-scoped).
	print(f"SCOPED_DOCTYPES (group-axis hook): {len(scoped)}")
	for s in scoped:
		print(f"  - {s}")
	print(
		f"\nbranch-bearing doctypes ({len(branch_doctypes)}): covered by native User-Permission branch axis"
	)
	for b in branch_doctypes:
		print(f"  - {b}")

	# Doctypes that are NEITHER branch-bearing NOR group-scoped.
	no_isolation = [
		dt["name"]
		for dt in doctypes
		if "branch" not in {f["fieldname"] for f in dt.get("fields", [])} and dt["name"] not in scoped_set
	]
	print(f"\nNO branch field AND not group-scoped ({len(no_isolation)}):")
	for n in no_isolation:
		print(f"  ! {n}")

	return 0


if __name__ == "__main__":
	sys.exit(main())
