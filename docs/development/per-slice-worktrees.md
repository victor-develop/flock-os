# Per-slice git worktree isolation — runbook

> Structural fix for [FLO-88](/FLO/issues/FLO-88) / [FLO-85](/FLO/issues/FLO-85):
> the merge gate was going red because multiple Phase-2 slices edited **one
> shared `master` working tree** at the same time. This document is the
> per-slice workflow every agent (and human) should follow so that never
> happens again.

> **MANDATORY ([FLO-91](/FLO/issues/FLO-91), project-wide blocker).** Every
> heartbeat that edits, builds, gates, or tests code **MUST** do so inside its
> own isolated slice worktree provisioned by `scripts/dev/issue-worktree.sh`.
> Verified: two concurrent slice worktrees each retain their own uncommitted
> work and each reach a green `scripts/qa-gate.sh` independently. Never write
> working files into the shared `master` tree — that is the exact anti-pattern
> that corrupts sibling runs.

## Why this exists

Before this change, two slices landing forward-looking tests ahead of their
implementation mutated the same files (e.g. `permissions.py` left `MM`
mid-edit), so the shared-tree gate could not stay green regardless of how the
gate script was tuned. The fix is structural: **each active slice gets its own
git worktree on a dedicated branch**, and the merge gate runs **per worktree**.

The mechanism is plain `git worktree` — no new dependencies, no separate
clones. One shared `.git` object database, N independent working trees.

## TL;DR — the four commands

```bash
# from anywhere inside the repo
scripts/dev/issue-worktree.sh create FLO-54      # 1. provision an isolated slice tree
cd "$(scripts/dev/issue-worktree.sh path FLO-54)"# 2. work there (edit, commit on slice/flo-54)
scripts/dev/issue-worktree.sh gate   FLO-54      # 3. run the merge gate in THAT tree only
scripts/dev/issue-worktree.sh merge  FLO-54      # 4. gate-green -> merge slice into master
scripts/dev/issue-worktree.sh remove FLO-54      #    teardown when done
```

## The per-slice workflow

### 1. Create the worktree

```bash
scripts/dev/issue-worktree.sh create <ISSUE-ID> [base-ref]
# e.g. scripts/dev/issue-worktree.sh create FLO-54
```

- Creates `<repo>/../flock-os-worktrees/<ISSUE-ID>` as a linked worktree.
- Branches it as `slice/<issue-id>` (lower-cased) off `master` by default
  (override with a second arg, e.g. `origin/master`).
- Symlinks the main repo's `.venv` into the worktree so `ruff`/`pytest` are
  available with no extra setup. The venv is code-independent tooling, safe to
  share.
- Idempotent: re-running just reports the existing worktree.

Override the worktree park with `FLOCK_WORKTREE_DIR=/some/path` if you need it
elsewhere.

### 2. Work in the slice

```bash
cd "$(scripts/dev/issue-worktree.sh path <ISSUE-ID>)"
```

Edit, stage, and commit **on the `slice/<issue-id>` branch** exactly as normal.
Your commits land on the slice branch, never on shared `master` until you merge.

### 3. Run the per-workspace gate

```bash
scripts/dev/issue-worktree.sh gate <ISSUE-ID>
# or, if your cwd is already inside the worktree:
scripts/dev/issue-worktree.sh gate
```

This runs `scripts/qa-gate.sh` **inside the slice's worktree**. Because the gate
resolves its root via its own script path (`BASH_SOURCE`), it always measures
the tree it lives in — your slice, not the shared `master`. The `-m "not phase2"`
foundation scoping from [FLO-86](/FLO/issues/FLO-86) applies per-tree too.

> **Why the gate tests the right code:** `flock_os` is not pip-installed, so
> `import flock_os` resolves via the process cwd. Pytest run from a worktree
> therefore imports that worktree's code — isolation is real, not cosmetic.

### 4. Merge into master (only when green)

```bash
scripts/dev/issue-worktree.sh merge <ISSUE-ID>
```

This re-runs the gate (must be green), switches the **main** worktree to
`master`, and merges `slice/<issue-id>` with `--no-ff`. If the main tree has
uncommitted changes, the merge aborts — it never stomps concurrent work. Solve
that by doing *your* slice work in a worktree, not in the main tree.

### 5. Teardown

```bash
scripts/dev/issue-worktree.sh remove <ISSUE-ID>
```

Removes the worktree and deletes the slice branch **if it is merged** into
master. Unmerged branches are kept (with a manual-delete hint) so history is
never lost.

## Concurrency rules (read these)

- **One slice = one worktree = one branch.** Never edit `master` directly for
  in-flight feature work; provision a worktree.
- **Never run the gate from the main tree while a slice is mid-edit there** —
  that is the exact anti-pattern this fixes. Each slice gates its own tree.
- The main repo clone (`flock-os/`) stays on `master` and is the **merge
  target** only. Keep it clean: the `merge` command refuses to run if the main
  tree is dirty.
- Worktrees share one `.git` object store, so a `git fetch`/commit in any tree
  is visible to all. Branches are independent; working trees are not.

## Inspecting state

```bash
scripts/dev/issue-worktree.sh list          # all linked worktrees
git worktree list                            # raw git view
git branch | grep '^slice/'                  # active slice branches
```

## Long-term topology note

Git worktrees are the working isolation **now**. If a native Paperclip
per-issue execution-workspace provisioning becomes the preferred long-term
topology, it would layer on top of (not replace) this: the operator/adapter
would provision a worktree per issue via this same helper at workspace
spin-up, so the create/gate/merge/teardown lifecycle already matches what a
native workspace would need. No application code depends on absolute paths,
and `qa-gate.sh` is path-relative, so nothing in the gate requires operator
config to move to that model.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `no ruff/pytest found in .../.venv` | The main repo `.venv` is missing tooling — run `pip install -r requirements-dev.txt` in the main repo, then re-run. |
| `worktree ... has uncommitted changes` on remove | Commit or discard your slice work first; removal refuses to destroy uncommitted edits. |
| `main tree has uncommitted changes` on merge | The shared `master` tree is dirty — commit/stash it. (Better: do the work in a worktree.) |
| `branch slice/x is NOT merged` | Teardown kept the branch on purpose. `git branch -D slice/x` to force-delete. |
| Worktree path collides | Set `FLOCK_WORKTREE_DIR` to a custom park, or pass a unique `<ISSUE-ID>`. |
