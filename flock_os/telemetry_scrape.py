"""
HTTP scrape surface for the Flock OS telemetry dashboard (FLO-53).

Exposes :func:`flock_os.telemetry.snapshot` as a Prometheus text endpoint at
``POST/GET /api/method/flock_os.telemetry_scrape.scrape`` so a Prometheus scrape
job feeds the §8 dashboards (D3/D5 revisit triggers + the bulk-latency p95 gate)
directly. Frappe is imported here (not in :mod:`flock_os.telemetry`) so the
telemetry core stays import-clean under the no-bench unit gate.
"""

from __future__ import annotations

import frappe

from flock_os.telemetry import snapshot


@frappe.whitelist()
def scrape() -> str:
	"""Return the current telemetry snapshot as Prometheus text exposition."""
	return snapshot().as_prometheus()
