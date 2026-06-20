"""
Migration-drift contract tests for ``flock_os/patches.txt`` (FLO-350 Phase 6.2).

These run under plain ``pytest`` (no Frappe site / bench required) and are the
**CI migration-drift gate**: they fail the merge gate whenever the committed
patch registry drifts from the patch modules that actually ship. The two
headline drift failures this catches — both of which are *silent* at runtime
because ``bench migrate`` only runs what ``patches.txt`` lists — are:

1. **Orphan patch** — a ``flock_os/patches/vX_Y/<name>.py`` module exists but is
   not registered in ``patches.txt``. ``bench migrate`` silently skips it; the
   index/fixture/data fix it carries never lands on prod. This is the classic
   "I added the patch file but forgot to register it" bug.
2. **Dangling registry entry** — ``patches.txt`` lists a patch whose module file
   does not exist (typo, rename, bad merge). ``bench migrate`` would raise an
   ``ImportError`` mid-run, aborting the migrate on prod.

It also enforces the Frappe patch contract (every patch module defines a
top-level ``execute`` callable, verified by AST so the module is never imported
— the patches import ``frappe`` which is unavailable under the project-level
gate) and the section structure (``[pre_model_sync]`` / ``[post_model_sync]``).

Scope note: this is the **static** drift gate. A full ``bench migrate`` against
a throwaway MariaDB (the "runs migrations against a throwaway DB" level) is the
heavier Level-2 gate, staged behind the deploy pipeline ([FLO-246]
(/FLO/issues/FLO-246)) which brings the bench infrastructure. See
``docs/operations/migration-runbook.md`` §"CI migration-drift gate".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ``flock_os/`` package root (the outer package dir = repo-root flock_os).
# __file__ = flock_os/tests/test_migration_drift.py → parent.parent = flock_os/.
PKG_ROOT = Path(__file__).resolve().parent.parent
PATCHES_TXT = PKG_ROOT / "patches.txt"
PATCHES_DIR = PKG_ROOT / "patches"

EXPECTED_SECTIONS = ("pre_model_sync", "post_model_sync")


def _registered_patches() -> list[tuple[str, str]]:
	"""``[(section, dotted_path), ...]`` for every patch entry, in file order.

	Parses the Frappe ``patches.txt`` format directly: ``[section]`` headers,
	``#`` full-line comments, and one bare dotted patch path per non-comment
	line. ``configparser`` is the wrong tool here because Frappe entries are not
	``key=value`` pairs.
	"""
	assert PATCHES_TXT.exists(), f"missing patch registry: {PATCHES_TXT}"
	entries: list[tuple[str, str]] = []
	section = ""
	for raw in PATCHES_TXT.read_text().splitlines():
		line = raw.strip()
		if not line or line.startswith("#"):
			continue
		if line.startswith("[") and line.endswith("]"):
			section = line[1:-1].strip()
			continue
		entries.append((section, line.strip().strip("'\"")))
	return entries


def _patch_file(dotted: str) -> Path:
	"""Map a dotted patch path to its module file.

	``flock_os.patches.v0_1.seed_core_fixtures`` → ``patches/v0_1/seed_core_fixtures.py``
	(resolved against the package root, so the leading ``flock_os.`` maps to the
	outer package dir itself).
	"""
	rel = dotted.split(".", 1)[1] if dotted.startswith("flock_os.") else dotted
	return PKG_ROOT / (rel.replace(".", "/") + ".py")


def _has_execute(path: Path) -> bool:
	"""True iff the module defines a top-level ``execute`` callable (AST)."""
	tree = ast.parse(path.read_text(), filename=str(path))
	return any(isinstance(node, ast.FunctionDef) and node.name == "execute" for node in tree.body)


# --------------------------------------------------------------------------- #
# Registry structure
# --------------------------------------------------------------------------- #


def test_registry_has_expected_sections():
	# A section is "present" if it has at least one entry OR its header appears
	# in the file (``[pre_model_sync]`` is legitimately empty today — every
	# flock_os patch touches DocType tables and must be post_model_sync).
	sections_with_entries = {section for section, _d in _registered_patches()}
	raw_section_headers = {
		line.strip()[1:-1].strip()
		for line in PATCHES_TXT.read_text().splitlines()
		if line.strip().startswith("[") and line.strip().endswith("]")
	}
	present = sections_with_entries | raw_section_headers
	missing = [s for s in EXPECTED_SECTIONS if s not in present]
	assert not missing, f"patches.txt must define both sections {EXPECTED_SECTIONS}; missing {missing}"


def test_registry_entries_use_flock_os_prefix():
	# Every registered patch must live under the flock_os package namespace so
	# the dotted path resolves into the tracked tree (guards against a stray
	# third-party / frappe patch leaking into our registry).
	for _section, dotted in _registered_patches():
		assert dotted.startswith("flock_os.patches."), f"patch {dotted!r} must be a flock_os.patches.* path"


# --------------------------------------------------------------------------- #
# Drift: registry ↔ patch files must agree exactly
# --------------------------------------------------------------------------- #


def test_no_dangling_registry_entries():
	# Every registered patch must resolve to a real file. A dangling entry
	# aborts `bench migrate` with ImportError on prod.
	missing = [dotted for _s, dotted in _registered_patches() if not _patch_file(dotted).exists()]
	assert not missing, "patches.txt lists modules with no file (would ImportError on migrate): " + ", ".join(
		missing
	)


def test_no_orphan_patch_files():
	# Every shipped patch module must be registered. An orphan is silently
	# skipped by `bench migrate` — the fix it carries never lands. This is the
	# headline migration-drift check.
	registered = {_patch_file(dotted).resolve() for _s, dotted in _registered_patches()}
	orphans = [
		str(p.relative_to(PATCHES_DIR))
		for p in PATCHES_DIR.rglob("*.py")
		if p.name != "__init__.py" and p.resolve() not in registered
	]
	assert not orphans, (
		"patch modules not registered in patches.txt (silently skipped on migrate): "
		+ ", ".join(sorted(orphans))
	)


def test_no_duplicate_registrations():
	# A patch registered twice runs twice on every migrate (double-seed /
	# double-index attempt) and signals a copy-paste merge error.
	seen: dict[str, str] = {}
	dupes: list[str] = []
	for section, dotted in _registered_patches():
		if dotted in seen:
			dupes.append(f"{dotted} (in {seen[dotted]} and {section})")
		seen[dotted] = section
	assert not dupes, "duplicate patch registrations: " + ", ".join(dupes)


# --------------------------------------------------------------------------- #
# Frappe patch contract: each module defines execute()
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dotted", [d for _s, d in _registered_patches()])
def test_patch_module_defines_execute(dotted):
	path = _patch_file(dotted)
	assert _has_execute(path), f"{dotted}: Frappe patches must define a top-level execute() callable"
