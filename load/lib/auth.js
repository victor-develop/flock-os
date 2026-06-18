// Flock OS – Frappe auth helper for k6 (FLO-49).
//
// Logs in as a Frappe user once per VU and stashes the session cookies so the
// whitelisted bulk_submit endpoint resolves the caller's Flock Branch User
// Permission scope (flock_os.attendance._resolve_caller_branch_scope).
import http from "k6/http";

// Returns the authenticated cookie jar (per-VU). On first call it performs a
// POST /api/method/login against the Frappe site and persists the session cookie.
export function login(cfg) {
	const jar = http.cookieJar();
	const params = {
		jar,
		headers: { "Content-Type": "application/x-www-form-urlencoded" },
	};
	const res = http.post(
		`${cfg.baseUrl}/api/method/login`,
		{
			usr: cfg.username,
			pwd: cfg.password,
		},
		params,
	);
	// Frappe login returns 200 with `{"message": "Logged In"}` on success.
	if (res.status !== 200) {
		throw new Error(`Frappe login failed for ${cfg.username}: HTTP ${res.status}`);
	}
	return jar;
}
