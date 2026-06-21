# Attendee Guide — Flock OS

Welcome! This guide covers what you — a **member** or **visitor** — need to do as
an attendee: register for an event, join a fun attendance session, and what to
expect on event day. It is intentionally short and text-first.

> **Admins/leaders:** see the [Branch Admin Guide](./admin-guide.md).

---

## 1. Your account

You have a `Flock Member` profile tied to your login. The key things to know:

- Your **home branch** (`Flock Member.branch`) and the **groups** you belong to
  (via `Flock Group Member` rows) decide which events and announcements you can
  see.
- Your `Flock Member.status` is one of:
  - **`Member`** — you are a joined member.
  - **`Pre-Member`** — you are on the way to joining.
  - **`Visitor`** — you are visiting (you can still attend and register).
- Visitors and pre-members are welcome at events and engagement sessions.

If you cannot see an event you expected, ask your group leader to confirm your
`Flock Group Member` row is `Active` and you are in the right group.

---

## 2. Register for a one-time event

For one-time events (conferences, joint gatherings), you register in advance.

1. Open the event link your leader shared, or find it in your event list.
2. Register before the window closes. The event's registration window is set by
   `registration_opens_on` and `registration_closes_on`.
3. Your registration (`Flock Event Registration`) starts at **`Registered`**.
   Your status can be:
   - **`Registered`** — you have a seat.
   - **`Waitlisted`** — the event is full; you get a seat if one opens up
     (automatic, oldest first).
   - **`Checked-in`** — you checked in at the event.
   - **`Cancelled`** — you cancelled (frees your seat).
   - **`No-show`** — you registered but did not attend.
4. **Capacity:** if the event reaches its `registration_capacity`, new
   registrations are `Waitlisted`. You will be promoted to `Registered`
   automatically when a seat frees.
5. **Eligibility:** you can only register if you are inside the event's
   registration scope (`Own Group`, `Group Subtree`, `Branch`, `Branch Subtree`,
   `Org-wide`, or `Invited Only`). If you were sent an invitation
   (`Flock Event Invitation`), use the RSVP link in that invite.

---

## 3. Join a fun attendance session

Instead of signing a paper list, your leader runs a **live game** or **live
questionnaire**. Playing it records you as **present** — that is the attendance.

### 3.1 How to join

1. Your leader shares one of:
   - A **link** to `/engage?session=<id>` (click it).
   - A **6-digit room code**. Go to `/engage` and type the code.
   - A **QR code**. Scan it to open `/engage`.
2. If prompted, **sign in** (or continue as a guest). You will be returned to the
   session automatically.
3. The session is live when the leader opens it. You will see the first round.

### 3.2 What you will play

Depending on the session's `engagement_type` and `kind`:

- **Games** (`engagement_type` = `game`):
  - `quiz_race` — a fast quiz; be quick and correct.
  - `tap_burst` — tap as fast as you can.
  - `reaction` — react to prompts.
  - `bingo` — complete a bingo card.
  - `team_challenge` — team-based challenge.
- **Questionnaires** (`engagement_type` = `questionnaire`):
  - `poll` — vote on options.
  - `word_cloud` — submit a word/phrase.
  - `qa` — ask a question or upvote others'.
  - `pulse` — rate something on a slider.

### 3.3 It is timed

Each round has a `duration_seconds` and starts on a server-published tick
(`starts_at`) so everyone plays in sync. Answer before the round ends. There is a
short grace period (`grace_seconds`, default 30s) for slow connections.

### 3.4 Accessibility

Sessions support an **accessibility mode** (larger tap targets, calmer rounds).
Look for the accessibility toggle on the `/engage` page if you need it. Sessions
may also be available in multiple languages.

### 3.5 You are counted when it closes

You do not need to do anything special to be marked present. When the leader
**closes** the session, everyone who participated is recorded as an attendee
(a `Flock Attendance Record` with your `Flock Member` reference). The
session lifecycle the leader controls is:
`draft → scheduled → open → closing → closed`.

---

## 4. Event day — what to expect

1. **Before:** register if it is a one-time event (see [§2](#2-register-for-a-one-time-event)).
   For routine gatherings (weekly services, cell meetings) you do not need to
   register — your leader will report attendance, or run a fun session.
2. **At the event:**
   - For a **fun attendance** event: join the session via the link, room code, or
     QR code (see [§3](#3-join-a-fun-attendance-session)). Play the rounds. You
     are marked present when the session closes.
   - For a **self check-in** event: use the check-in action on your registration
     to mark yourself `Checked-in`.
   - For a **leader-reported** event: just show up — your group leader records
     your attendance.
3. **After:** your attendance is recorded against the `Flock Gathering`. Counts
   roll up to your branch's reporting. You do not need to do anything else.

---

## Troubleshooting

- **"I cannot see the event."** Check with your leader that your
  `Flock Group Member` row is `Active` and you are in a group inside the event's
  registration scope.
- **"The room code does not work."** Make sure the session is **open** (the
  leader must open it before you can join). Codes are 6 digits.
- **"I registered but was waitlisted."** The event was full. You will be
  promoted automatically if a seat opens.
- **"I joined late."** Rounds are timed and sync to a server tick. Join on time;
  there is only a short grace window.
- **"My connection dropped."** The app queues your last response and replays it
  when you reconnect, within the grace window.

---

## Where to go next

- **Admins/leaders:** [Branch Admin Guide](./admin-guide.md).
- **All docs:** [User Docs index](./README.md).
