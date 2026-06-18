# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class FlockBranch(Document):
	# Flock Branch = the administrative org tree (is_tree=1). This node IS the
	# branch scoping axis (FLO-5 §3.1/§3.4). Frappe manages the nested set
	# (lft/rgt) + parent_branch adjacency natively; structural moves go through
	# flock_os.trees.move_branch (FLO-19/20).
	pass
