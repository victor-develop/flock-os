"""
Gate the Phase-1 demo (FLO-52) so it cannot rot behind the merge gate.

The demo in ``scripts/demo_phase1.py`` is the FLO-52 sign-off evidence: it
drives the real spine (:class:`flock_os.traversal.TreeTraversalService` + the
:mod:`flock_os.permissions` scoping API) over an in-memory multi-branch world
and asserts every DoD #5 guarantee. This test loads that script as a module
(it lives in ``scripts/``, outside the package) and asserts every scenario
reports green — so a regression in the spine or the demo world fails CI here,
not at sign-off time.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_DEMO_PATH = Path(__file__).resolve().parents[2] / "scripts" / "demo_phase1.py"
_DEMO_MOD_NAME = "flock_os_demo_phase1"


@pytest.fixture(scope="module")
def demo_module():
	spec = importlib.util.spec_from_file_location(_DEMO_MOD_NAME, _DEMO_PATH)
	assert spec and spec.loader, "demo_phase1.py must be loadable"
	module = importlib.util.module_from_spec(spec)
	# Register before exec: @dataclass annotation resolution looks up the
	# owning module in sys.modules (dataclasses._is_type). Without this the
	# standalone script still runs fine, but importlib loading would AttributeError.
	sys.modules[_DEMO_MOD_NAME] = module
	try:
		spec.loader.exec_module(module)  # type: ignore[union-attr]
	except Exception:
		sys.modules.pop(_DEMO_MOD_NAME, None)
		raise
	return module


def test_demo_script_exists():
	assert _DEMO_PATH.is_file(), f"demo script missing at {_DEMO_PATH}"


def test_demo_all_scenarios_pass(demo_module):
	results = demo_module.run()
	assert results, "demo produced no scenario results"
	failed = [r.label for r in results if not r.ok]
	assert not failed, f"Phase-1 demo scenarios failed: {failed}"


def test_demo_covers_all_four_dod_criteria(demo_module):
	# DoD #5 has four sign-off criteria (a..d); the demo must cover each.
	labels = " ".join(r.label for r in demo_module.run())
	for marker in ("a)", "b)", "c)", "d)"):
		assert marker in labels, f"demo is missing DoD #5 scenario {marker!r}"


def test_demo_seeded_world_is_multi_branch(demo_module):
	# DoD #5a: root org -> >=2 branches (one nested) -> nested groups across branches.
	assert len(demo_module.ALL_BRANCHES) >= 3
	# At least two top-level branches under the root org.
	roots = [b for b, p in demo_module.BRANCH_PARENT_OF.items() if p is None]
	assert len(roots) == 1  # exactly one root org
	children_of_root = [b for b, p in demo_module.BRANCH_PARENT_OF.items() if p == roots[0]]
	assert len(children_of_root) >= 2  # >=2 branches
	# A group subtree is branch-bound (ADR §4.2): every child group inherits its
	# parent group's branch — verified end-to-end via the structural validator.
	for group, parent in demo_module.GROUP_PARENT_OF.items():
		if parent is not None:
			demo_module.trees.validate_group_branch_binding(
				parent_branch=demo_module.GROUP_BRANCH[parent],
				child_branch=demo_module.GROUP_BRANCH[group],
			)
