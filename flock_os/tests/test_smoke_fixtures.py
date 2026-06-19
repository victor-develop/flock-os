"""
Smoke-fixture seed-shape unit tests — FLO-112.

Pins the runtime fixture names the FLO-10 §8 WS gate resolves against
(``gathering-smoke`` -> ``branch-smoke`` + the scoped leader), so a rename or a
drift from ``load/config.js`` fails the gate instead of silently breaking the
smoke. The seeder itself (``flock_os.utils.smoke_fixtures``) is bench-only
(lazy Frappe), so only the pure constants from :mod:`flock_os.fixtures` are
asserted here; the bench-side idempotency is exercised via ``bench execute``.
"""

from __future__ import annotations

from flock_os import fixtures


class TestSmokeFixtureShape:
	def test_gathering_and_branch_match_load_config_defaults(self):
		# load/config.js: eventId="gathering-smoke", branchId="branch-smoke".
		assert fixtures.FLOCK_SMOKE_GATHERING == "gathering-smoke"
		assert fixtures.FLOCK_SMOKE_BRANCH == "branch-smoke"

	def test_leader_matches_load_config_default(self):
		# load/config.js: username="leader@flock.os", password="flock".
		assert fixtures.FLOCK_SMOKE_USER == "leader@flock.os"
		assert fixtures.FLOCK_SMOKE_USER_PASSWORD == "flock"

	def test_org_and_group_are_branch_bound_anchors(self):
		# The seed chain reuses the site's singleton org -> branch -> group(->branch)
		# -> gathering(->branch+group). Non-empty anchors so the resolver always
		# resolves a real branch + group (FLO-114: org is a label, not a row PK).
		assert fixtures.FLOCK_SMOKE_ORG_NAME
		assert fixtures.FLOCK_SMOKE_GROUP
		assert fixtures.FLOCK_SMOKE_GATHERING_TITLE

	def test_seed_names_are_distinct_rows(self):
		# The smoke-owned row PKs (the org is reused from the site singleton, so it
		# is not a smoke-owned PK — FLO-114). These four must stay distinct so the
		# resolver binds a real branch -> group -> gathering chain.
		names = {
			fixtures.FLOCK_SMOKE_BRANCH,
			fixtures.FLOCK_SMOKE_GROUP,
			fixtures.FLOCK_SMOKE_GATHERING,
			fixtures.FLOCK_SMOKE_USER,
		}
		assert len(names) == 4
