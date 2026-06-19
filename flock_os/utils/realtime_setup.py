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

Path resolution is deliberately anchor-based rather than derived from
``frappe.get_app_path``: the flock_os app is installed as a **symlink** to this
repo, and ``get_app_path`` resolves through it to the *package* dir, not the app
root — so the script would not be found and the bench root would be wrong. We
instead walk up from ``frappe.__file__`` (the non-symlinked framework install)
to the directory holding ``apps/`` + ``sites/``, and walk up from this module to
the repo root that contains ``scripts/dev/``. Both are pure + unit-tested.

This is bench-integration surface: ``frappe`` is imported lazily inside the
hook, and ``flock_os/utils/*`` is intentionally omitted from the coverage
ratchet (same as the other bench adapters).
"""

from __future__ import annotations

import os
import subprocess

# Relative path to the idempotent wiring script, from the repo/app root that
# holds this module under flock_os/utils/.
WIRE_SCRIPT_REL = ("scripts", "dev", "wire-socketio-handler.sh")

# A bench root is a directory containing both of these siblings.
_BENCH_MARKERS = ("apps", "sites")


def _walk_up(start: str):
	d = os.path.abspath(start)
	while True:
		yield d
		parent = os.path.dirname(d)
		if parent == d:
			return
		d = parent


def find_bench_root(frappe_file: str) -> str | None:
	"""Resolve the bench root from ``frappe.__file__``.

	Walks up from the framework install dir to the first directory holding both
	``apps/`` and ``sites/`` (the bench layout). Robust to the exact install
	depth and to the flock_os symlink (frappe itself is not symlinked).
	"""
	for d in _walk_up(os.path.dirname(os.path.abspath(frappe_file))):
		if all(os.path.isdir(os.path.join(d, m)) for m in _BENCH_MARKERS):
			return d
	return None


def find_wire_script(module_file: str) -> str | None:
	"""Resolve the wiring script from this module's own location.

	Walks up from ``flock_os/utils/`` to the repo root that contains
	``scripts/dev/wire-socketio-handler.sh``. Robust to symlinked app installs.
	"""
	for d in _walk_up(os.path.dirname(os.path.abspath(module_file))):
		cand = os.path.join(d, *WIRE_SCRIPT_REL)
		if os.path.isfile(cand):
			return cand
	return None


def rewire_socketio_handler() -> None:
	"""Frappe ``after_migrate`` / ``after_install`` hook: re-wire the join handler.

	Idempotent: the wire script is a no-op when the marker is already present,
	so this is cheap to run on every migrate. Best-effort: any failure is logged
	as a warning but never breaks ``migrate``/``install`` — the wiring can always
	be re-run by hand (``scripts/dev/wire-socketio-handler.sh``).
	"""
	import frappe

	logger = frappe.logger("flock_os")
	bench_root = find_bench_root(frappe.__file__)
	script = find_wire_script(__file__)
	if not bench_root:
		logger.warning(
			"flock_os realtime handler auto-wire could not locate the bench root; "
			"re-run scripts/dev/wire-socketio-handler.sh manually so the join handler "
			"is not silently dropped after a bench update.",
		)
		return
	if not script:
		logger.warning(
			"flock_os realtime handler auto-wire could not locate "
			"scripts/dev/wire-socketio-handler.sh; re-run it manually so the join "
			"handler is not silently dropped after a bench update.",
		)
		return
	try:
		result = subprocess.run(
			["bash", script, "--bench", bench_root],
			check=True,
			capture_output=True,
			text=True,
		)
		logger.info(
			"flock_os realtime handler wiring verified by %s against %s: %s",
			script,
			bench_root,
			(result.stdout or "").strip() or "ok",
		)
	except Exception as exc:  # noqa: BLE001 - never break migrate/install
		logger.warning(
			"flock_os realtime handler auto-wire failed (%s); re-run "
			"scripts/dev/wire-socketio-handler.sh manually so the join handler "
			"is not silently dropped.",
			exc,
		)
