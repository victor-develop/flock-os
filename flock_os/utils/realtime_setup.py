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

FLO-110 closes the remaining gap: a best-effort hook that only *logs* on failure
still lets a dropped wiring ship silently (the path bug above hid a total
failure on the live bench for a whole heartbeat). So after driving the script
the hook now **verifies** the marker actually landed in ``index.js`` and, if it
is missing, raises :class:`RealtimeWiringError` so the ``bench migrate`` fails
loudly with a concrete remediation. A drop can never be silent again.

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

# The marker the wire script guards its injected block with. Single source of
# truth for "is the handler wired?": kept in lock-step with ``MARK_START`` in
# wire-socketio-handler.sh (pinned by the parity test).
WIRING_MARKER = "FLOCK_OS_REALTIME_HANDLER_START"

# FLO-116: the per-connection auth-cache wiring (clears the §8 15k WS wall).
# Same harness as the join handler, different anchor + script: the auth-cache
# wire script REPLACES ``realtime.use(authenticate);`` with a guarded
# ``.wrap(authenticate)`` swap. Kept as a parallel constant block so the two
# wirings are self-describing siblings (both re-applied by the hooks below).
AUTH_WIRE_SCRIPT_REL = ("scripts", "dev", "wire-socketio-auth-cache.sh")
AUTH_WIRING_MARKER = "FLOCK_OS_REALTIME_AUTH_CACHE_START"

# Path to the vendored Frappe realtime server, relative to the bench root.
_FRAPPE_REALTIME_INDEX = ("apps", "frappe", "realtime", "index.js")

# A bench root is a directory containing both of these siblings.
_BENCH_MARKERS = ("apps", "sites")


class RealtimeWiringError(RuntimeError):
	"""The realtime handler wiring is missing after a bench change (FLO-110).

	Raised from the ``after_migrate`` / ``after_install`` hook when the wire
	script ran (or was attempted) but the marker is still absent from
	``apps/frappe/realtime/index.js``. Failing the migrate is intentional: a
	silently broken realtime layer is exactly the FLO-107 regression this module
	exists to prevent.
	"""


def _marker_present(index_path: str, marker: str = WIRING_MARKER) -> bool:
	"""True iff ``index_path`` carries the given flock_os wiring marker.

	``marker`` defaults to the join-handler marker so existing callers are
	unchanged; the auth-cache hook passes :data:`AUTH_WIRING_MARKER`.
	"""
	try:
		with open(index_path, encoding="utf-8") as fh:
			return marker in fh.read()
	except OSError:
		return False


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


def find_wire_script(module_file: str, script_rel: tuple[str, ...] = WIRE_SCRIPT_REL) -> str | None:
	"""Resolve a wiring script from this module's own location.

	Walks up from ``flock_os/utils/`` to the repo root that contains the script
	(named by ``script_rel``). Defaults to the join-handler script for backward
	compatibility; the auth-cache hook passes :data:`AUTH_WIRE_SCRIPT_REL`.
	Robust to symlinked app installs.
	"""
	for d in _walk_up(os.path.dirname(os.path.abspath(module_file))):
		cand = os.path.join(d, *script_rel)
		if os.path.isfile(cand):
			return cand
	return None


def _rewire_realtime(
	*,
	script_rel: tuple[str, ...],
	marker: str,
	noun: str,
	script_name: str,
	consequence: str,
	regression: str,
) -> None:
	"""Shared ``after_migrate`` / ``after_install`` wiring driver.

	The join-handler (FLO-107/109/110) and auth-cache (FLO-116) wirings are the
	same mechanical flow — locate the bench + script, short-circuit when the
	marker is already present, drive the idempotent script, then FAIL LOUD
	(:class:`RealtimeWiringError`) if the marker is still absent. They differ
	only in script, marker, and the human words in the messages, so both hooks
	call into this one implementation (no copied logic). The only non-raising
	exits are when there is genuinely nothing to wire (bench root / script /
	realtime index not found — an unusual bench), which stay best-effort so this
	app never crashes a non-conformant install.
	"""
	import frappe

	logger = frappe.logger("flock_os")
	bench_root = find_bench_root(frappe.__file__)
	script = find_wire_script(__file__, script_rel)
	if not bench_root:
		logger.warning(
			"flock_os %s auto-wire could not locate the bench root; re-run "
			"scripts/dev/%s manually so it is not silently dropped after a bench "
			"update.",
			noun,
			script_name,
		)
		return
	if not script:
		logger.warning(
			"flock_os %s auto-wire could not locate scripts/dev/%s; re-run it "
			"manually so it is not silently dropped after a bench update.",
			noun,
			script_name,
		)
		return

	index_path = os.path.join(bench_root, *_FRAPPE_REALTIME_INDEX)
	if not os.path.isfile(index_path):
		# No vendored Frappe realtime server at all (very unusual bench) — nothing
		# to wire. Stay best-effort rather than crashing a non-conformant install.
		logger.warning(
			"flock_os realtime index.js not found at %s; auto-wire skipped.",
			index_path,
		)
		return

	# Already wired? Short-circuit before invoking the script (the script is
	# itself idempotent, but skipping the subprocess keeps migrate quiet on the
	# common no-op path). The verify below is the authoritative check either way.
	if _marker_present(index_path, marker):
		logger.info(
			"flock_os %s wiring already present in %s.",
			noun,
			index_path,
		)
		return

	detail = ""
	try:
		result = subprocess.run(
			["bash", script, "--bench", bench_root],
			check=True,
			capture_output=True,
			text=True,
		)
		detail = ((result.stdout or "") + (result.stderr or "")).strip()
	except Exception as exc:  # noqa: BLE001 - captured, then surfaced via verify
		# The script failed (non-zero exit, e.g. Frappe restructured the anchor,
		# or a transient error). Don't swallow it silently: fall through to the
		# verify, which raises if the marker did not land.
		logger.warning("flock_os %s auto-wire script error: %s", noun, exc)
		detail = str(exc)

	# The "can't silently drop it" guarantee. A migrate that succeeds while the
	# wiring is still absent is the silent regression; force a loud failure with
	# a concrete remediation instead.
	if not _marker_present(index_path, marker):
		raise RealtimeWiringError(
			f"flock_os {noun} wiring is MISSING after a bench update — "
			f"{consequence} ({regression} regression). Re-wire and restart, "
			f"then re-run `bench migrate`:\n"
			f"  bash {script} --bench {bench_root}\n"
			f"  bench restart\n"
			f"wire script output:\n{detail or '(no output)'}"
		)

	logger.info(
		"flock_os %s wiring verified by %s against %s: %s",
		noun,
		script,
		bench_root,
		detail or "ok",
	)


def rewire_socketio_handler() -> None:
	"""Frappe ``after_migrate`` / ``after_install`` hook: re-wire the join handler.

	Idempotent and fail-loud (FLO-109 / FLO-110). See :func:`_rewire_realtime`.
	"""
	_rewire_realtime(
		script_rel=WIRE_SCRIPT_REL,
		marker=WIRING_MARKER,
		noun="realtime handler",
		script_name="wire-socketio-handler.sh",
		consequence="the projector's broadcasts would silently reach no clients",
		regression="FLO-107",
	)


def rewire_socketio_auth_cache() -> None:
	"""Frappe ``after_migrate`` / ``after_install`` hook: re-wire the auth cache.

	FLO-116: a ``bench update`` rewrites ``apps/frappe/realtime/index.js`` and
	would silently restore the per-connection ``get_user_info`` HTTP callback,
	bringing back the §8 15k WS auth wall (connect p95 > 2 s,
	``flock_ws_receive_errors`` > 0). This re-applies the guarded
	``.wrap(authenticate)`` swap (idempotent + fail-loud, same harness as the
	join handler). Runbook: docs/development/ws-broadcast-delivery.md.
	"""
	_rewire_realtime(
		script_rel=AUTH_WIRE_SCRIPT_REL,
		marker=AUTH_WIRING_MARKER,
		noun="realtime auth-cache",
		script_name="wire-socketio-auth-cache.sh",
		consequence=("the §8 15k WS auth wall would recur (connect p95 > 2 s, flock_ws_receive_errors > 0)"),
		regression="FLO-116",
	)
