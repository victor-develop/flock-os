"""
Dashboard 5xx-panel metric-contract guard (FLO-909 / FLO-897 reconciliation).

The Phase 6.2 app-scrape instrumentation ([FLO-897](/FLO/issues/FLO-897)) emits a
dedicated error counter ``flock_http_errors_total{route_class}`` and a request
counter ``flock_http_requests_total{route_class}`` — **no ``status`` label** on
either. The ``App5xxSpike`` alert rule in ``docs/operations/alerting.md`` uses the
dedicated error counter as its numerator:

    sum(rate(flock_http_errors_total[1m])) by (route_class)
      / clamp_min(sum(rate(flock_http_requests_total[1m])) by (route_class), 1)

Three dashboard panels (owned by [FLO-694](/FLO/issues/FLO-694)) previously queried
``flock_http_requests_total{status=~"5.."}`` — a label the scrape never emits, so
every 5xx panel rendered empty against the live scrape. This guard pins the
reconciliation so the dead-label regression cannot silently return:

* No dashboard panel expression references the never-emitted ``status`` label on
  ``flock_http_requests_total``.
* Every 5xx panel numerator uses the canonical ``flock_http_errors_total`` counter.

Runs under plain ``pytest`` (no bench); see ``test_migration_drift.py`` for the
same repo-root-relative path resolution convention.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

# ``flock_os/`` package root (outer package dir = repo-root flock_os).
# __file__ = flock_os/tests/test_dashboard_metric_contract.py → parent.parent.
PKG_ROOT = Path(__file__).resolve().parent.parent
DASHBOARDS_DIR = PKG_ROOT.parent / "docs" / "operations" / "dashboards"

# The three dashboards that carry a 5xx panel (FLO-694).
DASHBOARD_FILES = (
	"event-day-ops.json",
	"incident-triage.json",
	"platform-health.json",
)

# Dedicated error counter emitted by the FLO-897 app scrape — the canonical 5xx
# numerator the alerting rule and dashboards must share.
ERROR_COUNTER = "flock_http_errors_total"
# The dead label matcher: the scrape emits flock_http_requests_total{route_class}
# with NO status label, so {status=...} always matches nothing.
DEAD_STATUS_LABEL = "flock_http_requests_total{status="


def _exprs(node: object) -> Iterator[str]:
	"""Yield every ``expr`` string found anywhere in a parsed dashboard JSON.

	Dashboard targets nest under ``panels[*].targets[*].expr`` (and may nest
	deeper inside collapsed row groups); a recursive walk is the robust shape.
	"""
	if isinstance(node, dict):
		for key, value in node.items():
			if key == "expr" and isinstance(value, str):
				yield value
			else:
				yield from _exprs(value)
	elif isinstance(node, list):
		for item in node:
			yield from _exprs(item)


def _load_exprs(filename: str) -> list[str]:
	path = DASHBOARDS_DIR / filename
	assert path.exists(), f"missing dashboard template: {path}"
	with path.open(encoding="utf-8") as fh:
		return list(_exprs(json.load(fh)))


@pytest.mark.parametrize("filename", DASHBOARD_FILES)
def test_no_panel_references_dead_status_label(filename: str) -> None:
	"""No dashboard expr may filter ``flock_http_requests_total`` by ``status``.

	The FLO-897 scrape emits no ``status`` label, so any such selector is a dead
	query that renders an empty panel — the exact regression FLO-909 fixes.
	"""
	dead = [expr for expr in _load_exprs(filename) if DEAD_STATUS_LABEL in expr]
	assert not dead, (
		f"{filename}: expr(s) reference the never-emitted status label (use {ERROR_COUNTER} instead): {dead}"
	)


@pytest.mark.parametrize("filename", DASHBOARD_FILES)
def test_5xx_panel_uses_dedicated_error_counter(filename: str) -> None:
	"""Each dashboard must carry a 5xx panel backed by ``flock_http_errors_total``.

	Guards both directions: the panel was not deleted, and it uses the canonical
	error counter that the ``App5xxSpike`` alert shares.
	"""
	exprs = _load_exprs(filename)
	assert any(ERROR_COUNTER in expr for expr in exprs), (
		f"{filename}: no panel references {ERROR_COUNTER}; the 5xx panel must "
		"use the dedicated error counter per the FLO-897 metric contract"
	)
