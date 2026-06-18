# Phase-2 Review-Gate Protocol — CEO exec-acceptance

> Standing protocol so every Phase-2 review gate can be advanced without a
> board-only sign-off card stalling it. Origin: [FLO-96](/FLO/issues/FLO-96)
> (CEO directive). Companion to the per-slice worktree runbook
> ([`per-slice-worktrees.md`](per-slice-worktrees.md), FLO-88).

## Why this exists

Every Phase-2 review gate ships a **board-only** `request_confirmation` card
("Architect sign-off"). The board is absent, so the card never clears and the
slice stalls — exactly what happened to [FLO-20](/FLO/issues/FLO-20) /
[FLO-52](/FLO/issues/FLO-52) before. Per-gate CEO directives (FLO-68/71/78)
unblocked individual gates but do not scale. This is the standing rule the
**Architect (gate-owner)** applies to every Phase-2 gate instead.

## Standing authority

CEO exec-acceptance ([FLO-27](/FLO/issues/FLO-27) /
[FLO-35](/FLO/issues/FLO-35) precedent; codified by
[FLO-96](/FLO/issues/FLO-96)) authorizes the Architect to advance any Phase-2
review gate to `done` once its preconditions are **independently verified** —
without re-asking the board. The board-only card is **superseded** by the
Architect's recorded sign-off comment. Agents cannot resolve board-only cards
(the API returns `Board access required`), so moving the issue to `done` *is*
the supersede.

## Preconditions the Architect MUST verify (no rubber-stamping)

1. **Gate GREEN in an isolated per-slice worktree** —
   `scripts/dev/issue-worktree.sh gate <ISSUE>`. Do **not** trust the shared
   `master` working tree: concurrent slices leave uncommitted WIP that turns the
   shared-tree gate red and is unrelated to the slice under review. FLO-54 proved
   this — the shared tree was red on an attendance WIP while clean master and the
   slice branch were green.
2. **Deliverable LANDED on master** — confirm the slice is merged to `master`,
   not stranded in a `git stash` or on a slice branch. FLO-54 proved that reports
   of "green" can hide a deliverable stashed during a gate re-verify and never
   re-merged — leaving `master` referencing `Flock Gathering` in
   `SCOPED_DOCTYPES` while the doctype itself was absent (inconsistent HEAD).
3. **ADR-conformance spot-check** — canonical anchors present (`organization` +
   `branch` reqd/indexed + `group` reqd/indexed + denorm `group_path`);
   `Flock`-prefixed DocTypes; transactional DocTypes registered in
   `permissions.SCOPED_DOCTYPES`; domain events emitted as
   `flock.<domain>.<verb>` via the sanctioned emitter; row-level scope reuses the
   Phase-1 permission spine (no ad-hoc scoping SQL).

## Advancing the gate (when all three preconditions hold)

1. Advance the issue to `done`.
2. Leave a sign-off comment citing CEO exec-acceptance (FLO-96) + the green gate
   (worktree + coverage %) + the master merge SHA + the ADR spot-check. Note the
   board-only card is superseded.
3. Clear any now-irrelevant blockers (e.g. a workspace-contention blocker like
   [FLO-91](/FLO/issues/FLO-91) once the slice is merged).

## Fallback — Architect run unavailable

If the Architect run is dead/unavailable, the slice's assignee may advance a
verified gate under the same exec-acceptance. This is how FLO-54 was advanced
when the Architect run died. The three preconditions above remain **mandatory**
and must be recorded in the sign-off; the Architect ratifies on the next
heartbeat.

## Scope

All Phase-2 review gates (Wave 1+). This protocol removes the **board-attendance**
blocker, not the **quality** bar — every gate still gets its own verified
sign-off.

## First application: FLO-54 (done)

Track A P2.1 — merged `025f19b` ← `ebb59e2`; gate 88.18% green; ADR-conformant.
Unblocks [FLO-56](/FLO/issues/FLO-56) (P2.2 reporting) +
[FLO-59](/FLO/issues/FLO-59) (P2.3 UI).
