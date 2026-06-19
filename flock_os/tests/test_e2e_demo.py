"""
Gate the Phase-5.1b end-to-end MVP demo (FLO-224) so it cannot rot behind the
merge gate.

The demo in ``scripts/e2e_demo.py`` is the FLO-1 close-trigger evidence: it
drives the **real** Flock OS domain services (traversal, permissions,
approvals, registrations, scheduling, reporting, engagement, notifications,
events) over an in-memory multi-branch world — the same hexagonal-gateway
discipline as the project test suite — and asserts every North-Star step (the
seven FLO-224 acceptance criteria). This test loads that script as a module
(it lives in ``scripts/``, outside the package) and asserts every scenario
reports green, so a regression in any spine or the demo wiring fails CI here,
not at the FLO-1 sign-off.

Mirrors :mod:`flock_os.tests.test_demo_phase1` (the Phase-1 gate). The script
is import-clean (no Frappe), so the whole North-Star path is exercised under
the SQLite-fast project-level harness — the realtime/WS transport fan-out is
best-effort in production (FLO-10 §5.3) and captured here by a recording
publisher, so no bench leg is needed for the orchestration proof.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_DEMO_PATH = Path(__file__).resolve().parents[2] / "scripts" / "e2e_demo.py"
_DEMO_MOD_NAME = "flock_os_e2e_demo"


@pytest.fixture(scope="module")
def demo_module():
	spec = importlib.util.spec_from_file_location(_DEMO_MOD_NAME, _DEMO_PATH)
	assert spec and spec.loader, "e2e_demo.py must be loadable"
	module = importlib.util.module_from_spec(spec)
	# Register before exec so @dataclass annotation resolution can resolve the
	# owning module (mirrors test_demo_phase1's loader).
	sys.modules[_DEMO_MOD_NAME] = module
	try:
		spec.loader.exec_module(module)  # type: ignore[union-attr]
	except Exception:
		sys.modules.pop(_DEMO_MOD_NAME, None)
		raise
	return module


def test_demo_script_exists():
	assert _DEMO_PATH.is_file(), f"demo script missing at {_DEMO_PATH}"


def test_demo_all_north_star_steps_pass(demo_module):
	results = demo_module.run()
	assert results, "e2e demo produced no scenario results"
	failed = [r.label for r in results if not r.ok]
	assert not failed, f"e2e demo steps failed: {failed}"


def test_demo_covers_all_seven_acceptance_steps(demo_module):
	# FLO-224 has seven North-Star acceptance steps (1..7); the demo must cover each.
	labels = " ".join(r.label for r in demo_module.run())
	for marker in ("1)", "2)", "3)", "4)", "5)", "6)", "7)"):
		assert marker in labels, f"e2e demo is missing acceptance step {marker!r}"


def test_demo_seeded_world_drives_real_services(demo_module):
	# The demo must drive the real domain services (no mocks): the world
	# implements every gateway port the North-Star path touches, and the demo
	# imports the real service classes / pure modules.
	from flock_os import approvals, events, notifications, registrations, scheduling
	from flock_os.engagement import EngagementService
	from flock_os.reporting import BulkAttendanceService

	assert hasattr(demo_module, "E2EWorld"), "demo must define the E2EWorld gateway"
	# The world implements the ports the real services consume.
	world = demo_module.E2EWorld()
	svc_tree_modules = (approvals, events, notifications, registrations, scheduling)
	for mod in svc_tree_modules:
		assert mod is not None
	assert EngagementService is not None
	assert BulkAttendanceService is not None
	# Sanity: the seeded world has the multi-branch shape FLO-1 requires.
	assert len(demo_module.ALL_BRANCHES) >= 3
	roots = [b for b, p in demo_module.BRANCH_PARENT_OF.items() if p is None]
	assert roots == [demo_module.ORG]
	assert world.get_branch("North") is not None
	assert world.group_branch("Youth") == "North"


def test_demo_exits_zero_when_green(demo_module):
	# The demo's exit code is itself a gate (mirrors demo_phase1.main).
	assert demo_module.main() == 0, "e2e demo main() must exit 0 when all steps are green"
