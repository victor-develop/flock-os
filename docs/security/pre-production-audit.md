# Pre-Production Security Audit — Flock OS MVP

| | |
|---|---|
| **Scope** | `flock_os` Frappe app on `master` (commit `f522a24`) |
| **Date** | 2026-06-22 |
| **Auditor** | Frontend Engineer (agent `a95965d2`) |
| **Issue** | [FLO-682](/FLO/issues/FLO-682) |
| **Method** | OWASP-aligned static code review (no external pen-test; no runtime target) |
| **Classification** | No-spend, no-prod-change code review |

---

## Executive Summary

The Flock OS MVP demonstrates a **mature security posture** for a pre-production codebase.
The codebase follows a disciplined hexagonal architecture with a clear separation between
transport (`@frappe.whitelist()` endpoints), domain logic (pure services), and permission
enforcement (central `flock_os.permissions` guards). Every whitelisted mutating endpoint
re-asserts row-level scope server-side — the "never trust the client" principle is applied
consistently throughout.

**No critical findings.** Two **medium** and three **low/info** findings were identified.
All are tracked with follow-up issues or documented tradeoffs. The codebase is **ready for
the Phase 6 launch go/no-go gate** with the medium findings addressed or tracked.

### Findings Summary

| ID | Category | Severity | Status |
|---|---|---|---|
| SEC-AUTH-1 | Auth & access control | **Medium** | Follow-up issue |
| SEC-AUTH-2 | Auth & access control | **Medium** | Follow-up issue |
| SEC-XSS-1 | Input validation / XSS | Low | Follow-up issue |
| SEC-SESS-1 | CSRF & session | Low / Info | Documented tradeoff |
| SEC-DOS-1 | Rate-limiting / DoS | Low | Follow-up issue |
| SEC-DOS-2 | Rate-limiting / DoS | Info | Tracked (FLO-294) |

---

## 1. Auth & Access Control

**Objective:** Verify every `@frappe.whitelist()` endpoint enforces proper permissions.
Flag authenticated-but-under-authorized or guest-accessible write endpoints.

### Methodology

Enumerated all 30+ `@frappe.whitelist()` endpoints across:
- `engagement_api.py` (7 endpoints), `engagement_views.py` (11 endpoints)
- `attendance.py` (2 endpoints), `scheduling.py` (2 endpoints)
- `flock_event_registration.py` (5 endpoints), `flock_event_approval.py` (6 endpoints)
- `flock_event_invitation.py` (2 endpoints), `telemetry_scrape.py` (1 endpoint)
- `realtime_views.py` (1 endpoint), `traversal.py` (transport wrappers)

### Positive Findings

The permission model is **consistently and correctly enforced**:

- **Branch scope assertion** (`assert_branch_scope`): Every mutating endpoint resolves the
  caller's org-tree branch via `_resolve_caller_branch_scope()` or `assert_branch_scope()`
  and rejects cross-branch access. The single sanctioned chokepoint (`flock_os.permissions`)
  is used everywhere — no ad-hoc permission checks.
- **Facilitator lifecycle**: `open_session`, `close_session` enforce `_assert_facilitator()`
  which checks bypass roles → facilitator-of-record → branch scope fallback.
- **Approval workflow**: `submit_for_approval` allows only the requester; `approve_event`/
  `reject_event` use `_decide_current_step()` → `assert_approval_scope()` (both role + row
  scope); `withdraw_event_request` allows only the requester; `cancel_event_request` requires
  Org/Branch Admin.
- **Registration**: `register_for_event` enforces window, scope, and capacity; leader-on-behalf
  registration requires branch scope over the event's branch.
- **No guest-accessible write endpoints**: Zero `allow_guest=True` decorators found. All
  endpoints require authentication.
- **Announcement compose/publish**: `_assert_author_branch_scope()` runs on every mutating
  surface, preventing cross-subtree fan-out.

### SEC-AUTH-1 (Medium): Telemetry scrape Endpoint Lacks Role Restriction

**File:** `flock_os/telemetry_scrape.py:18`
**Endpoint:** `GET/POST /api/method/flock_os.telemetry_scrape.scrape`

The `scrape()` endpoint is decorated with bare `@frappe.whitelist()` — it requires
authentication but enforces **no role restriction**. Any logged-in user (including a
basic member) can access the full Prometheus metrics exposition, which includes:

- MariaDB connection counts, slow query counts, InnoDB buffer pool hit ratios
- Redis memory/connection stats
- RQ queue depths (bulk attendance + default)
- Bulk attendance latency percentiles

**Risk:** Information disclosure. An attacker with a basic member account can enumerate
infrastructure capacity, health signals, and timing data, which aids targeted DoS or
lateral movement planning.

**Remediation:** Restrict to monitoring roles (e.g., System Manager, or a dedicated
`Flock Auditor` role). The Prometheus scrape job should authenticate with a service
account that carries the restricted role. Alternatively, bind the endpoint to an
internal-only network path (nginx ACL) so it is not reachable from the public ingress.

### SEC-AUTH-2 (Medium): `finalize_close` Lacks Facilitator Authorization

**File:** `flock_os/engagement_api.py:200`
**Endpoint:** `POST /api/method/flock_os.engagement_api.finalize_close`

The `finalize_close()` endpoint is decorated with `@frappe.whitelist()` but does **not**
call `_assert_facilitator()`. It is designed as an RQ target scheduled by `close_session`
after `grace_seconds`, but because it is whitelisted, any authenticated user can call it
directly at any time.

**Risk:** An authenticated user can trigger early finalization of a `closing` session,
cutting short the grace window. Reconnecting devices that flush offline queues during the
remaining grace period would have their submissions rejected (the session is already
`closed`). Data integrity is preserved (the operation is idempotent), but attendance
accuracy may be affected for legitimate attendees with flaky connections.

**Remediation:** Add `_assert_facilitator(session_id)` at the top of `finalize_close`,
mirroring `open_session` and `close_session`. The RQ worker runs as the scheduling user,
so the facilitator check should pass for RQ-invoked calls (the enqueue captures the
facilitator's context). Alternatively, verify the caller is the RQ framework (not a
direct HTTP call) via a shared secret or internal-only marker.

---

## 2. Input Validation & Injection

**Objective:** Sweep for SQL injection, XSS, and command injection.

### Methodology

- Searched all `.sql()` / `frappe.db.sql` calls for f-string interpolation of user input.
- Searched all Jinja templates for `| safe` filter usage (disabling autoescape).
- Searched all JS for `innerHTML` / `outerHTML` / `document.write` with server-supplied data.
- Searched all Python for `subprocess`, `os.system`, `eval`, `exec`, `pickle.loads`.

### Positive Findings

- **SQL injection: Clean.** All 46 `.sql()` calls use parameterized queries (`%s`
  placeholders with `values=`). The f-strings in `reporting.py` (lines 568, 636, 655)
  only interpolate static structural elements (placeholder group counts like
  `", ".join(["(%s, %s, %s)"] * len(key_list))`), never user input. The doctype table
  names in f-strings are class-level constants (`ATTENDANCE_DOCTYPE`, `SUMMARY_DOCTYPE`),
  not user-controlled.
- **Dev/bench tooling:** `scale_profile.py:167` concatenates `"EXPLAIN " + sql`, but
  the `sql` argument comes from internal profiling functions with hardcoded queries —
  not user-facing. Acceptable for a bench-only dev tool.
- **Command injection: Clean.** The only `subprocess.run()` in production code
  (`realtime_setup.py:208`) invokes a fixed internal bash script (`wire-socketio-handler.sh`)
  with a bench-root path resolved internally — no user input in the command line. All
  other `subprocess` usage is in test files (mocked).
- **No dangerous builtins:** Zero `eval()`, `exec()`, `pickle.loads()`, or `os.system()`
  calls in production code.
- **Jinja2 XSS: Clean.** No `| safe` filter used in any template. Frappe's Jinja2
  autoescape is active by default, so all `{{ }}` output is HTML-escaped. JSON data
  injected into `data-*` attributes (e.g., `data-endpoints="{{ endpoints_json }}"`) is
  autoescaped — `"` becomes `&quot;`, preventing attribute breakout.
- **`announce.js` XSS handling: Good.** Server-supplied data rendered via `innerHTML`
  (branch names, notification refs, scheduled timestamps) is consistently passed through
  `frappe.utils.escape_html()`.
- **`engage.js` / `engage-host.js` DOM construction: Good.** The `el(tag, cls, text)`
  helper creates elements via `document.createElement` and sets `textContent` — never
  `innerHTML` — for server-supplied values (review queue items, attendee names, etc.).
- **Querystring injection: Clean.** `engage.py` uses `frappe.utils.url_quote()` for
  session/room-code values in redirect querystrings.

### SEC-XSS-1 (Low): Exception Messages Rendered via innerHTML in engage-host.js

**File:** `flock_os/public/js/engage-host.js:119` (and `announce.js` does NOT have this issue)

The `setStatus(kind, html)` function sets `STATUS.innerHTML = html`. Several callers
interpolate server exception messages:

```javascript
setStatus("error", _("Override failed. ") + ((e && e.message) || ""));
setStatus("error", _("Could not load that template. ") + ((e && e.message) || ""));
```

Where `e.message` is derived from `new Error(String(r.exc))` — the Frappe AJAX exception
response. If an attacker can influence the server-side exception message (e.g., through a
crafted input that produces a validation error containing `<script>`), it would be rendered
as HTML.

**Risk:** Low. Frappe's framework typically sanitizes exception messages, and the attacker
would need to control the specific exception text that reaches the client. No stored XSS
vector was identified.

**Remediation:** Replace `setStatus(kind, html)` with a `textContent`-based variant for
dynamic content, or escape `e.message` via `frappe.utils.escape_html()` before
concatenating into the HTML string.

---

## 3. Secrets & Config Hygiene

**Objective:** Confirm zero hard-coded secrets; check config files and `.gitignore` coverage.

### Methodology

- Reviewed `.gitignore` for completeness.
- Reviewed `.env.example` for real secrets.
- Reviewed deploy templates (`common_site_config.json.tmpl`, `site_config.json.tmpl`).
- Searched for common secret patterns (API keys, passwords, tokens) in tracked files.
- Verified SOPS/age secrets management (FLO-678 pattern).

### Findings: Clean (No Issues)

- **`.gitignore`: Comprehensive.** Covers `.env`, `.env.*` (except `.env.example`),
  `secrets/*.yaml` (except `*.enc.yaml` encrypted bundles), `secrets/.age-key`,
  `secrets/*.key`, rendered env files, docker runtime config, PEM files.
- **`.env.example`: Placeholders only.** All secret values are `changeme-generate-a-strong-local-password`
  with generation instructions. No real credentials.
- **Deploy templates: Env-rendered.** Both `common_site_config.json.tmpl` and
  `site_config.json.tmpl` render every value from `${ENV_VAR}` placeholders. `db_password`,
  `encryption_key`, Redis URIs — all sourced from environment at deploy time.
  `developer_mode: 0` (production setting). No committed secrets.
- **SOPS+age:** `secrets/prod.enc.yaml` and `secrets/staging.enc.yaml` are encrypted
  ciphertext bundles. The age private key (`secrets/.age-key`) is gitignored. The FLO-678
  secrets scaffold is verified on the app side.
- **No hardcoded secrets found** in any tracked Python, JS, or config file.

---

## 4. CSRF & Session

**Objective:** Confirm CSRF middleware is active; check WebSocket/session handling gaps.

### Methodology

- Searched for CSRF exemptions (`ignore_csrf`, `allow_guest`).
- Reviewed WebSocket auth flow (`flock_room_handlers.js`, `flock_auth_cache.js`).
- Reviewed realtime scope gate (`realtime_views.py`).
- Reviewed session handling patterns.

### Positive Findings

- **CSRF: Framework-default.** Zero CSRF exemptions found. Frappe's CSRF middleware applies
  to all POST routes by default. No `ignore_csrf` or equivalent override anywhere.
- **WebSocket room join: Defense in depth.** Two independent gates:
  1. **Prefix ACL** (`isFlockEventRoom`): Only `flock_os:event:*` rooms matching the strict
     regex `/^flock_os:event:.+?:(?:broadcast|shard:\d+)$/` route to the handler. An
     authenticated socket cannot eavesdrop on Frappe's internal rooms (`doc:*`, `task:*`).
  2. **Branch scope** (`can_join_event_room`): Before joining, the server checks
     `flock_os.realtime_views.can_join_event_room` whether the socket's user has branch
     scope over the gathering. Fails closed (denied/error → stay out of room).
- **Realtime scope gate rate-limited:** `can_join_event_room` applies `enforce_public`
  (10/s per user+IP) before the scope decision.

### SEC-SESS-1 (Low / Info): WS Auth Cache TTL Window

**File:** `realtime/middlewares/flock_auth_cache.js`

The per-connection auth cache caches resolved identity by `sid` with a 60-second TTL. On a
cache HIT, the wrapper replays the cached `{user, user_type}` and skips the redundant
`get_user_info` HTTP validation. This is a **deliberate, documented tradeoff** to clear the
15k-concurrent-connection auth wall (FLO-116).

**Risk:** A revoked user can establish **new** WebSocket connections for up to 60 seconds
after their session is revoked (existing connections are unaffected — they persist until
disconnect). Within this window, the revoked user's cached identity grants the same access
they had before revocation.

**Assessment:** Acceptable for pre-production. The TTL is conservative (60s vs. the 120s
burst window), and the security tradeoff is explicitly documented in the code with Redis-push
logout invalidation noted as an optional hardening follow-up. This should be revisited if
the threat model changes (e.g., adversarial members within the same org).

---

## 5. File Upload / Download

**Objective:** Audit file-handling for path traversal, unrestricted file types, and SSRF.

### Findings: Not Applicable (No Issues)

The `flock_os` app has **no file-handling surfaces**:
- No file upload/download endpoints.
- No file-handling doctypes (no `File` field types in any DocType JSON).
- No path-construction from user input.
- No URL-fetch / SSRF-vulnerable HTTP calls with user-supplied URLs.
- No `open()`, `shutil`, or file I/O with user-controlled paths in production code.

This category is clean by absence — the app's domain (org tree, events, attendance,
engagement, announcements) does not require file upload/download functionality.

---

## 6. Rate-Limiting & DoS Surface

**Objective:** Identify unthrottled high-fan-out endpoints; cross-reference FLO-294.

### Methodology

- Reviewed `rate_limit.py` and `rate_limit_frappe.py` for the throttle primitive.
- Traced `enforce_public()` calls across all public endpoints.
- Identified endpoints lacking rate limiting.

### Positive Findings

The app-layer rate-limiting is well-designed and applied to the three highest-fan-out
**public door surfaces** (FLO-319):

| Endpoint | Throttle | Limit |
|---|---|---|
| `engagement_api.join_session` | `enforce_public("join_session", ...)` | 10/s per device+IP |
| `register_for_event` | `enforce_public("register_for_event", ...)` | 10/s per registrant+IP |
| `can_join_event_room` | `enforce_public("realtime_join", ...)` | 10/s per user+IP |
| `engagement.participate` | Engagement runtime throttle | 5/s per attendee |

Additional DoS protections:
- `bulk_submit` enforces `enforce_batch_size()` (batch size cap).
- `suspect_review_queue` payload is server-capped (`REVIEW_QUEUE_MAX`).
- Edge rate-limiting (Cloudflare) tracked in [FLO-294](/FLO/issues/FLO-294) (out of scope).

### SEC-DOS-1 (Low): Telemetry Scrape Endpoint Unthrottled

**File:** `flock_os/telemetry_scrape.py:18`

The `scrape()` endpoint has no rate limiting. Each invocation triggers `SHOW GLOBAL STATUS`
DB queries, Redis info calls, and RQ queue-depth Redis calls. An authenticated user polling
this endpoint in a tight loop amplifies infrastructure load.

**Remediation:** Apply `enforce_public("telemetry_scrape", ...)` or restrict the endpoint
to internal network paths (nginx ACL). Coupled with the SEC-AUTH-1 role restriction, this
becomes a non-issue if only the Prometheus scraper can reach it.

### SEC-DOS-2 (Info): `session_state` Endpoint Unthrottled

**File:** `flock_os/engagement_api.py:354`

The `session_state()` polling-fallback endpoint has no rate limiting. It performs a Redis
read (attendee list) per call. A user could poll it rapidly, but the impact is low (single
Redis read, no write amplification). The player client uses this as a reconnect fallback,
not a primary path (WebSocket is primary).

**Assessment:** Acceptable for pre-production. Edge rate-limiting (FLO-294) will cap
external polling at the infrastructure layer.

---

## Architecture Security Notes

The following architectural patterns significantly reduce the attack surface and are
commendable:

1. **Hexagonal separation:** Transport (`@frappe.whitelist()`) → pure domain services →
   gateway ports. Business logic never lives in the transport layer. This makes permission
   enforcement auditable at a single chokepoint.
2. **Single-source permission guard:** `flock_os.permissions.assert_branch_scope()` is the
   one sanctioned branch-scope assertion used by every endpoint. No drift, no ad-hoc checks.
3. **Permission query conditions hook:** `permission_query_conditions` in `hooks.py`
   narrows every list/get query at the SQL level for scoped doctypes — defense in depth
   beyond the endpoint-level checks.
4. **Tenant isolation tests:** `test_tenant_isolation.py` pins the deny-path against the
   underlying guard, ensuring regressions are caught.
5. **Best-effort DocType persistence:** Engagement session DocType writes use
   `ignore_permissions=True` but are best-effort metadata — the runtime state (attendance,
   scope) lives in the pure service layer with its own permission enforcement.
6. **No client-trust:** Every portal page comment and docstring emphasizes "the backend is
   the source of truth." The UI only renders offered scope; the backend re-validates.

---

## Recommendation for the FLO-533 Go/No-Go Gate

**Recommendation: GO.**

The codebase is production-ready from a security standpoint. The two medium findings
(SEC-AUTH-1, SEC-AUTH-2) are tracked with follow-up issues and do not represent
critical vulnerabilities — they are authorization-hardening gaps on already-authenticated
endpoints, not unauthenticated attack vectors. The low/info findings are either documented
tradeoffs or minor hardening opportunities.

The absence of critical/high findings, combined with the consistent permission enforcement
and strong input-validation hygiene, supports proceeding to the Phase 6 launch.

---

## Appendix: Files Reviewed

### Python (production)
- `flock_os/engagement_api.py`, `flock_os/engagement_views.py`
- `flock_os/attendance.py`, `flock_os/reporting.py`
- `flock_os/scheduling.py`, `flock_os/notifications.py`
- `flock_os/permissions.py`, `flock_os/portal.py`
- `flock_os/telemetry.py`, `flock_os/telemetry_scrape.py`
- `flock_os/realtime.py`, `flock_os/realtime_views.py`
- `flock_os/rate_limit.py`, `flock_os/rate_limit_frappe.py`
- `flock_os/traversal.py`, `flock_os/hooks.py`
- `flock_os/flock_os/doctype/flock_event_registration/flock_event_registration.py`
- `flock_os/flock_os/doctype/flock_event_approval/flock_event_approval.py`
- `flock_os/flock_os/doctype/flock_event_invitation/flock_event_invitation.py`
- `flock_os/utils/realtime_setup.py`, `flock_os/utils/scale_profile.py`

### JavaScript (production)
- `flock_os/public/js/announce.js`, `flock_os/public/js/engage.js`
- `flock_os/public/js/engage-host.js`, `flock_os/public/js/engagement-core.js`
- `realtime/handlers/flock_room_handlers.js`
- `realtime/middlewares/flock_auth_cache.js`
- `realtime/adapters/flock_redis_adapter.js`

### Templates & Config
- `flock_os/www/engage.html`, `flock_os/www/engage-host.html`
- `flock_os/www/login.html`, `flock_os/www/announce.html`
- `flock_os/www/engage.py`, `flock_os/www/engage-host.py`
- `flock_os/www/announce.py`, `flock_os/www/login.py`
- `flock_os/www/engage-templates.py`
- `deploy/templates/common_site_config.json.tmpl`
- `deploy/templates/site_config.json.tmpl`
- `.env.example`, `.gitignore`, `.sops.yaml`
- `secrets/prod.enc.yaml`, `secrets/staging.enc.yaml` (encrypted — not decrypted)
