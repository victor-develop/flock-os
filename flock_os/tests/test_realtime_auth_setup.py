"""
Realtime auth-cache auto-wiring harness (FLO-116 / FLO-14 / FLO-10 §8).

The §8 15k WS wall is the per-connection ``get_user_info`` HTTP callback in
vendored ``apps/frappe/realtime/middlewares/authenticate.js``. flock_os wraps it
with a sid-keyed cache (``realtime/middlewares/flock_auth_cache.js``) wired into
``apps/frappe/realtime/index.js`` by
``scripts/dev/wire-socketio-auth-cache.sh`` as a marker-guarded,
idempotent REPLACE of ``realtime.use(authenticate);``. A ``bench update``
rewrites ``index.js`` and silently restores the un-cached line — exactly the
wall recurring. flock_os's ``after_migrate`` / ``after_install`` hooks
(``rewire_socketio_auth_cache``) re-apply it automatically. These tests pin the
guard so a silent regression becomes a red gate instead (sibling of
``test_realtime_setup.py`` for the join handler):

1. The auth wire script round-trips on a temp bench: it REPLACES the anchor,
   is idempotent, a simulated reinstall drops it, and re-running restores it.
2. ``--check`` is a non-mutating assert (exit 0 wired / 1 absent).
3. The hook drives the real script end-to-end (frappe stubbed).
4. FLO-116 fail-loud: a missing marker after the wire attempt RAISES
   :class:`RealtimeWiringError` naming the regression + remediation.
5. The two wirings (join handler + auth cache) compose on a real Frappe v15
   index that carries both anchors.
6. Marker parity: ``realtime_setup.AUTH_WIRING_MARKER`` agrees with the bash
   ``MARK_START`` the script keys on.

Runs under plain ``pytest`` (no bench) on any host with ``bash``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from flock_os.utils import realtime_setup
from flock_os.utils.realtime_setup import (
	AUTH_WIRING_MARKER,
	RealtimeWiringError,
)

# The real auth wiring script, located the same way the hook locates it.
AUTH_WIRE_SCRIPT = realtime_setup.find_wire_script(
	realtime_setup.__file__, realtime_setup.AUTH_WIRE_SCRIPT_REL
)
assert AUTH_WIRE_SCRIPT, "wire-socketio-auth-cache.sh not found from module location"

# A pristine Frappe v15 realtime index carrying BOTH anchors the two flock_os
# wirings key on: `realtime.use(authenticate);` (auth cache) and
# `frappe_handlers(realtime, socket);` (join handler). Only these anchor lines
# matter to the wirings' awk.
PRISTINE_INDEX = (
	'const authenticate = require("./middlewares/authenticate");\n'
	"realtime.use(authenticate);\n"
	'const frappe_handlers = require("./handlers/frappe_handlers");\n'
	"function on_connection(socket) {\n"
	"\tfrappe_handlers(realtime, socket);\n"
	"}\n"
)


def _bench(tmp_path: Path) -> Path:
	"""A temp bench layout with a pristine frappe realtime index (both anchors)."""
	bench = tmp_path / "bench"
	(bench / "apps" / "frappe" / "realtime").mkdir(parents=True)
	(bench / "sites").mkdir(parents=True)
	(bench / "apps" / "frappe" / "realtime" / "index.js").write_text(PRISTINE_INDEX)
	return bench


def _wire(bench: Path, *flags: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		["bash", str(AUTH_WIRE_SCRIPT), "--bench", str(bench), *flags],
		capture_output=True,
		text=True,
	)


def _wired(index_path: Path) -> bool:
	return AUTH_WIRING_MARKER in index_path.read_text()


# --------------------------------------------------------------------------- #
# Wire-script behaviour (replace semantics, idempotent, self-healing)
# --------------------------------------------------------------------------- #


def test_find_wire_script_locates_auth_script():
	script = realtime_setup.find_wire_script(
		realtime_setup.__file__, realtime_setup.AUTH_WIRE_SCRIPT_REL
	)
	assert script and os.path.isfile(script)
	assert script.endswith(os.path.join("scripts", "dev", "wire-socketio-auth-cache.sh"))


def test_wire_script_replaces_anchor_round_trip_idempotent(tmp_path):
	"""Wire REPLACES the anchor; reinstall drops it; re-run restores it (FLO-116)."""
	bench = _bench(tmp_path)
	index = bench / "apps" / "frappe" / "realtime" / "index.js"

	# 1. Wire -> marker present, pristine anchor line gone (swapped for .wrap).
	assert _wire(bench).returncode == 0
	assert _wired(index)
	assert "realtime.use(authenticate);" not in index.read_text(), (
		"wiring must REPLACE the anchor, not append alongside it"
	)
	assert ".wrap(authenticate)" in index.read_text()

	# 2. Idempotent: wiring again is a no-op (still exactly one marker block).
	first = index.read_text()
	assert _wire(bench).returncode == 0
	assert index.read_text() == first
	assert first.count(AUTH_WIRING_MARKER) == 1

	# 3. Simulated `bench update`/reinstall rewrites index.js -> marker dropped,
	#    pristine anchor restored.
	index.write_text(PRISTINE_INDEX)
	assert not _wired(index), "fixture: reinstall should drop the wiring"
	assert "realtime.use(authenticate);" in index.read_text()

	# 4. Re-wire restores it — the auto-wire hook's job.
	assert _wire(bench).returncode == 0
	assert _wired(index)


def test_revert_restores_pristine_anchor_byte_identical(tmp_path):
	bench = _bench(tmp_path)
	index = bench / "apps" / "frappe" / "realtime" / "index.js"

	assert _wire(bench).returncode == 0
	assert _wired(index)

	assert _wire(bench, "--revert").returncode == 0
	assert not _wired(index), "marker must be gone after --revert"
	# The replaced line is restored exactly, so the file equals the pristine one.
	assert index.read_text() == PRISTINE_INDEX


def test_check_mode_reflects_wiring_state(tmp_path):
	bench = _bench(tmp_path)
	index = bench / "apps" / "frappe" / "realtime" / "index.js"

	# Absent -> --check exits 1.
	assert _wire(bench, "--check").returncode == 1

	# Present -> --check exits 0 and makes no change.
	assert _wire(bench).returncode == 0
	wired = index.read_text()
	assert _wire(bench, "--check").returncode == 0
	assert index.read_text() == wired


def test_check_and_revert_are_mutually_exclusive(tmp_path):
	bench = _bench(tmp_path)
	assert _wire(bench, "--check", "--revert").returncode == 2


# --------------------------------------------------------------------------- #
# The hook (frappe stubbed; real script + real module)
# --------------------------------------------------------------------------- #


def _stub_frappe(frappe_file: Path, sink: dict) -> SimpleNamespace:
	"""A frappe stub that records log calls; frappe.__file__ drives find_bench_root."""
	return SimpleNamespace(
		__file__=str(frappe_file),
		logger=lambda name: SimpleNamespace(
			info=lambda *a, **k: sink.setdefault("info", []).append(a),
			warning=lambda *a, **k: sink.setdefault("warning", []).append(a),
		),
	)


def test_rewire_auth_hook_drives_real_script_end_to_end(monkeypatch, tmp_path):
	bench = _bench(tmp_path)
	index = bench / "apps" / "frappe" / "realtime" / "index.js"
	frappe_file = bench / "apps" / "frappe" / "frappe" / "__init__.py"
	frappe_file.parent.mkdir(parents=True)
	frappe_file.write_text("")

	monkeypatch.setitem(sys.modules, "frappe", _stub_frappe(frappe_file, {}))

	realtime_setup.rewire_socketio_auth_cache()

	assert _wired(index), "hook should leave the auth cache wired"
	assert ".wrap(authenticate)" in index.read_text()


def test_rewire_auth_hook_short_circuits_when_already_wired(monkeypatch, tmp_path):
	"""An already-wired index must not invoke the script (no-op, no raise)."""
	bench = _bench(tmp_path)
	index = bench / "apps" / "frappe" / "realtime" / "index.js"
	frappe_file = bench / "apps" / "frappe" / "frappe" / "__init__.py"
	frappe_file.parent.mkdir(parents=True)
	frappe_file.write_text("")
	assert _wire(bench).returncode == 0
	assert _wired(index)

	calls = []
	sink: dict = {}
	monkeypatch.setitem(sys.modules, "frappe", _stub_frappe(frappe_file, sink))

	def _must_not_run(*a, **k):
		calls.append(k)
		raise AssertionError("subprocess.run must not be called when already wired")

	monkeypatch.setattr(realtime_setup.subprocess, "run", _must_not_run)

	realtime_setup.rewire_socketio_auth_cache()
	assert calls == [], "already-wired path must short-circuit before subprocess.run"
	assert sink.get("info"), "already-wired path should log info"


def test_rewire_auth_hook_raises_loud_when_wiring_still_missing(monkeypatch, tmp_path):
	"""FLO-116: a wire attempt that leaves the marker absent RAISES, never silent."""
	bench = _bench(tmp_path)
	frappe_file = bench / "apps" / "frappe" / "frappe" / "__init__.py"
	frappe_file.parent.mkdir(parents=True)
	frappe_file.write_text("")
	sink: dict = {}
	monkeypatch.setitem(sys.modules, "frappe", _stub_frappe(frappe_file, sink))

	def boom(cmd, **kwargs):
		raise FileNotFoundError(str(cmd[1]))

	monkeypatch.setattr(realtime_setup.subprocess, "run", boom)

	with pytest.raises(RealtimeWiringError) as exc_info:
		realtime_setup.rewire_socketio_auth_cache()

	message = str(exc_info.value)
	assert "MISSING" in message
	assert "FLO-116" in message  # names the regression it prevents
	assert "bench restart" in message  # concrete remediation
	assert sink.get("warning"), "the underlying script error should still be logged"


def test_rewire_auth_hook_warns_and_skips_when_index_absent(monkeypatch, tmp_path):
	"""No vendored realtime index -> best-effort warn+return (nothing to wire)."""
	bench = _bench(tmp_path)
	(bench / "apps" / "frappe" / "realtime" / "index.js").unlink()
	frappe_file = bench / "apps" / "frappe" / "frappe" / "__init__.py"
	frappe_file.parent.mkdir(parents=True)
	frappe_file.write_text("")
	sink: dict = {}
	calls = []
	monkeypatch.setitem(sys.modules, "frappe", _stub_frappe(frappe_file, sink))
	monkeypatch.setattr(realtime_setup.subprocess, "run", lambda *a, **k: calls.append(k))

	realtime_setup.rewire_socketio_auth_cache()  # must not raise

	assert sink.get("warning"), "missing index should warn"
	assert calls == [], "must not run the script when there is no index to wire"


# --------------------------------------------------------------------------- #
# Composition: join-handler wiring + auth-cache wiring coexist (real v15 index)
# --------------------------------------------------------------------------- #


def test_both_wirings_compose_on_real_frappe_index(monkeypatch, tmp_path):
	"""A real Frappe v15 index carries both anchors; both hooks wire cleanly."""
	bench = _bench(tmp_path)
	index = bench / "apps" / "frappe" / "realtime" / "index.js"
	frappe_file = bench / "apps" / "frappe" / "frappe" / "__init__.py"
	frappe_file.parent.mkdir(parents=True)
	frappe_file.write_text("")
	monkeypatch.setitem(sys.modules, "frappe", _stub_frappe(frappe_file, {}))

	realtime_setup.rewire_socketio_handler()
	realtime_setup.rewire_socketio_auth_cache()

	text = index.read_text()
	assert realtime_setup.WIRING_MARKER in text, "join handler wired"
	assert AUTH_WIRING_MARKER in text, "auth cache wired"
	assert ".wrap(authenticate)" in text
	assert text.count(realtime_setup.WIRING_MARKER) == 1
	assert text.count(AUTH_WIRING_MARKER) == 1


# --------------------------------------------------------------------------- #
# Marker parity: Python constant <-> bash MARK_START
# --------------------------------------------------------------------------- #


def test_python_auth_marker_matches_script_mark_start():
	"""The two sources of truth for "is the auth cache wired?" must agree.

	``realtime_setup.AUTH_WIRING_MARKER`` is what the hook verifies;
	``wire-socketio-auth-cache.sh`` keys on ``MARK_START``. If they drift the
	hook would raise forever or never detect a drop.
	"""
	script = Path(AUTH_WIRE_SCRIPT).read_text(encoding="utf-8")
	assert f'MARK_START="{AUTH_WIRING_MARKER}"' in script
