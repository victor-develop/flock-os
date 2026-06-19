/*
 * Flock OS — Fun Attendance facilitator console (FLO-12 / FLO-9 §12, §6.8).
 *
 * Drives the `/engage-host` form: scope picker (branch → group/gathering),
 * session config, create / open / close, share (QR + 6-digit code + deep link),
 * the live Redis headcount (via the realtime broadcast channel), the suspect-
 * pattern review queue, and the audit-logged manual override.
 *
 * Business logic stays server-side: every mutating call goes to
 * `flock_os.engagement_views.*` (FLO-11), which re-validates scope (source of
 * truth) and emits the canonical events. The client only renders offered scope
 * + clear loading/empty/error states.
 */
(function () {
	"use strict";

	const FORM = document.getElementById("flock-host-form");
	if (!FORM) return;

	const _ = (window.frappe && window.frappe._ && ((s) => window.frappe._(s))) || ((s) => s);
	const $ = (id) => document.getElementById(id);
	const el = (tag, cls, text) => {
		const n = document.createElement(tag);
		if (cls) n.className = cls;
		if (text != null) n.textContent = text;
		return n;
	};

	const ENDPOINTS = JSON.parse(FORM.dataset.endpoints || "{}");
	const KINDS = JSON.parse(FORM.dataset.kinds || "[]");
	const PARITY = JSON.parse(FORM.dataset.parity || "{}");
	const GROUPS_BY_BRANCH = JSON.parse(FORM.dataset.groupsByBranch || "{}");
	const GATHERINGS_BY_BRANCH = JSON.parse(FORM.dataset.gatheringsByBranch || "{}");
	const ORG = FORM.dataset.organization;

	const BRANCH = $("flock-host-branch");
	const GROUP = $("flock-host-group");
	const GATHERING = $("flock-host-gathering");
	const KIND = $("flock-host-kind");
	const ROUNDS = $("flock-host-rounds");
	const TITLE = $("flock-host-title");
	const CALM = $("flock-host-calm");
	const SHARE = $("flock-host-share");
	const CODE = $("flock-host-code");
	const QR = $("flock-host-qr");
	const DEEPLINK = $("flock-host-deeplink");
	const LIVE = $("flock-host-live");
	const COUNT = $("flock-host-count");
	const STATE = $("flock-host-state");
	const REVIEW = $("flock-host-review");
	const QUEUE = $("flock-host-queue");
	const STATUS = $("flock-host-status");
	const BTN_CREATE = $("flock-host-create");
	const BTN_OPEN = $("flock-host-open");
	const BTN_CLOSE = $("flock-host-close");

	let currentSession = null; // { name, status, room_code, ... }

	// ---- kind picker (rendered from the catalog) -------------------------- //
	KINDS.forEach((k) => {
		const opt = el("option");
		opt.value = k.kind;
		opt.textContent = _(k.i18n_key) + " (" + _(k.family) + ")";
		KIND.appendChild(opt);
	});

	// ---- status helper ---------------------------------------------------- //
	function setStatus(kind, html) {
		if (!STATUS) return;
		STATUS.className = "flock-status flock-status--" + kind;
		STATUS.innerHTML = html;
	}
	function busy(on) {
		[BTN_CREATE, BTN_OPEN, BTN_CLOSE].forEach((b) => (b.disabled = on || !b.dataset.ready));
		if (on) setStatus("loading", '<span class="flock-spinner" aria-hidden="true"></span> ' + _("Working…"));
	}

	// ---- frappe.call wrapper ---------------------------------------------- //
	function call(method, args) {
		return new Promise((resolve, reject) => {
			if (!method || !window.frappe || !window.frappe.call) {
				reject(new Error(_("Realtime backend not available.")));
				return;
			}
			window.frappe.call({
				method: method,
				args: args,
				callback: (r) => {
					if (r && r.exc) reject(new Error(String(r.exc)));
					else resolve(r && r.message);
				},
			});
		});
	}

	// ---- scope pickers (branch → group / gathering) ----------------------- //
	function fillGroupPicker() {
		const chosen = BRANCH.value;
		// Keep the "all groups" placeholder; append the branch's groups.
		Array.from(GROUP.querySelectorAll("option[data-branch]")).forEach((o) => o.remove());
		(GROUPS_BY_BRANCH[chosen] || []).forEach((g) => {
			const opt = el("option");
			opt.value = g.name;
			opt.textContent = g.label;
			opt.dataset.branch = chosen;
			GROUP.appendChild(opt);
		});
		GROUP.disabled = !(GROUPS_BY_BRANCH[chosen] || []).length;
	}

	function fillGatheringPicker() {
		const chosen = BRANCH.value;
		GATHERING.innerHTML = "";
		const list = GATHERINGS_BY_BRANCH[chosen] || [];
		const placeholder = el("option");
		placeholder.value = "";
		placeholder.disabled = true;
		placeholder.selected = true;
		placeholder.textContent = list.length ? _("Select a gathering…") : _("No hostable gatherings in this branch");
		GATHERING.appendChild(placeholder);
		list.forEach((g) => {
			const opt = el("option");
			opt.value = g.name;
			opt.textContent = g.label;
			GATHERING.appendChild(opt);
		});
		GATHERING.disabled = !list.length;
		refreshCreate();
	}

	function refreshCreate() {
		const ready = Boolean(BRANCH.value && GATHERING.value);
		BTN_CREATE.dataset.ready = ready ? "1" : "";
		BTN_CREATE.disabled = !ready || currentSession;
	}

	BRANCH.addEventListener("change", () => { fillGroupPicker(); fillGatheringPicker(); refreshCreate(); });
	GATHERING.addEventListener("change", refreshCreate);

	// ---- create / open / close ------------------------------------------- //
	async function createSession() {
		busy(true);
		try {
			const res = await call(ENDPOINTS.create_session, {
				organization: ORG,
				branch: BRANCH.value,
				group: GROUP.value || null,
				gathering: GATHERING.value,
				engagement_kind: KIND.value,
				rounds: Number(ROUNDS.value) || 1,
				title: (TITLE.value || "").trim() || null,
				calm_default: CALM.checked,
			});
			currentSession = res;
			renderShare(res);
			subscribeLive(res);
			setStatus("success", _("Session created. Share the code, then open it live."));
			BTN_CREATE.disabled = true;
			BTN_OPEN.dataset.ready = "1";
			BTN_OPEN.disabled = false;
		} catch (e) {
			setStatus("error", _("Could not create session. ") + ((e && e.message) || ""));
		} finally {
			busy(false);
		}
	}

	async function openSession() {
		if (!currentSession) return;
		busy(true);
		try {
			const res = await call(ENDPOINTS.open_session, { name: currentSession.name });
			currentSession.status = res.status || "open";
			setState(res.status || "open");
			setStatus("success", _("Live now — players can check in."));
			BTN_OPEN.disabled = true;
			BTN_CLOSE.dataset.ready = "1";
			BTN_CLOSE.disabled = false;
		} catch (e) {
			setStatus("error", _("Could not open. ") + ((e && e.message) || ""));
		} finally {
			busy(false);
		}
	}

	async function closeSession() {
		if (!currentSession) return;
		if (!confirm(_("Close the session and record attendance?"))) return;
		busy(true);
		try {
			const res = await call(ENDPOINTS.close_session, { name: currentSession.name });
			setState("closed");
			const n = (res && res.count) != null ? res.count : null;
			setStatus("success",
				_("Closed. Recorded {0} attendees.").replace("{0}", n != null ? n : "—"));
			BTN_CLOSE.disabled = true;
			renderReview();
		} catch (e) {
			setStatus("error", _("Could not close. ") + ((e && e.message) || ""));
		} finally {
			busy(false);
		}
	}

	BTN_CREATE.addEventListener("click", createSession);
	BTN_OPEN.addEventListener("click", openSession);
	BTN_CLOSE.addEventListener("click", closeSession);

	// ---- share (QR + room code + deep link) ------------------------------- //
	function renderShare(res) {
		if (!SHARE) return;
		SHARE.hidden = false;
		if (CODE) CODE.textContent = (res.room_code || "——").toString();
		if (DEEPLINK) {
			const url = res.player_url || ("/engage?session=" + encodeURIComponent(res.name));
			DEEPLINK.href = url;
			DEEPLINK.textContent = url;
		}
		if (QR && res.qr_url) {
			QR.style.backgroundImage = "url('" + res.qr_url + "')";
		} else if (QR) {
			// Lightweight fallback QR via a public chart endpoint is backend-owned;
			// until FLO-11 ships qr_url, show the room code large (still usable).
			QR.textContent = (res.room_code || "").toString();
		}
	}

	// ---- live headcount (realtime broadcast channel) ---------------------- //
	function subscribeLive(res) {
		if (!LIVE) return;
		LIVE.hidden = false;
		setState(res.status || "draft");
		const ev = PARITY.realtime_events;
		const frappeRT = window.frappe && window.frappe.realtime;
		if (!ev || !frappeRT) return;
		try {
			if (ev.attendance_count) frappeRT.on(ev.attendance_count, (msg) => {
				if (msg && typeof msg.count === "number" && COUNT) COUNT.textContent = String(msg.count);
			});
			if (ev.game_state) frappeRT.on(ev.game_state, (msg) => {
				if (msg && msg.state) setState(msg.state);
				if (msg && typeof msg.count === "number" && COUNT) COUNT.textContent = String(msg.count);
			});
		} catch (_e) { /* realtime is best-effort */ }
	}

	function setState(state) {
		const map = {
			draft: _("Draft — not open yet"),
			open: _("Live — accepting check-ins"),
			closing: _("Closing — grace window"),
			closed: _("Closed — attendance recorded"),
		};
		if (STATE) STATE.textContent = map[state] || state;
		if (LIVE) LIVE.dataset.state = state;
	}

	// ---- suspect-pattern review queue + manual override -------------------- //
	async function renderReview() {
		if (!REVIEW || !currentSession) return;
		try {
			const res = await call(ENDPOINTS.review_queue, { name: currentSession.name });
			const items = (res && res.items) || [];
			REVIEW.hidden = !items.length;
			if (!QUEUE) return;
			QUEUE.innerHTML = "";
			items.forEach((it) => QUEUE.appendChild(reviewRow(it)));
		} catch (_e) {
			REVIEW.hidden = true;
		}
	}

	function reviewRow(item) {
		const row = el("div", "flock-host__queue-row");
		const reason = el("div", "flock-host__queue-reason", item.reason || _("Flagged pattern"));
		const who = el("div", "flock-host__queue-who",
			(item.attendee_display_name || _("Anonymous")) + " · " + (item.flag || ""));
		row.appendChild(reason);
		row.appendChild(who);
		const acts = el("div", "flock-host__queue-acts");
		const credit = el("button", "btn btn-secondary btn-sm");
		credit.type = "button";
		credit.textContent = _("Keep");
		credit.style.minHeight = "40px";
		credit.addEventListener("click", () => override(item, "keep"));
		const revoke = el("button", "btn btn-default btn-sm");
		revoke.type = "button";
		revoke.textContent = _("Revoke");
		revoke.style.minHeight = "40px";
		revoke.addEventListener("click", () => override(item, "revoke"));
		acts.appendChild(credit);
		acts.appendChild(revoke);
		row.appendChild(acts);
		return row;
	}

	async function override(item, action) {
		const reason = prompt(_("Reason for {0} (audit-logged):").replace("{0}", action), "");
		if (reason === null) return;
		busy(true);
		try {
			await call(ENDPOINTS.manual_override, {
				name: currentSession.name,
				attendee_key: item.attendee_key,
				action: action,
				reason: reason || _("Facilitator review"),
			});
			setStatus("success", _("Override recorded."));
			await renderReview();
		} catch (e) {
			setStatus("error", _("Override failed. ") + ((e && e.message) || ""));
		} finally {
			busy(false);
		}
	}

	// ---- init ------------------------------------------------------------- //
	fillGroupPicker();
	fillGatheringPicker();
	refreshCreate();
})();
