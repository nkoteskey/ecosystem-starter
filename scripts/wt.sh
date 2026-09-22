#!/usr/bin/env bash
# scripts/wt.sh — git worktree workflow helper (docs/DESIGN.md §3).
#
# Every session works in its own worktree at <worktree root>/<name>, on
# branch <worktree.branch_prefix><name>. The PRIMARY checkout (resolved
# below via `git rev-parse --git-common-dir`) is the landing checkout —
# this script never edits it beyond `worktree add`/`worktree remove`/
# `fetch` and the repo-level `core.hooksPath` config.
#
# The worktree root is, in order: $MERGE_GATE_WT_ROOT, merge-gate.toml
# `worktree.root`, or $HOME/dev/<repo basename>.wt.
#
# Commands:
#   wt.sh new <name> [--from <ref>] [--bootstrap] [--no-hooks] [--shared-target]
#   wt.sh rm <name> [--force] [--delete-branch]
#   wt.sh ls
#   wt.sh bootstrap [<name>] [--shared-target]
#
# Stdlib bash only (must run on macOS's system bash 3.2 and on Linux).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PY="$SCRIPT_DIR/mergegate_config.py"

cfg() { python3 "$CONFIG_PY" get "$@"; }
cfg_list() {
  CFG_LIST=()
  local line
  while IFS= read -r line; do
    if [ -n "$line" ]; then
      CFG_LIST+=("$line")
    fi
  done < <(cfg "$@")
}

if ! python3 "$CONFIG_PY" validate >/dev/null; then
  exit 1
fi

REMOTE="$(cfg repo.remote)"
MAIN="$(cfg repo.main_branch)"
MAIN_REF="$REMOTE/$MAIN"
BRANCH_PREFIX="$(cfg worktree.branch_prefix)"
HOOKS_PATH="$(cfg repo.hooks_path)"

usage() {
  cat <<EOF
Usage: wt.sh <command> [args]

Commands:
  new <name> [--from <ref>] [--bootstrap] [--no-hooks] [--shared-target]
      Create a worktree at <worktree root>/<name> on branch
      ${BRANCH_PREFIX}<name> (default --from $MAIN_REF, fetched first). If
      the branch already exists locally, it is reused instead (a plain
      \`worktree add\` onto the existing branch, no --from applied).

  rm <name> [--force] [--delete-branch]
      Remove a worktree. Refuses if dirty or the branch has unpushed
      commits, unless --force. Leaves the branch itself intact unless
      --delete-branch is given, in which case the local branch is also
      deleted if it is fully merged into $MAIN_REF (or force-deleted
      if --force is also given).

  ls
      List every worktree of this repo: path, branch, dirty file count,
      ahead/behind $MAIN_REF, and any claims held by that branch.

  bootstrap [<name>] [--shared-target]
      Run the --bootstrap step alone against an existing worktree (default:
      the current directory). Copies the gitignored directories listed in
      merge-gate.toml worktree.bootstrap_copy from the primary checkout,
      runs \`npm ci\` in worktree.bootstrap_npm_dirs that have a lockfile,
      and (re)installs git hooks.
EOF
}

# --------------------------------------------------------------------------
# Primary-checkout resolution
# --------------------------------------------------------------------------

resolve_primary_root() {
  local common_dir
  common_dir="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  if [ -z "$common_dir" ]; then
    common_dir="$(git rev-parse --git-common-dir)"
    case "$common_dir" in
      /*) : ;;
      *) common_dir="$(cd "$(dirname "$common_dir")" && pwd)/$(basename "$common_dir")" ;;
    esac
  fi
  dirname "$common_dir"
}

PRIMARY_ROOT="$(resolve_primary_root)"

resolve_wt_root() {
  if [ -n "${MERGE_GATE_WT_ROOT:-}" ]; then
    echo "$MERGE_GATE_WT_ROOT"
    return
  fi
  local configured
  configured="$(cfg worktree.root)"
  if [ -n "$configured" ]; then
    local stripped="${configured#\~/}"
    if [ "$stripped" != "$configured" ]; then
      echo "$HOME/$stripped"
    else
      echo "$configured"
    fi
    return
  fi
  echo "$HOME/dev/$(basename "$PRIMARY_ROOT").wt"
}

WT_ROOT="$(resolve_wt_root)"

valid_name() {
  case "$1" in
    ""|-*|*/*|*..*|.*) return 1 ;;
  esac
  git check-ref-format --branch "${BRANCH_PREFIX}$1" >/dev/null 2>&1
}

# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

# core.hooksPath is stored in the COMMON .git/config, so this one call
# applies to every worktree of this repo, not just the one being acted on
# right now. That is intended: one hooks install covers the whole worktree
# family, current and future.
install_hooks() {
  git -C "$PRIMARY_ROOT" config core.hooksPath "$HOOKS_PATH"
  echo "==> installed git hooks (core.hooksPath=$HOOKS_PATH; applies to every worktree of this repo)"
}

write_shared_target_envrc() {
  local target_wt="$1"
  # shellcheck disable=SC2016  # deliberately literal — expanded when SOURCED
  # later (by the user or direnv), not by this script.
  local line='export CARGO_TARGET_DIR=$HOME/.cargo-target-shared'
  cat >&2 <<'EOF'
WARNING: --shared-target opts this worktree into a SHARED CARGO_TARGET_DIR
($HOME/.cargo-target-shared). A shared target dir with concurrent builds of
the SAME crate from two worktrees can cross-contaminate build artifacts —
per-worktree target/ is the safe default. Only use this if you understand
the risk (e.g. a disk-constrained machine).
EOF
  if [ -f "$target_wt/.envrc.local" ] && grep -qF -- "$line" "$target_wt/.envrc.local"; then
    echo "==> $target_wt/.envrc.local already exports CARGO_TARGET_DIR — leaving it as-is (idempotent)"
    return 0
  fi
  echo "$line" >>"$target_wt/.envrc.local"
  echo "==> wrote $target_wt/.envrc.local (gitignore it) — this script does NOT export into your shell; source it yourself before building in that worktree."
}

do_bootstrap() {
  local wt_path="$1"
  echo "==> bootstrap: $wt_path"

  # 1. gitignored build resources — a fresh worktree lacks them. Each entry
  #    of worktree.bootstrap_copy is a glob relative to the repo root.
  cfg_list worktree.bootstrap_copy
  local pattern src rel dst
  for pattern in "${CFG_LIST[@]:+${CFG_LIST[@]}}"; do
    # shellcheck disable=SC2231  # the glob expansion is the point here
    for src in "$PRIMARY_ROOT"/$pattern; do
      [ -e "$src" ] || continue
      rel="${src#"$PRIMARY_ROOT"/}"
      dst="$wt_path/$rel"
      case "$rel" in
        ""|/*|..*|*/..*) echo "  refusing suspicious path '$rel'" >&2; continue ;;
      esac
      mkdir -p "$(dirname "$dst")"
      if [ -e "$dst" ]; then
        rm -rf "${dst:?}"
      fi
      cp -R "$src" "$dst"
      echo "  copied $rel"
    done
  done

  # 2. npm ci in every configured frontend dir that has a lockfile.
  cfg_list worktree.bootstrap_npm_dirs
  local t
  for t in "${CFG_LIST[@]:+${CFG_LIST[@]}}"; do
    if [ -f "$wt_path/$t/package-lock.json" ]; then
      echo "  npm ci: $t"
      (cd "$wt_path/$t" && npm ci)
    else
      echo "  skip npm ci: $t (no package-lock.json)"
    fi
  done

  # 3. hooks (idempotent, repo-level — see install_hooks).
  install_hooks

  echo "==> bootstrap complete: $wt_path"
}

# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

cmd_new() {
  if [ $# -eq 0 ]; then
    echo "wt.sh new: missing <name>" >&2
    exit 1
  fi
  local name="$1"
  shift
  if ! valid_name "$name"; then
    echo "wt.sh new: '$name' is not a valid worktree name" >&2
    exit 1
  fi
  local from_ref="$MAIN_REF" bootstrap=0 no_hooks=0 shared_target=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --from)
        from_ref="$2"
        shift 2
        ;;
      --bootstrap)
        bootstrap=1
        shift
        ;;
      --no-hooks)
        no_hooks=1
        shift
        ;;
      --shared-target)
        shared_target=1
        shift
        ;;
      *)
        echo "wt.sh new: unknown argument '$1'" >&2
        exit 1
        ;;
    esac
  done

  local wt_path="$WT_ROOT/$name"
  local branch="${BRANCH_PREFIX}$name"

  if [ -e "$wt_path" ]; then
    echo "wt.sh new: $wt_path already exists" >&2
    exit 1
  fi

  mkdir -p "$WT_ROOT"

  echo "==> fetching $REMOTE"
  git -C "$PRIMARY_ROOT" fetch "$REMOTE"

  if git -C "$PRIMARY_ROOT" show-ref --verify --quiet "refs/heads/$branch"; then
    echo "==> branch '$branch' already exists locally — reusing it (not creating fresh from $from_ref)"
    git -C "$PRIMARY_ROOT" worktree add "$wt_path" "$branch"
  else
    echo "==> creating worktree $wt_path on new branch $branch (from $from_ref)"
    git -C "$PRIMARY_ROOT" worktree add -b "$branch" "$wt_path" "$from_ref"
  fi

  if [ "$no_hooks" -eq 0 ]; then
    install_hooks
  fi

  if [ "$shared_target" -eq 1 ]; then
    write_shared_target_envrc "$wt_path"
  fi

  if [ "$bootstrap" -eq 1 ]; then
    do_bootstrap "$wt_path"
  fi

  echo "==> worktree ready: $wt_path (branch $branch)"
}

cmd_bootstrap() {
  local name="" shared_target=0
  if [ $# -gt 0 ] && [[ "$1" != --* ]]; then
    name="$1"
    shift
  fi
  while [ $# -gt 0 ]; do
    case "$1" in
      --shared-target)
        shared_target=1
        shift
        ;;
      *)
        echo "wt.sh bootstrap: unknown argument '$1'" >&2
        exit 1
        ;;
    esac
  done

  local wt_path
  if [ -n "$name" ]; then
    if ! valid_name "$name"; then
      echo "wt.sh bootstrap: '$name' is not a valid worktree name" >&2
      exit 1
    fi
    wt_path="$WT_ROOT/$name"
  else
    wt_path="$(pwd)"
  fi

  if [ ! -d "$wt_path" ]; then
    echo "wt.sh bootstrap: $wt_path does not exist" >&2
    exit 1
  fi

  if [ "$shared_target" -eq 1 ]; then
    write_shared_target_envrc "$wt_path"
  fi

  do_bootstrap "$wt_path"
}

cmd_rm() {
  if [ $# -eq 0 ]; then
    echo "wt.sh rm: missing <name>" >&2
    exit 1
  fi
  local name="$1"
  shift
  if ! valid_name "$name"; then
    echo "wt.sh rm: '$name' is not a valid worktree name" >&2
    exit 1
  fi
  local force=0 delete_branch=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --force)
        force=1
        shift
        ;;
      --delete-branch)
        delete_branch=1
        shift
        ;;
      *)
        echo "wt.sh rm: unknown argument '$1'" >&2
        exit 1
        ;;
    esac
  done

  local wt_path="$WT_ROOT/$name"
  local branch="${BRANCH_PREFIX}$name"

  if [ ! -d "$wt_path" ]; then
    echo "wt.sh rm: $wt_path does not exist" >&2
    exit 1
  fi

  if [ "$force" -eq 0 ]; then
    if [ -n "$(git -C "$wt_path" status --porcelain 2>/dev/null)" ]; then
      echo "wt.sh rm: $wt_path is dirty — refusing (use --force)" >&2
      exit 1
    fi
    git -C "$PRIMARY_ROOT" fetch "$REMOTE" --quiet 2>/dev/null || true
    if ! git -C "$wt_path" rev-parse --verify -q "$REMOTE/$branch" >/dev/null 2>&1; then
      echo "wt.sh rm: '$branch' has no upstream on $REMOTE (unpushed) — refusing (use --force)" >&2
      exit 1
    fi
    local unpushed
    unpushed="$(git -C "$wt_path" log --oneline "$REMOTE/$branch..$branch" 2>/dev/null || true)"
    if [ -n "$unpushed" ]; then
      echo "wt.sh rm: '$branch' has unpushed commits — refusing (use --force):" >&2
      echo "$unpushed" >&2
      exit 1
    fi
  fi

  echo "==> removing worktree $wt_path"
  local rm_args=("$wt_path")
  if [ "$force" -eq 1 ]; then
    rm_args=(--force "$wt_path")
  fi
  git -C "$PRIMARY_ROOT" worktree remove "${rm_args[@]}"
  git -C "$PRIMARY_ROOT" worktree prune

  if [ "$delete_branch" -eq 0 ]; then
    echo "==> removed. Branch '$branch' left intact (not deleted)."
    return 0
  fi

  git -C "$PRIMARY_ROOT" fetch "$REMOTE" --quiet 2>/dev/null || true
  if git -C "$PRIMARY_ROOT" merge-base --is-ancestor "$branch" "$MAIN_REF" 2>/dev/null; then
    git -C "$PRIMARY_ROOT" branch -d "$branch"
    echo "==> removed worktree and deleted local branch '$branch' (fully merged into $MAIN_REF)"
  elif [ "$force" -eq 1 ]; then
    git -C "$PRIMARY_ROOT" branch -D "$branch"
    echo "==> removed worktree and force-deleted local branch '$branch' (NOT confirmed merged into $MAIN_REF)"
  else
    echo "wt.sh rm: worktree removed, but '$branch' is not fully merged into $MAIN_REF — refusing --delete-branch (use --force to also force-delete it)" >&2
    exit 1
  fi
}

cmd_ls() {
  printf "%-55s %-25s %-6s %-16s %s\n" "PATH" "BRANCH" "DIRTY" "AHEAD/BEHIND" "CLAIMS"
  git -C "$PRIMARY_ROOT" worktree list --porcelain | awk '
    /^worktree / { if (path != "") { print path"\t"branch }; path = $2; branch = "" }
    /^branch /   { b = $2; sub("refs/heads/", "", b); branch = b }
    END { if (path != "") print path"\t"branch }
  ' | while IFS=$'\t' read -r path branch; do
    [ -z "$path" ] && continue
    local dirty
    dirty="$(git -C "$path" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"

    local ahead="?" behind="?"
    if [ -n "$branch" ]; then
      git -C "$path" fetch "$REMOTE" "$MAIN" --quiet 2>/dev/null || true
      if counts="$(git -C "$path" rev-list --left-right --count "$MAIN_REF...$branch" 2>/dev/null)"; then
        behind="${counts%%[[:space:]]*}"
        ahead="${counts##*[[:space:]]}"
      fi
    fi

    local claims
    claims="$(python3 "$SCRIPT_DIR/claims.py" list --json 2>/dev/null \
      | MERGE_GATE_WT_BRANCH="$branch" python3 -c '
import json, os, sys
branch = os.environ.get("MERGE_GATE_WT_BRANCH", "")
try:
    records = json.load(sys.stdin)
except Exception:
    records = []
mine = [r.get("service", "?") for r in records if r.get("branch") == branch]
print(",".join(mine) if mine else "-")
' 2>/dev/null || echo "?")"

    printf "%-55s %-25s %-6s %-16s %s\n" "$path" "${branch:-?}" "$dirty" "${ahead}/${behind}" "$claims"
  done
}

main() {
  local cmd="${1:-}"
  if [ $# -gt 0 ]; then
    shift
  fi
  case "$cmd" in
    new) cmd_new "$@" ;;
    rm) cmd_rm "$@" ;;
    ls) cmd_ls "$@" ;;
    bootstrap) cmd_bootstrap "$@" ;;
    -h | --help | help | "") usage ;;
    *)
      echo "wt.sh: unknown command '$cmd'" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
