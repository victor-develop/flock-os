"""Auto-(re)wiring of the flock_os realtime join handler into vendored Frappe.

Background (FLO-107 / FLO-109)
-----------------------------
Frappe v15 ships no per-app socket-handler extension point, so flock_os's
``join`` handler is wired into the bench's vendored
``apps/frappe/realtime/index.js`` by ``scripts/dev/wire-socketio-handler.sh``
as a single guarded ``require`` (marker-guarded, idempotent). A ``bench update``
(or Frappe reinstall) **rewrites** that ``index.js`` and silently drops the line
— joins then become a no-op and published broadcasts reach zero clients, with no
startup error (the runtime ``try/catch`` swallows the missing module). That is
the FLO-107 symptom recurring silently.

This module registers the wiring as a Frappe ``after_migrate`` / ``after_install``
hook (see ``flock_os.hooks``) so a ``bench update`` — which performs a
``bench migrate`` — re-inserts the handler automatically. The wire script stays
the single source of truth; this module only locates the bench + drives it.

It is bench-integration surface: ``frappe`` is imported lazily inside the hook,
and the pure path resolution (``bench_root_from_app_path``) is unit-tested under
plain ``pytest`` (no bench). ``flock_os/utils/*`` is intentionally omitted from
the coverage ratchet for the same reason as the other bench adapters.
"""

from __future__ import annotations

import os
import subprocess

# Relative path to the idempotent wiring script, from the flock_os app root
# (the repo root, which the bench exposes via apps/flock_os).
WIRE_SCRIPT_REL = ("scripts", "dev", "wire-socketio-handler.sh")


def bench_root_from_app_path(app_path: str) -> str:
	"""Resolve the bench root from a flock_os app install path.

	``<bench>/apps/flock_os`` -> ``<bench>/apps`` -> ``<bench>``. Pure (no
	Frappe) so it is testable under plain ``pytest``.
	"""
	apps_dir = os.path.dirname(app_path)  # <bench>/apps
	return os.path.dirname(apps_dir)  # <bench>


def rewire_socketio_handler() -> None:
	"""Frappe ``after_migrate`` / ``after_install`` hook: re-wire the join handler.

	Idempotent: the wire script is a no-op when the marker is already present,
	so this is cheap to run on every migrate. Best-effort: any failure is logged
	as a warning but never breaks ``migrate``/``install`` — the wiring can always
	be re-run by hand (``scripts/dev/wire-socketio-handler.sh``).
	"""
	import frappe

	app_path = frappe.get_app_path("flock_os")
	script = os.path.join(app_path, *WIRE_SCRIPT_REL)
	bench_root = bench_root_from_app_path(app_path)
	logger = frappe.logger("flock_os")
	try:
		result = subprocess.run(
			["bash", script, "--bench", bench_root],
			check=True,
			capture_output=True,
			text=True,
		)
		logger.info(
			"flock_os realtime handler wiring verified by %s: %s",
			os.path.relpath(script, app_path),
			(result.stdout or "").strip() or "ok",
		)
	except Exception as exc:  # noqa: BLE001 - never break migrate/install
		logger.warning(
			"flock_os realtime handler auto-wire failed (%s); re-run "
			"scripts/dev/wire-socketio-handler.sh manually so the join handler "
			"is not silently dropped.",
			exc,
		)
