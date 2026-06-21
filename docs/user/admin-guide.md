# Branch Admin Guide — Flock OS

This guide walks a **branch admin** (and group leader) through the core Flock OS
flows: setting up a branch, creating and running groups, scheduling events,
taking attendance, running fun engagement sessions, and reading the reporting
dashboards.

It is written for a non-developer. Every field name referenced below is a real
field on a real DocType in the canonical data model ([FLO-5](/FLO/issues/FLO-5)).
If you ever need the authoritative definition of a field, open the matching
DocType in **Flock OS → DocTypes** (Desk).

> **Where this guide fits:** this is the end-user / admin product guide. For
> infrastructure, deploy, and incident runbooks see the
> [User Docs index](./README.md).

---

## 0. Roles & permissions (read this first)

Flock OS is a multi-branch system. What you can see and do is governed by your
**role** and where you sit in the **org tree**. There are six roles
([FLO-5 §4.1](/FLO/issues/FLO-5)):

| Role | What it can do |
|------|----------------|
| **Flock Org Admin** | Everything, across **all branches**. The root authority. |
| **Flock Branch Admin** | Manage their **branch subtree** (their branch + descendant branches), its groups, members, events, attendance, approvals, and announcements. |
| **Flock Group Leader** | Lead the **groups** assigned to them (via `Flock Group Member` roster rows where `role` = `Leader` / `Co-Leader`). Report attendance, create one-time events, run engagement sessions. |
| **Flock Member** | Self-serve: register for events, join engagement sessions, see their own groups. |
| **Flock Visitor** | Join an engagement session / register for an event. Scoped to **self only**. |
| **Flock Auditor** | Read-only visibility across **all branches** (compliance). |

Two trees organize your organization:

- **`Flock Branch`** (`is_tree=1`) — the administrative / regional tree. Parent
  link field: `parent_branch`. A branch admin's reach is defined by a
  `Flock Branch Admin Scope` row, which materializes the subtree they administer.
- **`Flock Group`** (`is_tree=1`) — the ministry / cell tree. Parent link field:
  `parent_group`. A group subtree lives **within exactly one branch**
  (`Flock Group.branch`). Groups never span branches.

A **sibling branch is never visible** to a scoped branch admin — the picker in
every compose/announce/engage surface only offers your targetable subtree.

---

## 1. Set up your branch + groups

### 1.1 Confirm your branch exists

A `Flock Branch Admin` manages one branch subtree. Branches are created by an
`Flock Org Admin` (or by you, for sub-branches under your scope).

1. In Desk, open **Flock Branch List**.
2. Confirm your branch row is present. Key fields on `Flock Branch`:
   - `branch_name` (required, unique — it is the document name).
   - `organization` → links `Flock Organization` (the singleton tenant root).
   - `parent_branch` → the tree parent (root branches leave this blank).
   - `country`, `city`, `timezone` → location context.
   - `is_active` → uncheck to retire a branch without deleting it.
3. To **nest** a branch (e.g. a region → campus), create the child branch and set
   its `parent_branch` to the region.

### 1.2 Assign a branch admin (scope)

A user becomes a branch admin through a `Flock Branch Admin Scope` row:

1. Open **Flock Branch Admin Scope List** → **Add New**.
2. Fields:
   - `user` → the Frappe `User` who will administer.
   - `branch` → the **admin root** branch (the subtree root they own).
   - `organization` → auto-resolved from the branch.
   - `is_active` → check to enable.
3. Save. On save, the row **syncs Frappe User Permissions** so the user can see
   exactly that branch subtree (`last_synced_subtree` records the sync). The
   user must also hold the `Flock Branch Admin` **role**.

### 1.3 Create group types (optional, once)

`Flock Group Type` categorizes groups (e.g. "Youth", "Worship Team",
"Outreach"). Create once per organization:

1. Open **Flock Group Type List** → **Add New**.
2. `group_type_name` (required, unique), `description`, `is_active`.

### 1.4 Create a group

`Flock Group` is the ministry/cell unit. Groups are branch-bound and nest.

1. Open **Flock Group List** → **Add New** (or use the **Tree View**).
2. Required fields:
   - `group_name` (required) — e.g. "Youth Band".
   - `branch` → links `Flock Branch` (required). A group's branch is fixed.
   - `organization` → auto-resolved.
3. Optional fields:
   - `parent_group` → nest under a parent group (keeps the tree within one
     branch — a child group inherits the branch of its parent).
   - `group_type` → links `Flock Group Type`.
   - `leader` → links `Flock Member` who leads the group.
   - `established_date`, `description`.
   - `is_active` → default on.
4. The document name is auto-generated as `{branch}-{group_name}`.

### 1.5 Add members to a group

Membership is the `Flock Group Member` edge between a `Flock Member` and a
`Flock Group`.

1. Open **Flock Group Member List** → **Add New**.
2. Required fields:
   - `group` → links `Flock Group`.
   - `member` → links `Flock Member`.
   - `branch` → auto-denormalized from the group's branch (read-only).
   - `role` → one of `Leader`, `Co-Leader`, `Member`, `Visitor` (default
     `Member`). The `Leader` / `Co-Leader` values are what make a person a
     **group leader** for permission routing.
   - `status` → `Active` or `Inactive` (default `Active`).
   - `joined_date` → default today.

### 1.6 Create / manage members

`Flock Member` is a person, linked 1:1 to a Frappe `User` via `linked_user`.

1. Open **Flock Member List** → **Add New**.
2. Key fields:
   - `first_name`, `last_name` → `full_name` is auto-computed (read-only).
   - `status` → `Member`, `Pre-Member`, or `Visitor`.
     **`Visitor` is a status on `Flock Member`**, not a separate DocType.
     Pre-members and visitors are accepted as attendees in attendance reports.
   - `branch` → **Home Branch** (required). The row-level permission anchor.
   - `organization` → auto-resolved.
   - `email`, `phone`, `gender`, `dob` → contact fields (visible to leaders+).
   - `linked_user` → the Frappe login account.
   - `admin_note` → branch-admin-only note.
   - `is_active` → default on.

---

## 2. Create + publish an event

A gathering/event is a `Flock Gathering` (submittable document). There are two
categories, controlled by `event_category`:

- **`Routine`** (default) — a recurring group gathering (a Sunday service, a
  weekly cell meeting). No approval flow. The leader creates it and reports
  attendance directly.
- **`One-time`** — a leader-created event that must be **approved up the group
  tree** before registration opens (a conference, a joint outreach). Triggers a
  `Flock Event Approval`.

### 2.1 (Once) configure gathering types

`Flock Gathering Type` holds per-type defaults. Create once:

1. Open **Flock Gathering Type List** → **Add New**.
2. Fields:
   - `gathering_type_name` (required, unique).
   - `branch` → leave blank for an org-wide default, or set for a branch-specific type.
   - `is_recurring_default`, `default_duration_min`.
   - `capture_methods` → multi-select: `Manual Report`, `Self Check-in`,
     `Live Game`, `Live Questionnaire` (which attendance paths this type allows).
   - `requires_confirmation` → whether a branch admin must confirm the report.
   - `is_active` → default on.

### 2.2 Create a routine gathering

1. Open **Flock Gathering List** → **Add New**.
2. Required / key fields:
   - `branch` (required) → links `Flock Branch`.
   - `group` (required) → links `Flock Group` (must be in the same branch).
   - `title` (required).
   - `starts_on` (required, datetime).
   - `ends_on`, `location`.
   - `gathering_type` → links `Flock Gathering Type`.
   - `status` → default `Scheduled`. Lifecycle:
     `Scheduled → Held → Reported → Confirmed` (terminals: `Confirmed`,
     `Cancelled`). See [§4](#4-take-attendance) for the transitions.
   - `capacity` → optional non-negative cap.
   - `event_category` → `Routine` (default).
   - `approval_status` → `Not Required` (default for routine).
3. **Save**. The gathering is `Scheduled`.
4. When the gathering happens, move it to **`Held`** (submit the document).
   A submittable `Flock Gathering` flows Draft → Submitted; the domain status
   field tracks the lifecycle above.

### 2.3 Create a one-time event (with approval)

A one-time event needs approval before registration opens.

1. Create the `Flock Gathering` as above, but set **`event_category` = `One-time`**.
2. This unlocks the **Scoped Registration** section (permlevel 1, leader+):
   - `registration_scope` → who may register: `Own Group`, `Group Subtree`,
     `Branch`, `Branch Subtree`, `Org-wide`, or `Invited Only` (`None` = closed).
   - `registration_capacity` → seat cap (optional).
   - `registration_opens_on` / `registration_closes_on` → the registration window.
3. On save/submit, a **`Flock Event Approval`** is created (1:1 with the
   gathering; referenced by `Flock Gathering.approval_request`). Its lifecycle:
   - `status`: `Draft → Pending Approval → Approved` (or
     `Rejected` / `Cancelled` / `Withdrawn`).
   - `approval_policy` → links `Flock Event Approval Policy` (the chain rules).
   - `steps` → child table of `Flock Event Approval Step` rows, one per
     approver level (`Parent Group Leader`, `Ancestor Group Leader`,
     `Branch Admin`). Each step has `step_status`:
     `Pending → Approved` / `Rejected` / `Skipped` / `Recused`.
   - `proposed_registration_scope` → the scope the final approver confirms.
   - `auto_approved` → set if the policy auto-approves under a capacity threshold.
4. Approvers act on each step in order. The **final** step is typically a
   `Branch Admin` (controlled by the policy's
   `require_branch_admin_final`). When the chain approves:
   - `Flock Event Approval.status` → `Approved`.
   - `Flock Gathering.approval_status` → `Approved`.
   - The gathering's `registration_scope` is locked to the confirmed scope and
     registration opens at `registration_opens_on`.

### 2.4 Approval policies (configure once)

`Flock Event Approval Policy` defines the chain. Fields:

- `approval_policy_name` (required, unique).
- `branch` → blank = org-wide default; set for a branch override.
- `require_branch_admin_final` → default on (branch admin is the terminator).
- `max_approval_levels` → cap the chain length (blank = full walk to root).
- `allow_self_approval` → default off.
- `auto_approve_below_capacity` → auto-approve small events under this headcount.
- `default_registration_scope` → default `Own Group`.
- `enable_waitlist` → default on.
- `approval_timeout_hours` → auto-escalate/expire after this long.

---

## 3. Manage event registration

Registration applies to **one-time events** (routine gatherings use leader
attendance reporting instead).

### 3.1 How members register

A registration is a `Flock Event Registration` row. Members self-register
(`registered_via` = `Self`) or a leader registers them (`Leader`); invitations
register via `Invite`, and bulk imports via `Bulk`. Key fields:

- `branch`, `group`, `gathering` (required) → scoping.
- `registrant` → links `Flock Member` (required).
- `registrant_name` → auto (read-only).
- `registration_status` → `Registered` (default), `Waitlisted`, `Cancelled`,
  `Checked-in`, `No-show`.
- `registered_at` → server-stamped (read-only).
- `checked_in_attendance` → links the `Flock Attendance Record` produced on
  check-in (read-only).

Registration only succeeds if the member is **inside the gathering's confirmed
`registration_scope`** and the **registration window is open**
(`registration_opens_on` ≤ now ≤ `registration_closes_on`) and there is
**capacity** (`registration_capacity`). When full, a member is `Waitlisted`
(if `enable_waitlist`); when a seat frees, the oldest waitlister is promoted.

### 3.2 Invitations (Invited Only scope)

For `registration_scope` = `Invited Only`, send `Flock Event Invitation` rows:

- `gathering` (required), `invitee` (a `Flock Member`) or `invitee_group` (bulk-
  invite a whole group subtree).
- `invite_token` → a login-less RSVP token (auto-generated, read-only).
- `status` → `Sent → Accepted` / `Declined` / `Expired`.
- `expires_on` → optional expiry.
- `accepted_registration` → the resulting `Flock Event Registration` (read-only).

---

## 4. Take attendance

There are **four** attendance capture paths. All produce `Flock Attendance
Record` rows; the `source` field records which path created each row
(`Manual`, `Self`, `Game`, `Questionnaire`, or `leader`).

### 4.1 Lifecycle reminder

The gathering status drives reporting:

```
Scheduled → Held → Reported → Confirmed
                 ↘ Cancelled
```

- `Scheduled` → the event is planned.
- `Held` → the event happened (leader marks it held).
- `Reported` → the leader has submitted the attendance report.
- `Confirmed` → a branch admin has confirmed the report (terminal).
- `Cancelled` → terminal.

### 4.2 Leader attendance report (single / manual)

A group leader reports attendance for a `Held` gathering:

1. Open the `Flock Gathering`, ensure `status` = `Held`.
2. Use the **Report Attendance** action (the `report_attendance` endpoint). The
   leader submits members **and visitors/pre-members** as attendees.
3. Each attendee is recorded as a `Flock Attendance Record`:
   - `event` → the gathering name (legacy string ref).
   - `gathering` → the proper `Flock Gathering` link.
   - `attendee_ref` / `member` → the `Flock Member`.
   - `branch`, `group`, `organization` → scoping.
   - `status` → `Present` (default).
   - `source` → `leader` for this path.
   - `role` → `member` / `visitor` / `leader`.
4. On success, the gathering moves `Held → Reported` and the
   `flock.attendance.reported` event is emitted.

### 4.3 Bulk attendance import

For large events (up to ~15,000 attendees), use the bulk path:

1. Use the `bulk_submit` endpoint with a `batch_id` and a list of attendance
   items (each ≤ `BULK_BATCH_SIZE`). Items carry `attendee_ref`, optional
   `client_req_id` (per-item idempotency key), and `status`.
2. The batch is **enqueued** (durable the moment the queue accepts it) and a
   receipt is returned immediately. A background job does the durable write with
   idempotent retries and a dead-letter queue.
3. Rows are deduplicated by `(event, attendee_ref)` (and by `client_req_id`).
4. Hot-path counts are maintained atomically via `Event Attendance Summary`
   (per `(branch, event)` counter) — **never** rely on a live `COUNT(*)`.

### 4.4 Self check-in

Members can self-check-in to a registered one-time event. This flips their
`Flock Event Registration.registration_status` → `Checked-in` and creates the
linked `Flock Attendance Record` (stored on
`registration.checked_in_attendance`).

### 4.5 Fun attendance (games + questionnaires)

The engagement runtime (see [§5](#5-run-a-fun-attendance-session)) also records
attendance: every player who participates in a live game or questionnaire is
recorded as an attendee (`source` = `Game` or `Questionnaire`). This is the
primary "fun" path — see below.

---

## 5. Run a fun attendance session

**Fun Attendance** replaces boring "mark present" forms with live mini-games and
live questionnaires. After a session closes, the players are recorded as
attendees. There are two portal pages:

- **`/engage-host`** — the **facilitator console** (leaders / branch admins).
- **`/engage`** — the **player portal** (members + visitors).

### 5.1 Engagement kinds

A `Flock Engagement Session` has an `engagement_type` and a `kind`:

- **Games** (`engagement_type` = `game`):
  `quiz_race`, `tap_burst`, `reaction`, `bingo`, `team_challenge`.
- **Questionnaires** (`engagement_type` = `questionnaire`):
  `poll`, `word_cloud`, `qa`, `pulse`.

### 5.2 Templates (optional, reusable)

You can pre-build reusable session templates so facilitators launch fast:

- `Flock Engagement Game Template` — for game kinds (`GAME-TPL-.#####`).
- `Flock Engagement Questionnaire Template` — for questionnaire kinds
  (`Q-TPL-.#####`).

Both carry: `template_name`, `kind`, `config` (JSON: rounds, questions, bingo
cells, poll options), `description`, `accessibility_mode_default`, `reviewed`,
`is_active`. Questionnaire templates also carry `free_text_retention_days` and
`moderation_required`. Manage them at **`/engage-templates`**.

### 5.3 Create + run a session (facilitator)

1. Go to **`/engage-host`**. The console shows only your **targetable** branches,
   groups, and gatherings (siblings never appear).
2. Pick the **gathering**, the **engagement type** + **kind**, and a title.
   You may launch from a saved template (the `?template=...` preselect).
3. Create the session. A `Flock Engagement Session` is created with:
   - `status` = `draft` (or `scheduled` if you set `scheduled_at`).
   - `facilitator` → your `Flock Member`.
   - `gathering` (required), `branch`, `group`, `organization`.
   - `grace_seconds` → default `30` (lateness tolerance on close).
   - `room_code` → a **6-digit join code** players enter.
   - `config` → the rounds / questions payload.
4. **Open** the session (`status` → `open`). The `open_at` timestamp is
   server-stamped. The realtime projector broadcasts `flock.engagement.opened`
   so the player portal goes live.
5. **Share** the room: give players the `/engage` link with `?session=<id>` or
   the 6-digit `room_code` (or a QR code).
6. Players join and **participate**. Each round is a `Flock Engagement Round`
   (1-based `round_index`, `prompt`, `options_json`, `duration_seconds`,
   server-timed `starts_at`). Responses are `Flock Engagement Feedback` rows.
7. **Close** the session (`status` → `closing` → `closed`). On close:
   - `close_at` is server-stamped.
   - Every player is recorded as a `Flock Attendance Record`
     (`source` = `Game`/`Questionnaire`, `engagement_session` linked).
   - `attendee_count` and `batch_id` are populated (read-only).

Session lifecycle: `draft → scheduled → open → closing → closed → archived`.

> **Accessibility:** sessions support an accessibility mode
> (`accessibility_mode_default`, larger tap targets via `A11Y_MIN_TARGET_PX`),
> `calm_equivalent` rounds, and multi-language (`languages`).

---

## 6. Send announcements / notifications

Announcements let admins push scoped messages to leaders/members.

1. Go to **`/announce`** (the compose surface). Like the engage console, it only
   offers your targetable branches + groups.
2. A `Flock Announcement` has:
   - `subject` + `body` (required; body is rich text).
   - `category` → `General`, `Urgent`, `Event`, `Pastoral`, `Administrative`,
     `Other`.
   - `priority` → `Low`, `Normal`, `High`, `Critical`.
   - `audience_role` → `Everyone`, `Leaders Only`, `Admins Only`, `Members Only`.
   - `branch` (required) + optional `group` → the scope.
   - `pinned` → pin to top.
   - `channels` → child rows of `Flock Announcement Channel`:
     `In-App`, `Push`, `Email`, `SMS`.
   - `status` → `Draft → Scheduled → Publishing → Published → Archived`.
   - `scheduled_at`, `published_at` (read-only), `expires_at`.
3. **Preview** the audience (the `preview_audience` endpoint shows who will
   receive it), then **Publish** (or **Schedule**). The backend re-validates the
   scope on publish — the picker is defense-in-depth, not the source of truth.

---

## 7. Read the reporting / attendance dashboards

After events are reported and engagement sessions close, the counts roll up.

### 7.1 Gathering attendance counters

On each `Flock Gathering`, the **Confirmation + Roll-up Counters** section
(permlevel 2, branch admin+) holds:

- `registered_count`, `checked_in_count` → registration counters.
- `member_attendance_count`, `visitor_attendance_count`,
  `total_attendance_count`, `first_time_count` → the confirmed roll-up.
- `attendance_source` → which sources contributed (`Manual` / `Self` / `Game` /
  `Questionnaire`).
- `reported_by` / `reported_at`, `confirmed_by` / `confirmed_at`.

### 7.2 Branch / event roll-up

`Event Attendance Summary` is the **sanctioned hot counter** — one row per
`(branch, event)` with a `total` maintained by atomic
`INSERT ... ON DUPLICATE KEY UPDATE`. This is what dashboards and scale paths
read; never run a live `COUNT(*)` on attendance rows.

### 7.3 Confirming reports

A branch admin confirms a leader's report:

1. Open the `Flock Gathering` with `status` = `Reported`.
2. Review the counters (`total_attendance_count`, etc.).
3. Move `status` → **`Confirmed`** (terminal). This stamps `confirmed_by` /
   `confirmed_at`.

### 7.4 Engagement session results

On a closed `Flock Engagement Session`, the **Close Results** section
(read-only) shows `attendee_count` and `batch_id`. Per-round responses live in
`Flock Engagement Feedback` (poll choices, word-cloud terms, Q&A
questions/upvotes, slider/pulse values) and the `Flock Engagement Round` rows.

### 7.5 Audit trail

Every privileged or cross-scope operation writes a `Flock Audit Log` row:
`action`, `doctype_ref`, `docname`, `actor` (the `User`), a mandatory `reason`,
and `detail` (JSON). `Flock Auditor` role has read-only access to this trail
across all branches.

---

## Quick reference — DocType catalog

| DocType | Purpose | Key unique field |
|---------|---------|------------------|
| `Flock Organization` | Tenant root (singleton) | — |
| `Flock Branch` | Admin tree (`is_tree`) | `branch_name` |
| `Flock Group` | Ministry/cell tree (`is_tree`, branch-bound) | `{branch}-{group_name}` |
| `Flock Group Type` | Group categories | `group_type_name` |
| `Flock Group Member` | Membership / roster edge | naming series `GM-` |
| `Flock Member` | A person | naming series `MEM-` |
| `Flock Branch Admin Scope` | Branch admin subtree mapping | `{user}-{branch}` |
| `Flock Gathering` | An event/gathering (submittable) | naming series `GATH-` |
| `Flock Gathering Type` | Event type defaults | `gathering_type_name` |
| `Flock Event Registration` | One-time event registration | naming series `EVREG-` |
| `Flock Event Invitation` | Invite / RSVP | naming series `INV-` |
| `Flock Event Approval` | One-time event approval (submittable) | naming series `EVAPPR-` |
| `Flock Event Approval Step` | Approval chain child row | naming series |
| `Flock Event Approval Policy` | Approval chain rules | `approval_policy_name` |
| `Flock Attendance Record` | One attendance row (autoincrement) | `(event, attendee_ref)` unique |
| `Event Attendance Summary` | Hot counter per (branch, event) | `(branch, event)` unique |
| `Flock Engagement Session` | A fun-attendance session | naming series `ENG-` |
| `Flock Engagement Round` | One round in a session (autoincrement) | — |
| `Flock Engagement Feedback` | One response (autoincrement) | — |
| `Flock Engagement Game Template` | Reusable game template | naming series `GAME-TPL-` |
| `Flock Engagement Questionnaire Template` | Reusable questionnaire template | naming series `Q-TPL-` |
| `Flock Announcement` | Scoped announcement | `ANN-{#####}` |
| `Flock Announcement Channel` | Channel child row | naming series `AC-` |
| `Flock Audit Log` | Audit trail | random hash |

---

## Where to go next

- **Attendee-facing help**: [Attendee Guide](./attendee-guide.md).
- **All docs index**: [User Docs](./README.md).
- **Canonical data model**: [FLO-5](/FLO/issues/FLO-5).
- **Permission matrix (full role × DocType)**:
  [Permission Audit](../security/permission-audit.md).
