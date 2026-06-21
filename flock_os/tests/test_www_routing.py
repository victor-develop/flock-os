"""
Routing smoke test for the Fun Attendance + announcement www pages (FLO-615).

Pins the P0-1 finding from the [FLO-610 audit](/FLO/issues/FLO-610): the four
share/QR entry-point routes (``/engage``, ``/engage-host``, ``/engage-templates``,
``/announce``) 404'd because the ``www`` folder lived at ``flock_os/flock_os/www``
(the hexagonal *module* dir) instead of ``flock_os/www`` — the *app package* dir
that ``frappe.website.router.get_pages()`` walks (``get_app_path("flock_os") +
"/www"``). This test holds that contract so the nesting cannot regress.

Runs under plain ``pytest`` (no Frappe site / bench), mirroring every other
project-level test. Frappe's website router resolves a www page by filename
(``/engage`` <- ``www/engage.html`` + ``www/engage.py`` controller), so this test
asserts the structural facts the router depends on:

* the ``www`` directory is a direct child of the app package dir the router
  walks (computed exactly as ``get_app_path`` does), and **not** nested a level
  deeper;
* every documented route resolves to a template + a controller exposing
  ``get_context``;
* the controllers are syntactically valid Python (they ``import frappe`` so they
  can't be imported here — AST is the proxy);
* ``/engage`` issues the documented guest -> login redirect (the attendee entry
  point must not hard-404 a signed-out visitor).

The live 200/redirect assertions belong to ``bench run-tests`` (the test_client
GETs); this is the SQL-light regression guard that the routes are discoverable
at all — the exact layer the bug hid in.
"""

from __future__ import annotations

import ast
import os
import pathlib

import flock_os

# --------------------------------------------------------------------------- #
# The four attendee/admin entry-point routes (FLO-610 audit P0-1).
# Each route resolves to ``<name>.html`` + ``<name>.py`` in the www dir.
# --------------------------------------------------------------------------- #
ROUTES = ("engage", "engage-host", "engage-templates", "announce")

# The player page is the documented guest entry point: a signed-out visitor is
# bounced to login with a return-to so they land back on the room (FLO-9 §2).
GUEST_REDIRECT_ROUTE = "engage"


def _app_package_dir() -> pathlib.Path:
	"""Mirror ``frappe.get_app_path("flock_os")`` without importing frappe.

	``get_app_path`` is ``os.path.dirname(get_module(app).__file__)``: the
	directory holding the top-level package's ``__init__.py``. The website router
	appends ``"/www"`` to exactly this path when collecting www pages.
	"""
	return pathlib.Path(os.path.dirname(os.path.abspath(flock_os.__file__)))


def _router_www_dir() -> pathlib.Path:
	"""The directory Frappe's website router actually walks for www pages."""
	return _app_package_dir() / "www"


def _nested_module_www_dir() -> pathlib.Path:
	"""The wrongly-nested location the bug put the pages in (FLO-610 P0-1)."""
	return _app_package_dir() / "flock_os" / "www"


# --------------------------------------------------------------------------- #
# Location: www is router-walkable + not nested a level too deep.
# --------------------------------------------------------------------------- #
def test_www_dir_lives_where_the_router_walks_it():
	"""The ``www`` folder is a direct child of the app package dir."""
	assert _router_www_dir().is_dir(), (
		f"www must live at {_router_www_dir()} (where get_app_path('flock_os')"
		" + '/www' resolves); it is missing — every www route will 404 (FLO-610 P0-1)."
	)


def test_www_is_not_nested_in_the_module_dir():
	"""Regression guard: the pages must not sit a level deeper in the module dir.

	This is the exact nesting that caused the FLO-610 P0-1 outage — the router
	walked ``flock_os/www`` while the pages hid in ``flock_os/flock_os/www``.
	"""
	assert not _nested_module_www_dir().exists(), (
		f"stray nested www found at {_nested_module_www_dir()} — Frappe's router"
		" does not walk the module dir; relocate the pages to flock_os/www (FLO-610 P0-1)."
	)


# --------------------------------------------------------------------------- #
# Route -> file resolution (filename = path segment for www pages).
# --------------------------------------------------------------------------- #
def test_every_documented_route_resolves_to_template_and_controller():
	"""Each entry-point route maps to a ``.html`` template + a ``.py`` controller."""
	www = _router_www_dir()
	missing = []
	for route in ROUTES:
		for ext in (".html", ".py"):
			page = www / f"{route}{ext}"
			if not page.is_file():
				missing.append(str(page))
	assert not missing, "missing www page files (route would 404): " + ", ".join(missing)


def test_no_other_orphan_www_pages_leak_into_routing():
	"""Only the four documented routes should be router-discoverable.

	A stray file under www/ would silently publish an unintended route, so this
	keeps the entry-point surface tight (HTML + PY pairs only).
	"""
	www = _router_www_dir()
	expected = {f"{route}{ext}" for route in ROUTES for ext in (".html", ".py")}
	actual = {p.name for p in www.iterdir() if p.is_file()}
	assert actual == expected, (
		f"unexpected www surface: extra={actual - expected}, missing={expected - actual}"
	)


# --------------------------------------------------------------------------- #
# Controller contract: valid Python + exposes get_context (AST proxy, no bench).
# --------------------------------------------------------------------------- #
def test_controllers_are_syntactically_valid_and_expose_get_context():
	"""Every controller compiles and defines the ``get_context(context)`` entrypoint."""
	for route in ROUTES:
		source = (_router_www_dir() / f"{route}.py").read_text()
		tree = ast.parse(source, filename=f"{route}.py")
		# ast.walk covers module-level defs regardless of precise arg shape.
		names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
		assert "get_context" in names, f"{route}.py controller is missing get_context(context)"
		# Sanity: the function takes exactly one positional arg (the Frappe context).
		ctx_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "get_context")
		assert len(ctx_fn.args.args) == 1, f"{route}.py get_context must take exactly one arg (context)"


def test_engage_controller_redirects_guests_to_login():
	"""``/engage`` is the attendee entry point — a guest must be bounced to login.

	The documented behaviour (FLO-9 §2): a signed-out visitor following a share /
	QR link is sent to ``/login`` with a ``redirect-to=/engage`` round-trip so the
	backend can mint the session ticket. This must never hard-404 a guest.
	"""
	engage_py = (_router_www_dir() / "engage.py").read_text()
	tree = ast.parse(engage_py, filename="engage.py")

	# The controller compares against the Frappe sentinel "Guest" ...
	has_guest_branch = any(
		isinstance(test, ast.Compare)
		and isinstance(test.left, ast.Name)
		and test.left.id == "user"
		and any(isinstance(c, ast.Constant) and c.value == "Guest" for c in test.comparators)
		for test in ast.walk(tree)
	)
	assert has_guest_branch, "engage.py has no `user == 'Guest'` branch — guest redirect regressed"

	# ... and bounces them to /login with a return-to the engage room.
	string_consts = {
		node.value
		for node in ast.walk(tree)
		if isinstance(node, ast.Constant) and isinstance(node.value, str)
	}
	assert any(s.startswith("/login") for s in string_consts), (
		"engage.py guest branch does not redirect to /login (documented guest entry point)"
	)
	assert any("redirect-to=/engage" in s for s in string_consts), (
		"engage.py must round-trip `redirect-to=/engage` so a guest lands back on the room"
	)


def test_facilitator_and_announce_pages_guard_guests():
	"""The facilitator + announce pages bounce guests to login (admin-only surface)."""
	for route in ("engage-host", "engage-templates", "announce"):
		tree = ast.parse((_router_www_dir() / f"{route}.py").read_text(), filename=f"{route}.py")
		string_consts = {
			node.value
			for node in ast.walk(tree)
			if isinstance(node, ast.Constant) and isinstance(node.value, str)
		}
		assert any(s.startswith("/login") for s in string_consts), (
			f"{route}.py does not redirect guests to /login (admin-only entry point)"
		)
