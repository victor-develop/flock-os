/*
 * Flock OS — Fun Attendance player experience (FLO-12 / FLO-9 §12, §3, §7).
 *
 * Wires the `/engage` shell to `FlockEngageCore.useEngagementSession`:
 *
 *   - a11y profile (persisted in localStorage): reduced motion, high contrast,
 *     colorblind-safe palette, captions, haptics, + the Calm Check-in no-time-
 *     pressure path for every timed game (WCAG 2.1 AA, FLO-9 §7),
 *   - <EngagementStage>: joins the session, subscribes, manages the offline
 *     queue, and routes to the per-kind component exposing
 *     `participate(kind, payload)` (FLO-9 §12),
 *   - starter components for all 9 kinds — each with an accessibility-mode
 *     variant. Components render prompts via `frappe._` so every label is i18n'd,
 *     and never trust themselves for attendance (the backend owns the clock +
 *     validators, §6).
 *
 * Loaded after engagement-core.js. All text goes through `_()` so it is
 * translated with the player's language (FLO-9 §7 i18n).
 */
(function () {
	"use strict";

	const ROOT = document.getElementById("flock-engage-root");
	if (!ROOT) return;

	const _ = (window.frappe && window.frappe._ && ((s) => window.frappe._(s))) || ((s) => s);
	const $ = (id) => document.getElementById(id);
	const el = (tag, cls, text) => {
		const n = document.createElement(tag);
		if (cls) n.className = cls;
		if (text != null) n.textContent = text;
		return n;
	};

	const ENDPOINTS = JSON.parse(ROOT.dataset.endpoints || "{}");
	const KINDS = JSON.parse(ROOT.dataset.kinds || "[]");
	const PARITY = JSON.parse(ROOT.dataset.parity || "{}");
	const A11Y_DEFAULTS = JSON.parse(ROOT.dataset.a11yDefaults || "{}");
	const A11Y_KEY = ROOT.dataset.a11yPrefKey || "flock:engage:a11y";
	const MIN_TARGET = parseInt(ROOT.dataset.minTargetPx || "48", 10);

	const JOIN_VIEW = $("flock-engage-join");
	const JOIN_FORM = $("flock-engage-join-form");
	const ROOM_INPUT = $("flock-engage-room");
	const JOIN_STATUS = $("flock-engage-join-status");
	const STAGE = $("flock-engage-stage");
	const TOAST = $("flock-engage-toast");
	const CONN = $("flock-engage-conn");
	const CONN_LABEL = $("flock-engage-conn-label");
	const A11Y_TOGGLE = $("flock-engage-a11y-toggle");
	const A11Y_PANEL = $("flock-engage-a11y-panel");

	let session = null;
	let currentKind = null;
	let calmCheckin = false;

	// ---- accessibility profile (persisted, applied before first paint) ---- //
	function loadA11y() {
		let saved = {};
		try { saved = JSON.parse(localStorage.getItem(A11Y_KEY) || "{}"); } catch (_e) { saved = {}; }
		const merged = Object.assign({}, A11Y_DEFAULTS, saved);
		// Respect the OS reduced-motion preference on first run.
		if (saved.reduced_motion === undefined && window.matchMedia) {
			merged.reduced_motion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
		}
		return merged;
	}

	let a11y = loadA11y();

	function applyA11y() {
		document.documentElement.classList.toggle("flock-a11y", a11y.enabled);
		document.documentElement.classList.toggle("flock-reduced-motion", a11y.reduced_motion);
		document.documentElement.classList.toggle("flock-high-contrast", a11y.high_contrast);
		document.documentElement.classList.toggle("flock-colorblind", a11y.colorblind_safe);
		if (A11Y_TOGGLE) A11Y_TOGGLE.setAttribute("aria-pressed", String(a11y.enabled));
		Array.prototype.forEach.call(
			document.querySelectorAll("[data-a11y]"),
			(box) => {
				const key = box.dataset.a11y;
				box.checked = Boolean(a11y[key]);
			}
		);
	}

	function saveA11y() {
		try { localStorage.setItem(A11Y_KEY, JSON.stringify(a11y)); } catch (_e) { /* storage may be blocked */ }
		applyA11y();
	}

	function haptic(ms) {
		if (a11y.haptics && window.navigator && navigator.vibrate) {
			try { navigator.vibrate(ms || 12); } catch (_e) { /* noop */ }
		}
	}

	function caption(node, text) {
		if (!a11y.captions || !text) return;
		const c = el("div", "flock-caption", text);
		c.setAttribute("role", "status");
		node.appendChild(c);
	}

	applyA11y();

	if (A11Y_TOGGLE) {
		A11Y_TOGGLE.addEventListener("click", () => {
			A11Y_PANEL.hidden = !A11Y_PANEL.hidden;
		});
	}
	Array.prototype.forEach.call(document.querySelectorAll("[data-a11y]"), (box) => {
		box.addEventListener("change", () => {
			a11y[box.dataset.a11y] = box.checked;
			if (box.dataset.a11y === "enabled" && box.checked) {
				// Enabling accessibility mode turns on the calm path by default.
				calmCheckin = true;
			}
			saveA11y();
		});
	});

	// ---- toast + connection chrome ---------------------------------------- //
	let toastTimer = null;
	function toast(msg, kind) {
		if (!TOAST) return;
		TOAST.hidden = false;
		TOAST.textContent = msg;
		TOAST.className = "flock-engage__toast" + (kind ? " flock-engage__toast--" + kind : "");
		if (toastTimer) clearTimeout(toastTimer);
		toastTimer = setTimeout(() => { TOAST.hidden = true; }, 3200);
	}

	function setConn(kind, label) {
		const dot = CONN && CONN.querySelector(".flock-dot");
		if (dot) dot.className = "flock-dot flock-dot--" + kind;
		if (CONN_LABEL) CONN_LABEL.textContent = label;
	}

	// ---- join flow -------------------------------------------------------- //
	function setJoinStatus(kind, msg) {
		if (!JOIN_STATUS) return;
		JOIN_STATUS.className = "flock-engage__status flock-engage__status--" + kind;
		JOIN_STATUS.textContent = msg;
	}

	async function autoOrManualJoin(sessionId, roomCode) {
		setJoinStatus("loading", _("Joining…"));
		try {
			session = window.FlockEngageCore.useEngagementSession(sessionId, {
				parity: PARITY,
				endpoints: ENDPOINTS,
				i18n: _,
			});
			session.on("connection", (c) => {
				if (c === "live") setConn("ok", _("Live"));
				else if (c === "polling") setConn("slow", _("Slow network — polling"));
				else if (c === "offline") setConn("off", _("Offline — your check-in is saved"));
				else setConn("wait", _("Connecting…"));
			});
			session.on("lifecycle", onLifecycle);
			session.on("headcount", (n) => toast(_("Checked in: {0}").replace("{0}", n)));
			await session.start(roomCode ? { room_code: roomCode } : {});
			enterStage();
		} catch (e) {
			setJoinStatus("error", (e && e.message) || _("Could not join that session."));
		}
	}

	if (JOIN_FORM) {
		JOIN_FORM.addEventListener("submit", (ev) => {
			ev.preventDefault();
			const code = (ROOM_INPUT.value || "").trim();
			if (!/^\d{6}$/.test(code)) {
				setJoinStatus("error", _("Enter the 6-digit code."));
				return;
			}
			autoOrManualJoin(null, code);
		});
	}

	// Share-link auto-join (?session=… / ?code=…).
	const preSession = ROOT.dataset.sessionId;
	const preCode = ROOT.dataset.roomCode;
	if (preSession || preCode) autoOrManualJoin(preSession || null, preCode || null);

	// ---- stage (the <EngagementStage> host) ------------------------------- //
	function enterStage() {
		if (JOIN_VIEW) JOIN_VIEW.hidden = true;
		if (STAGE) STAGE.hidden = false;
		const kind = (session.state.serverSnapshot && session.state.serverSnapshot.kind)
			|| (session.state.serverSnapshot && session.state.serverSnapshot.engagement_kind);
		mountKind(kind);
	}

	function onLifecycle(message) {
		if (!message) return;
		if (message.state === "opened") toast(_("Let's go!"), "ok");
		if (message.state === "closed") {
			renderResults(message.results);
		}
	}

	function renderResults(results) {
		if (!STAGE) return;
		const card = el("section", "flock-engage__results");
		card.setAttribute("role", "status");
		card.appendChild(el("h2", "flock-engage__results-title", _("Session closed")));
		const ok = session.state.participated;
		card.appendChild(el(
			"p",
			"flock-engage__results-line",
			ok ? _("You're checked in — thanks for joining!") : _("Attendance was not recorded this time.")
		));
		if (results && typeof results.count === "number") {
			card.appendChild(el("p", "flock-engage__results-count",
				_("Total checked in: {0}").replace("{0}", results.count)));
		}
		STAGE.innerHTML = "";
		STAGE.appendChild(card);
	}

	// ---- kind router ------------------------------------------------------ //
	const COMPONENTS = {
		TapBurst: renderTapBurst,
		QuizRace: renderQuizRace,
		ReactionTap: renderReactionTap,
		BingoCard: renderBingoCard,
		TeamChallenge: renderTeamChallenge,
		LivePoll: renderLivePoll,
		WordCloud: renderWordCloud,
		LiveQA: renderLiveQA,
		PulseSurvey: renderPulseSurvey,
	};

	function mountKind(kindKey) {
		currentKind = kindKey;
		if (!STAGE) return;
		STAGE.innerHTML = "";
		const meta = KINDS.find((k) => k.kind === kindKey);
		if (!meta) {
			STAGE.appendChild(el("p", "flock-engage__waiting", _("Waiting for the facilitator to start…")));
			return;
		}
		const renderer = COMPONENTS[meta.component];
		const card = el("section", "flock-engage__kind flock-engage__kind--" + meta.family);
		card.setAttribute("aria-label", _(meta.i18n_key));
		const head = el("div", "flock-engage__kind-head");
		head.appendChild(el("h2", "flock-engage__kind-title", _(meta.i18n_key)));
		if (meta.calm_checkin) head.appendChild(renderCalmToggle(meta));
		card.appendChild(head);
		if (renderer) renderer(card, meta);
		else card.appendChild(el("p", "text-muted", _("This experience loads shortly…")));
		STAGE.appendChild(card);
	}

	function renderCalmToggle(meta) {
		const wrap = el("label", "flock-check flock-check--calm");
		const cb = el("input");
		cb.type = "checkbox";
		cb.checked = calmCheckin;
		cb.addEventListener("change", () => {
			calmCheckin = cb.checked;
			haptic(10);
			mountKind(currentKind);
		});
		const span = el("span", null, _("Calm Check-in (no timer)"));
		wrap.appendChild(cb);
		wrap.appendChild(span);
		wrap.appendChild(el("small", "form-text text-muted", _("Counts attendance without scoring speed.")));
		return wrap;
	}

	function participateAck(kind) {
		haptic(20);
		toast(_("Check-in sent!"), "ok");
	}

	// ---- helper: a big accessible tap button ------------------------------ //
	function bigButton(label, onPress, opts) {
		const b = el("button", "flock-engage__tap", label);
		b.type = "button";
		b.style.minHeight = MIN_TARGET + "px";
		b.style.minWidth = MIN_TARGET + "px";
		if (opts && opts.ariaLabel) b.setAttribute("aria-label", opts.ariaLabel);
		if (opts && opts.variant) b.classList.add("flock-engage__tap--" + opts.variant);
		b.addEventListener("click", onPress);
		return b;
	}

	// ===================== STARTER KIND COMPONENTENTS ====================== //
	// Each renders into `card`, reads the session's round config from the server
	// snapshot when present, and calls `session.participate(kind, payload)`.
	// Every timed game offers the Calm Check-in variant (FLO-9 §7).
	// ====================================================================== //

	function renderTapBurst(card, meta) {
		if (calmCheckin) {
			calmCta(card, meta.kind, _("Tap to check in"));
			return;
		}
		const arena = el("div", "flock-engage__arena");
		arena.setAttribute("role", "application");
		arena.setAttribute("aria-label", _("Tap the moving target"));
		card.appendChild(arena);
		let hits = 0;
		const move = () => {
			arena.innerHTML = "";
			const target = bigButton(_("Tap!"), () => {
				hits++;
				session.participate(meta.kind, { round: 1, hit: hits });
				participateAck(meta.kind);
				move();
			}, { ariaLabel: _("Moving check-in target"), variant: "game" });
			const top = 10 + Math.random() * 60;
			const left = 10 + Math.random() * 60;
			target.style.top = top + "%";
			target.style.left = left + "%";
			arena.appendChild(target);
		};
		move();
		caption(card, _("A target moves around — tap it once to check in."));
	}

	function renderQuizRace(card, meta) {
		const rounds = (session.state.serverSnapshot && session.state.serverSnapshot.rounds) || [];
		if (calmCheckin || !rounds.length) {
			calmCta(card, meta.kind, _("Answer at your own pace"));
			return;
		}
		const q = rounds[0] || {};
		const form = el("form", "flock-engage__quiz");
		form.appendChild(el("p", "flock-engage__quiz-q", q.prompt || _("Question")));
		(q.options || []).forEach((opt, idx) => {
			const id = "flock-quiz-" + idx;
			const lab = el("label", "flock-engage__quiz-opt");
			const radio = el("input");
			radio.type = "radio";
			radio.name = "answer";
			radio.value = String(idx);
			radio.id = id;
			radio.style.minHeight = MIN_TARGET + "px";
			lab.appendChild(radio);
			lab.appendChild(el("span", null, opt));
			form.appendChild(lab);
		});
		const submit = bigButton(_("Submit"), () => {
			const picked = form.querySelector('input[name="answer"]:checked');
			session.participate(meta.kind, { round: 1, answer: picked ? Number(picked.value) : null });
			participateAck(meta.kind);
		}, { variant: "primary" });
		form.appendChild(submit);
		card.appendChild(form);
	}

	function renderReactionTap(card, meta) {
		if (calmCheckin) {
			calmCta(card, meta.kind, _("Tap to check in"));
			return;
		}
		const stage = el("div", "flock-engage__reaction", _("Wait for green…"));
		stage.setAttribute("role", "status");
		stage.setAttribute("aria-live", "polite");
		card.appendChild(stage);
		const wait = 1200 + Math.random() * 2800;
		setTimeout(() => {
			stage.textContent = _("TAP NOW");
			stage.className = "flock-engage__reaction flock-engage__reaction--go";
			stage.appendChild(bigButton(_("Tap"), () => {
				session.participate(meta.kind, { round: 1, reaction: true });
				participateAck(meta.kind);
			}, { variant: "go", ariaLabel: _("Tap now") }));
		}, wait);
		caption(card, _("Wait for the green signal, then tap once."));
	}

	function renderBingoCard(card, meta) {
		const actions = (session.state.serverSnapshot && session.state.serverSnapshot.actions) || [
			_("Met a newcomer"), _("Said hello to a leader"), _("Answered the poll"),
		];
		const grid = el("div", "flock-engage__bingo");
		actions.forEach((label, i) => {
			const cell = el("button", "flock-engage__bingo-cell");
			cell.type = "button";
			cell.textContent = label;
			cell.style.minHeight = MIN_TARGET + "px";
			cell.setAttribute("aria-pressed", "false");
			cell.addEventListener("click", () => {
				cell.setAttribute("aria-pressed", "true");
				cell.classList.add("flock-engage__bingo-cell--done");
				session.participate(meta.kind, { action_index: i, action: label });
				participateAck(meta.kind);
			});
			grid.appendChild(cell);
		});
		card.appendChild(grid);
	}

	function renderTeamChallenge(card, meta) {
		const wrap = el("div", "flock-engage__team");
		wrap.appendChild(el("p", "flock-engage__team-hint",
			_("Tap to add your team's contribution.")));
		const tally = el("div", "flock-engage__team-tally", "0");
		let n = 0;
		wrap.appendChild(tally);
		wrap.appendChild(bigButton(_("+1 for my team"), () => {
			n++;
			tally.textContent = String(n);
			session.participate(meta.kind, { contribution: 1 });
			participateAck(meta.kind);
		}, { variant: "game", ariaLabel: _("Add one for my team") }));
		card.appendChild(wrap);
	}

	function renderLivePoll(card, meta) {
		const opts = (session.state.serverSnapshot && session.state.serverSnapshot.options) || [
			_("Yes"), _("No"), _("Not sure"),
		];
		const list = el("div", "flock-engage__poll");
		opts.forEach((label, i) => {
			list.appendChild(bigButton(label, () => {
				session.participate(meta.kind, { choice: i });
				participateAck(meta.kind);
				Array.prototype.forEach.call(list.children, (c) => (c.disabled = true));
			}, { ariaLabel: _("Vote: {0}").replace("{0}", label) }));
		});
		card.appendChild(list);
	}

	function renderWordCloud(card, meta) {
		const form = el("form", "flock-engage__word");
		const input = el("input", "form-control");
		input.type = "text";
		input.maxLength = 60;
		input.placeholder = _("One word that sums it up…");
		input.setAttribute("aria-label", _("Your word"));
		input.style.minHeight = MIN_TARGET + "px";
		form.appendChild(input);
		form.appendChild(bigButton(_("Share"), () => {
			const v = (input.value || "").trim();
			if (!v) return;
			session.participate(meta.kind, { term: v });
			participateAck(meta.kind);
			input.value = "";
		}, { variant: "primary" }));
		card.appendChild(form);
	}

	function renderLiveQA(card, meta) {
		const wrap = el("div", "flock-engage__qa");
		const input = el("input", "form-control");
		input.type = "text";
		input.maxLength = 200;
		input.placeholder = _("Ask a question…");
		input.setAttribute("aria-label", _("Your question"));
		input.style.minHeight = MIN_TARGET + "px";
		wrap.appendChild(input);
		const btns = el("div", "flock-engage__qa-btns");
		btns.appendChild(bigButton(_("Submit question"), () => {
			const v = (input.value || "").trim();
			if (!v) return;
			session.participate(meta.kind, { question: v });
			participateAck(meta.kind);
			input.value = "";
		}, { variant: "primary" }));
		btns.appendChild(bigButton(_("Upvote a question"), () => {
			session.participate(meta.kind, { upvote: true });
			participateAck(meta.kind);
		}));
		wrap.appendChild(btns);
		card.appendChild(wrap);
	}

	function renderPulseSurvey(card, meta) {
		const sliders = (session.state.serverSnapshot && session.state.serverSnapshot.sliders) || [
			{ key: "mood", label: _("How do you feel?") },
			{ key: "clarity", label: _("Was it clear?") },
			{ key: "satisfaction", label: _("Satisfied?") },
		];
		const form = el("form", "flock-engage__pulse");
		const values = {};
		sliders.forEach((s) => {
			values[s.key] = 3;
			const wrap = el("label", "flock-engage__pulse-row");
			wrap.appendChild(el("span", "flock-engage__pulse-label", s.label));
			const range = el("input");
			range.type = "range";
			range.min = "1";
			range.max = "5";
			range.value = "3";
			range.step = "1";
			range.setAttribute("aria-label", s.label);
			range.addEventListener("input", () => { values[s.key] = Number(range.value); });
			wrap.appendChild(range);
			form.appendChild(wrap);
		});
		form.appendChild(bigButton(_("Submit"), () => {
			session.participate(meta.kind, { sliders: values });
			participateAck(meta.kind);
		}, { variant: "primary" }));
		card.appendChild(form);
	}

	// Calm Check-in shared CTA (the no-time-pressure attendance path, §7).
	function calmCta(card, kind, label) {
		card.appendChild(el("p", "flock-engage__calm-hint",
			_("No timer. Check in at your own pace.")));
		card.appendChild(bigButton(label, () => {
			session.participate(kind, { calm: true, round: 1 });
			participateAck(kind);
		}, { variant: "primary", ariaLabel: label }));
	}
})();
