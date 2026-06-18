"""
Project-level sanity tests for the flock_os app.

These run under plain ``pytest`` (no Frappe site / bench required) so the CI
lint+test gate stays fast and green. Frappe-level integration tests (DocTypes,
permissions, event emission) live alongside each DocType under
``flock_os/flock_os/doctype/`` and run locally via ``bench run-tests``.
"""

import re

import flock_os
import flock_os.hooks as hooks

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def test_app_version_is_semver():
	"""The package version follows a valid semver-ish shape."""
	assert isinstance(flock_os.__version__, str)
	assert _VERSION_RE.match(flock_os.__version__), (
		f"__version__ {flock_os.__version__!r} is not semver-shaped"
	)


def test_hooks_identity_is_consistent():
	"""hooks.py exposes the canonical app identity used everywhere."""
	assert hooks.app_name == "flock_os"
	assert hooks.app_title == "Flock OS"
	assert hooks.app_license == "MIT"
	assert hooks.app_version == flock_os.__version__


def test_default_module_matches_app_name():
	"""Domain DocTypes land under the flock_os module by convention."""
	assert hooks.default_app_module == "flock_os"
