#!/usr/bin/env bash
#
# Per-slice git worktree isolation for Flock OS (FLO-88).
#
# Gives each active issue/slice its own isolated git worktree on a dedicated
# branch off master, so concurrent slices never mutate the same working tree —
# the structural root cause of the FLO-85 merge-gate churn (two agents editing
# one shared `master` tree, e.g. `permissions.py` left `MM` mid-edit). The
# per-workspace merge gate runs scripts/qa-gate.sh INSIDE the slice's worktree,
# so the gate measures only that slice's tree.
#
# Why worktrees work here:
#   - `flock_os` is NOT pip-installed; `import flock_os` resolves via the
#     process cwd. So pytest run from <worktree>/ tests that worktree's code,
#     not the main tree's — isolation is real, not cosmetic.
#   - scripts/qa-gate.sh resolves ROOT via BASH_SOURCE (not cwd), so the same
#     script run from a worktree gates that worktree with zero path changes.
#   - `create` symlinks the main repo's `.venv` into the worktree so ruff/pytest
#     are available with no extra setup (the venv is code-independent tooling).
#
# Usage:
#   scripts/dev/issue-worktree.sh create  <issue-id> [base-ref]   # provision a slice worktree
#   scripts/dev/issue-worktree.sh gate   [issue-id]               # run qa-gate in the worktree
#   scripts/dev/issue-worktree.sh path   <issue-id>               # print the worktree path
#   scripts/dev/issue-worktree.sh list                            # list slice worktrees
#   scripts/dev/issue-worktree.sh merge  <issue-id>               # gate-then-merge slice -> master
#   scripts/dev/issue-worktree.sh remove <issue-id>               # teardown worktree + branch
#
# Layout:
#   - worktree dir: $FLOCK_WORKTREE_DIR/<issue-id>  (default: <repo>/../flock-os-worktrees)
#   - slice branch: slice/<issue-id>  (identifier lower-cased, e.g. slice/flo-54)
#
# Full runbook: docs/development/per-slice-worktrees.md
#
set -euo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"

# Resolve the MAIN repo root (the one owning this script, i.e. the shared clone
# that holds master). This script lives at <repo>/scripts/dev/, so its grandparent
# is the repo root. Follow symlinks so a worktree-side symlinked copy still finds
# the real main clone.
_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
	_dir="$(cd -P "$(dirname "$_src")" && pwd)"
	_src="$(readlink "$_src")"
	[[ $_src != /* ]] && _src="$_dir/$_src"
done
MAIN_ROOT="$(cd -P "$(dirname "$_src")/../.." && pwd)"

# Default worktree park is a sibling of the main repo so it never overlaps the
# working tree and survives a `git clean` in the main clone.
FLOCK_WORKTREE_DIR="${FLOCK_WORKTREE_DIR:-$(cd "$MAIN_ROOT/.." && pwd)/flock-os-worktrees}"
BASE_REF_DEFAULT="master"

die() { echo "$PROG: error: $*" >&2; exit 1; }

# Lower-case, slash-safe slice branch name from an issue identifier (FLO-54 -> flo-54).
branch_name() { echo "slice/$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"; }
worktree_path() { echo "$FLOCK_WORKTREE_DIR/$1"; }

ensure_git_repo() {
	git -C "$MAIN_ROOT" rev-parse --show-toplevel >/dev/null 2>&1 || die "$MAIN_ROOT is not a git repo."
}

# Inside a worktree, `git rev-parse --git-common-dir` points at the shared .git;
# the main worktree is the one whose --git-common-dir == --git-dir.
is_main_worktree() {
	local common dir
	common="$(git -C "$1" rev-parse --git-common-dir 2>/dev/null)"
	dir="$(git -C "$1" rev-parse --git-dir 2>/dev/null)"
	[[ "$common" == "$dir" ]]
}

cmd_create() {
	local issue="${1:-}" base="${2:-$BASE_REF_DEFAULT}"
	[[ -n "$issue" ]] || die "usage: $PROG create <issue-id> [base-ref]"
	ensure_git_repo
	local branch path
	branch="$(branch_name "$issue")"
	path="$(worktree_path "$issue")"

	# Refresh base ref from origin if available (non-fatal if offline/unconfigured).
	git -C "$MAIN_ROOT" rev-parse --verify --quiet "refs/remotes/origin/${base}" >/dev/null 2>&1 \
		&& git -C "$MAIN_ROOT" fetch --quiet origin "$base" 2>/dev/null \
		|| git -C "$MAIN_ROOT" rev-parse --verify --quiet "$base" >/dev/null 2>&1 \
		|| die "base ref '$base' not found."

	mkdir -p "$FLOCK_WORKTREE_DIR"

	if git -C "$MAIN_ROOT" worktree list --porcelain | grep -q "^worktree ${path}$"; then
		echo "Worktree already exists: $path"
		echo "    branch: $branch"
		return 0
	fi

	# Create the slice branch off base if missing; otherwise reuse it.
	if ! git -C "$MAIN_ROOT" rev-parse --verify --quiet "$branch" >/dev/null 2>&1; then
		git -C "$MAIN_ROOT" worktree add -b "$branch" "$path" "$base"
	else
		# Branch exists but worktree was removed — reattach.
		git -C "$MAIN_ROOT" worktree add "$path" "$branch"
	fi

	# Share the main repo's tooling venv (ruff/pytest) — code-independent, so safe
	# to reuse. This makes scripts/qa-gate.sh work unchanged inside the worktree.
	if [[ -x "$MAIN_ROOT/.venv/bin/ruff" || -x "$MAIN_ROOT/.venv/bin/pytest" ]]; then
		ln -sfn "$MAIN_ROOT/.venv" "$path/.venv"
	fi

	echo
	echo "Created slice worktree for $issue"
	echo "    path:   $path"
	echo "    branch: $branch  (off $base)"
	echo
	echo "Next:"
	echo "    cd \"$path\""
	echo "    # ...edit, then run the per-workspace gate:"
	echo "    \"$MAIN_ROOT/scripts/dev/issue-worktree.sh\" gate $issue"
}

# Resolve which worktree to act on: explicit issue-id, else the cwd's worktree.
resolve_worktree() {
	local issue="${1:-}"
	if [[ -n "$issue" ]]; then
		local path
		path="$(worktree_path "$issue")"
		[[ -d "$path" ]] || die "no worktree for $issue at $path (run: $PROG create $issue)."
		echo "$path"
	else
		# Infer the worktree from cwd.
		local cwd
		cwd="$(pwd)"
		git -C "$cwd" rev-parse --show-toplevel >/dev/null 2>&1 || die "not inside a git worktree (pass an <issue-id>)."
		is_main_worktree "$cwd" && die "cwd is the main worktree; pass an <issue-id> or run from a slice worktree."
		echo "$cwd"
	fi
}

cmd_gate() {
	local wt
	wt="$(resolve_worktree "${1:-}")"
	local venv_bin="$wt/.venv/bin"
	# Fall back to the main repo venv if the worktree has no tooling symlink.
	if ! [[ -x "$venv_bin/ruff" && -x "$venv_bin/pytest" ]]; then
		venv_bin="$MAIN_ROOT/.venv/bin"
	fi
	if ! [[ -x "$venv_bin/ruff" && -x "$venv_bin/pytest" ]]; then
		die "no ruff/pytest found in $wt/.venv or $MAIN_ROOT/.venv."
	fi
	echo "Running per-workspace gate in: $wt"
	# qa-gate.sh resolves ROOT via its own BASH_SOURCE -> gates THIS worktree.
	cd "$wt"
	PATH="$venv_bin:$PATH" bash "$wt/scripts/qa-gate.sh"
}

cmd_path() {
	local issue="${1:-}"
	[[ -n "$issue" ]] || die "usage: $PROG path <issue-id>"
	worktree_path "$issue"
}

cmd_list() {
	ensure_git_repo
	echo "Slice worktrees (park: $FLOCK_WORKTREE_DIR):"
	echo
	# git worktree list already shows all linked worktrees.
	git -C "$MAIN_ROOT" worktree list
}

cmd_merge() {
	local issue="${1:-}"
	[[ -n "$issue" ]] || die "usage: $PROG merge <issue-id>"
	ensure_git_repo
	local branch path
	branch="$(branch_name "$issue")"
	path="$(worktree_path "$issue")"
	[[ -d "$path" ]] || die "no worktree for $issue at $path."

	echo "==> [1/2] Per-workspace gate for $issue"
	if ! PATH="$MAIN_ROOT/.venv/bin:$PATH" bash "$0" gate "$issue"; then
		die "gate failed for $issue — merge aborted (fix in the worktree, then retry)."
	fi

	echo
	echo "==> [2/2] Merge $branch -> master (no-ff)"
	# Must merge from a checkout of master. Use the MAIN worktree (not the slice).
	# Bail if the main tree has uncommitted changes — never stomp concurrent work.
	if ! git -C "$MAIN_ROOT" diff --quiet || ! git -C "$MAIN_ROOT" diff --cached --quiet; then
		die "main tree ($MAIN_ROOT) has uncommitted changes — commit/stash them first."
	fi
	git -C "$MAIN_ROOT" checkout master
	git -C "$MAIN_ROOT" merge --no-ff "$branch" -m "merge: $issue slice into master (per-workspace gate green)"
	echo
	echo "Merged $branch -> master. Teardown when ready:"
	echo "    $0 remove $issue"
}

cmd_remove() {
	local issue="${1:-}"
	[[ -n "$issue" ]] || die "usage: $PROG remove <issue-id>"
	ensure_git_repo
	local branch path
	branch="$(branch_name "$issue")"
	path="$(worktree_path "$issue")"

	if [[ -d "$path" ]]; then
		# Refuse if the worktree has uncommitted work (don't silently destroy a slice).
		if ! git -C "$path" diff --quiet || ! git -C "$path" diff --cached --quiet; then
			die "worktree $path has uncommitted changes — commit/discard them before removal."
		fi
		# Drop the shared venv symlink so worktree removal doesn't touch the real venv.
		[[ -L "$path/.venv" ]] && rm -f "$path/.venv"
		git -C "$MAIN_ROOT" worktree remove "$path"
	else
		echo "    (worktree $path already gone)"
	fi

	# Best-effort branch deletion; keep the branch if unmerged so history isn't lost.
	if git -C "$MAIN_ROOT" rev-parse --verify --quiet "$branch" >/dev/null 2>&1; then
		if git -C "$MAIN_ROOT" branch --merged master | grep -q "$branch"; then
			git -C "$MAIN_ROOT" branch -d "$branch"
		else
			echo "    branch $branch is NOT merged into master — kept. Delete manually if unwanted:"
			echo "    git -C \"$MAIN_ROOT\" branch -D $branch"
		fi
	fi
	echo "Removed slice worktree for $issue."
}

usage() {
	cat <<USAGE
Per-slice git worktree isolation (FLO-88). Usage:

  $PROG create  <issue-id> [base-ref]   Provision an isolated slice worktree (off master)
  $PROG gate   [issue-id]              Run scripts/qa-gate.sh inside the slice worktree
  $PROG path   <issue-id>              Print the worktree path (for scripting)
  $PROG list                          List linked worktrees
  $PROG merge  <issue-id>              Gate-then-merge the slice into master
  $PROG remove <issue-id>              Teardown the worktree (and merged branch)

Env:
  FLOCK_WORKTREE_DIR  worktree park (default: <repo>/../flock-os-worktrees)

Full runbook: docs/development/per-slice-worktrees.md
USAGE
}

main() {
	local cmd="${1:-}"
	[[ $# -gt 0 ]] && shift
	case "$cmd" in
		create) cmd_create "$@" ;;
		gate) cmd_gate "$@" ;;
		path) cmd_path "$@" ;;
		list) cmd_list "$@" ;;
		merge) cmd_merge "$@" ;;
		remove) cmd_remove "$@" ;;
		""|-h|--help|help) usage ;;
		*) die "unknown command '$cmd' (try: $PROG --help)" ;;
	esac
}

main "$@"
