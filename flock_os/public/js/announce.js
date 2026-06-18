/*
 * Flock OS — admin announcement composer client (FLO-60 / FLO-8 §8).
 *
 * Mobile-first interactivity for the /announce portal page. The scope <select>s
 * are populated server-side from the admin's targetable subtree; this script
 * only filters the group picker by the chosen branch and drives the three
 * mutating actions through the FLO-94 REST controller:
 *
 *   preview_audience      -> "Reaches N branches" delivery-target preview
 *   publish_announcement  -> Draft -> Published + scoped fan-out (send now)
 *   schedule_announcement -> Draft -> Scheduled (sends later via the scheduler)
 *
 * Business logic stays server-side: every call is re-validated by the backend
 * (source of truth). The client never trusts itself for enforcement — it only
 * renders the offered scope and shows clear loading / success / error states.
 */
(function () {
	"use strict";

	const FORM = document.getElementById("flock-compose-form");
	if (!FORM) return;

	const $ = (id) => document.getElementById(id);
	const subject = $("flock-subject");
	const body = $("flock-body");
	const branch = $("flock-branch");
	const group = $("flock-group");
	const groupOptions = group ? Array.from(group.querySelectorAll("option[data-branch]")) : [];
	const scheduledAt = $("flock-scheduled-at");
	const previewBtn = $("flock-preview-btn");
	const scheduleBtn = $("flock-schedule-btn");
	const sendBtn = $("flock-send-btn");
	const preview = $("flock-preview");
	const status = $("flock-status");

	const ENDPOINTS = {
		preview: FORM.dataset.endpointPreview,
		publish: FORM.dataset.endpointPublish,
		schedule: FORM.dataset.endpointSchedule,
		insert: FORM.dataset.endpointInsert,
	};
	const ORG = FORM.dataset.organization;

	// ---- state helpers ------------------------------------------------------ //
	function valid() {
		return Boolean(subject.value.trim() && body.value.trim() && branch.value);
	}

	function refreshButtons() {
		const ok = valid();
		sendBtn.disabled = !ok;
		previewBtn.disabled = !branch.value;
		scheduleBtn.disabled = !(ok && scheduledAt.value);
	}

	function busy(on) {
		[previewBtn, sendBtn, scheduleBtn].forEach((b) => (b.disabled = on || !valid()));
		status.className = on ? "flock-status flock-status--loading" : "flock-status";
		if (on) status.innerHTML = '<span class="flock-spinner" aria-hidden="true"></span> Working…';
	}

	function setStatus(kind, html) {
		status.className = "flock-status flock-status--" + kind;
		status.innerHTML = html;
	}

	function clearStatus() {
		status.className = "flock-status";
		status.innerHTML = "";
	}

	// ---- group picker filter (scoped to the chosen branch) ------------------ //
	function filterGroups() {
		if (!group) return;
		const chosen = branch.value;
		let any = false;
		groupOptions.forEach((opt) => {
			const match = opt.dataset.branch === chosen;
			opt.hidden = !match;
			if (match) any = true;
		});
		group.disabled = !any;
		group.value = "";
		// keep the "all groups in subtree" placeholder first.
	}

	// ---- audience preview (delivery target) --------------------------------- //
	function previewAudience() {
		if (!branch.value) return;
		busy(true);
		frappe.call({
			method: ENDPOINTS.preview,
			args: { organization: ORG, branch: branch.value, group: group.value || null },
			callback: (r) => {
				busy(false);
				if (r.exc) {
					setStatus("error", "Could not preview audience. " + (r.exc || ""));
					return;
				}
				const res = r.message || {};
				const count = res.branch_count || 0;
				const branches = res.branches || [];
				preview.hidden = false;
				preview.innerHTML =
					'<div class="flock-preview__count">' +
					"<strong>" + count + "</strong> " + (count === 1 ? "branch" : "branches") +
					" will receive this</div>" +
					(count ? '<ul class="flock-preview__list">' +
						branches.map((b) => "<li>" + frappe.utils.escape_html(b) + "</li>").join("") +
						"</ul>" : "<p class='text-muted'>No recipients in this subtree yet.</p>");
			},
		});
	}

	// ---- send / schedule ---------------------------------------------------- //
	function buildDoc(statusValue) {
		const channels = Array.from(FORM.querySelectorAll('input[name="channel"]:checked'))
			.map((c) => ({ doctype: "Flock Announcement Channel", channel: c.value }));
		return {
			doctype: "Flock Announcement",
			organization: ORG,
			branch: branch.value,
			group: group.value || undefined,
			subject: subject.value.trim(),
			body: body.value.trim(),
			category: $("flock-category").value,
			priority: $("flock-priority").value,
			audience_role: $("flock-audience-role").value,
			channels: channels,
			scheduled_at: scheduledAt.value || undefined,
			status: statusValue,
		};
	}

	function sendNow(ev) {
		ev.preventDefault();
		if (!valid()) return;
		busy(true);
		frappe.call({
			method: ENDPOINTS.insert,
			args: { doc: buildDoc("Draft") },
			callback: (r) => {
				if (r.exc || !r.message) {
					busy(false);
					setStatus("error", "Could not save announcement. " + (r.exc || ""));
					return;
				}
				publish(r.message.name);
			},
		});
	}

	function publish(name) {
		frappe.call({
			method: ENDPOINTS.publish,
			args: { name: name },
			callback: (r) => {
				busy(false);
				if (r.exc) {
					setStatus("error", "Send failed. " + (r.exc || ""));
					return;
				}
				const res = r.message || {};
				setStatus(
					"success",
					"Sent — reached <strong>" + (res.audience_branch_count || 0) +
					"</strong> branches. Ref <code>" + frappe.utils.escape_html(res.notification_ref || name) + "</code>"
				);
				resetForm();
			},
		});
	}

	function schedule(ev) {
		ev.preventDefault();
		if (!valid() || !scheduledAt.value) return;
		busy(true);
		frappe.call({
			method: ENDPOINTS.insert,
			args: { doc: buildDoc("Draft") },
			callback: (r) => {
				if (r.exc || !r.message) {
					busy(false);
					setStatus("error", "Could not save announcement. " + (r.exc || ""));
					return;
				}
				frappe.call({
					method: ENDPOINTS.schedule,
					args: { name: r.message.name },
					callback: (rr) => {
						busy(false);
						if (rr.exc) {
							setStatus("error", "Scheduling failed. " + (rr.exc || ""));
							return;
						}
						setStatus("success", "Scheduled for <strong>" +
							frappe.utils.escape_html((rr.message || {}).scheduled_at || "") + "</strong>.");
						resetForm();
					},
				});
			},
		});
	}

	function resetForm() {
		FORM.reset();
		filterGroups();
		preview.hidden = true;
		preview.innerHTML = "";
		refreshButtons();
	}

	// ---- wire up ------------------------------------------------------------ //
	[subject, body].forEach((el) => el.addEventListener("input", () => { refreshButtons(); clearStatus(); }));
	branch.addEventListener("change", () => { filterGroups(); refreshButtons(); clearStatus(); });
	scheduledAt.addEventListener("change", refreshButtons);
	previewBtn.addEventListener("click", previewAudience);
	sendBtn.addEventListener("click", sendNow);
	scheduleBtn.addEventListener("click", schedule);
	FORM.addEventListener("submit", sendNow);

	filterGroups();
	refreshButtons();
})();
