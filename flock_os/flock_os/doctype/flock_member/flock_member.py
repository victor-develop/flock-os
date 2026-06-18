# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document

from flock_os.flock_os import rules


class FlockMember(Document):
	# Flock Member = the person (FLO-5 §3.2). Membership != login: visitors /
	# pre-members are Members with a nullable `linked_user`. Attendance
	# (FLO-6/9) references Flock Member, never User. Unique on (email, branch)
	# so the same person may exist at two campuses (CEO-ratified, FLO-5 §8.3).

	def validate(self):
		self.full_name = rules.compute_member_full_name(first_name=self.first_name, last_name=self.last_name)
		rules.validate_member_status(self.status)
		self.organization = _branch_organization(self)
		_validate_email_branch_uniqueness(self)


def _branch_organization(doc: Document) -> str:
	# Denormalized tenant floor (ADR §3 contract): mirror the branch's org.
	if doc.organization or not doc.branch:
		return doc.organization
	return frappe.db.get_value("Flock Branch", doc.branch, "organization") or doc.organization


def _validate_email_branch_uniqueness(doc: Document) -> None:
	# (email, branch) composite uniqueness (FLO-5 §3.2/§8.3). Same email may
	# recur across branches, never within one. The composite-key decision is
	# pure (rules.is_duplicate_pair); the row set is fetched via the framework.
	if not doc.email:
		return
	existing = frappe.db.get_all(
		"Flock Member",
		filters={"email": doc.email, "branch": doc.branch, "name": ["!=", doc.name or ""]},
		pluck="name",
	)
	pairs = [(doc.email, doc.branch)] * len(existing)
	if rules.is_duplicate_pair((doc.email, doc.branch), pairs):
		frappe.throw(
			f"A Flock Member with email {doc.email!r} already exists in branch {doc.branch!r}.",
			frappe.ValidationError,
		)
