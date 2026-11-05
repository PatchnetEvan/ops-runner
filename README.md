# ops-runner

A deterministic runner that pushes only the exact bytes a coordinator verified, on the exact
base it verified them against. Every outcome comes back as one of six statuses. The runner
never repairs anything: no rebase, no `reset --hard`, no `clean`, no `stash`, no file edits,
no tests. It's a script; no model runs inside it.

It implements Evan's "Multi-Agent Git Ops Pipeline — Build Spec" (2026-11-05).

## Roles

| Role | Does | Never does |
|---|---|---|
| Worker | Writes code and tests in its own worktree | Commits to the remote; holds credentials |
| Coordinator | Splits the work, runs `verify_cmd`, computes `tree_sha`, writes the manifest, recovers from stale bases | Pushes |
| Ops runner (`ops_reconcile.py`) | Checks the manifest against the worktree, commits, pushes, returns a status | Edits files, runs tests, rebases, force-pushes, pushes to `main` |

## Use

```bash
python3 ops_reconcile.py manifest.json      # or the manifest on stdin
```

It prints exactly one JSON object on stdout:
- **Exit codes:** 0 for `PUSHED` and `ALREADY_COMMITTED`, 1 for everything else.
- **Logs:** go to stderr.

The manifest:

```json
{
  "task_id": "chunk_04_auth_endpoints",
  "worktree": "/work/wt-chunk04",
  "base_sha": "<40-char sha the worktree was verified on>",
  "target_branch": "feature/auth-service",
  "files_changed": [
    {"path": "src/routes/auth.py", "status": "M"},
    {"path": "tests/test_auth.py", "status": "A"}
  ],
  "tree_sha": "<git write-tree after staging the listed paths>",
  "verify_cmd": "pytest tests/test_auth.py -q",
  "commit": {"type": "feat", "scope": "auth", "summary": "implement jwt refresh and revocation endpoints",
             "body": "Why-text written by the coordinator."}
}
```

How `files_changed` works:
- **Statuses:** each entry is `A`, `M` or `D`. A rename is a `D` and an `A`.
- **Authority:** `tree_sha` is what counts. `files_changed` is a cross-check.

The coordinator builds a manifest with `coordinator.manifest(...)`, which stages the listed paths
and hashes the tree. `coordinator.land_chunk(m)` is the retry loop from the spec.

## Order of checks (first failure returns)

1. **Validate and lock.**
   - Required fields, 40-character SHAs, paths inside the worktree.
   - `main` is refused.
   - An exclusive `flock` per target branch.
2. **Already committed?** If a remote commit carries this `Task-Id` trailer, return
   `ALREADY_COMMITTED`.
3. **Stale base?**
   - Remote tip ≠ `base_sha` → `STALE_BASE`.
   - Worktree `HEAD` ≠ `base_sha` → `MANIFEST_MISMATCH`.
4. **Stage** only the listed paths: `git add` for A and M, `git rm --cached` for D.
5. **Nothing stray.** Every porcelain line must be fully staged, and the staged set must equal
   `files_changed`.
6. **Tree matches.** `git write-tree` must equal `tree_sha`.
7. **Secrets scan** (see below).
8. **Commit,** with the repo's hooks and the `Task-Id` and `Verified-Base` trailers.
   - If a hook fails, soft-reset and return `HOOK_FAILED`.
   - If a hook changed the commit's tree, return `MANIFEST_MISMATCH`.
9. **Push,** with no force and no `+` refspec.
   - If it's rejected, soft-reset to `base_sha` and return `PUSH_REJECTED`.
10. **Report:** `PUSHED`.

| Status | Coordinator action |
|---|---|
| `PUSHED` | Mark the chunk done |
| `ALREADY_COMMITTED` | Mark the chunk done; no new write |
| `STALE_BASE` | Rebase recovery, re-verify, new `tree_sha`, resubmit |
| `PUSH_REJECTED` | Same as `STALE_BASE` |
| `HOOK_FAILED` | Send the redacted report back to the worker |
| `MANIFEST_MISMATCH` | Quarantine the worktree and alert Evan; never retry automatically |

## The secrets scan

gitleaks runs on the **staged contents** of every added or modified file, copied out of the
index into an empty directory. That's exactly what will be committed. The scan works this way
because a scan of the live worktree can be configured by the repo, and the repo is written by
workers:
- **Repo config files:** `.gitleaks.toml` and `.gitleaksignore` from the scanned directory are
  honoured by gitleaks. In the copy they're renamed, so they're still scanned as content but
  never act as config.
- **Allow comments:** `gitleaks:allow` comments are ignored (`--ignore-gitleaks-allow`).
- **Config:** the runner's own `gitleaks.toml` is used, which extends gitleaks' defaults.
- **Whole files:** whole staged files are scanned, not just the changed lines.

A missing scanner fails closed: `HOOK_FAILED`, nothing committed.

Pin the gitleaks version in the runner's image. The harness was run with v8.21.2.

## Tests

```bash
OPS_GITLEAKS=/path/to/gitleaks python3 tests/harness.py
```

Each case builds a throwaway bare repo as `origin`, plus a worktree clone, then runs the real
runner as a subprocess or the coordinator loop.
- **The spec's cases:** 1 to 12 are its acceptance tests.
- **Extra guards found while building:**
  - E1–E3: a repo-supplied `.gitleaks.toml`, `gitleaks:allow` or `.gitleaksignore` can't
    silence a secret.
  - E4: a manifest for `main` is refused.
  - E5: paths that escape the worktree are refused.
  - E6 and E7: a failing hook, and a hook that changes the commit.
  - E8: no scanner, which fails closed.
  - E9: two runners on one branch at once.
  - E10: a file listed as deleted but back on disk.

## Environment

| Variable | Default |
|---|---|
| `OPS_REMOTE` | `origin` |
| `OPS_LOCK_DIR` | `/var/lock` |
| `OPS_GITLEAKS` | `gitleaks` on `PATH` |
| `OPS_GITLEAKS_CONFIG` | `gitleaks.toml` next to the runner |

Requires Python 3.9+ and git 2.32+, for `--trailer`.

The runner is only a real boundary once it is the only thing that can push: see
[docs/infrastructure.md](docs/infrastructure.md).
