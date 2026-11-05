# ops-runner

A deterministic runner that pushes only the exact bytes a coordinator verified, on the exact
base it verified them against. Every outcome comes back as one of six statuses. The runner
never repairs anything: no rebase, no `reset --hard`, no `clean`, no `stash`, no file edits,
no tests. It's a script; no model runs inside it.

It implements Evan's "Multi-Agent Git Ops Pipeline — Build Spec" (2026-11-05), with the
changes listed under [Where this differs from the spec](#where-this-differs-from-the-spec),
made after a security review.

## The one rule behind the design

The worktree is written by workers, so nothing in it is trusted, and that includes its
`.git/config` and its hooks. In the spec's original design the runner committed in the
worktree with the repo's hooks. The review showed what that allows:
- a worker-written hook can run with the runner's push credentials, and one pushed to `main`;
- a hook can rewrite the history under the verified tree;
- `pushurl` and `url.*.insteadOf` can redirect the push or fake an "already committed";
- `core.fsmonitor` can run a command during `git status`;
- `trailer.*` settings can change the trailers.

So the runner never runs git with the worktree's config or hooks:
- It works in **its own bare repository** (`OPS_REPO`), with no system config, its own
  global config (`OPS_GIT_CONFIG`, where its credential lives) and an empty hooks directory.
- It builds the tree as **`base_sha`'s tree plus the listed paths, read from the worktree
  as plain files** (`--work-tree`, with a throwaway index).
- It commits with **`commit-tree`**: the verified tree, parent `base_sha`, the runner's
  own message. No hooks run.
- It pushes **the new commit's SHA** to **`OPS_REMOTE_URL`**, never to a remote name the
  worktree defines. Then it confirms with `ls-remote`.
- It **never modifies the worktree.** On success or failure, `HEAD` stays at `base_sha`
  with the changes uncommitted.

The coordinator computes `tree_sha` the same way, in a scratch repository that borrows the
worktree's objects, so the same bytes give the same tree.

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
   - `main` and `master` are refused in any letter case.
   - Trailer-like lines in the commit text are refused.
   - An exclusive `flock` per target branch.
2. **Already committed?** Return `ALREADY_COMMITTED` only for a remote commit that has this
   `Task-Id` **and** the verified tree, parent `base_sha` and `Verified-Base: base_sha`. Any
   other commit with the same `Task-Id` → `MANIFEST_MISMATCH`.
3. **Stale base?**
   - Remote tip ≠ `base_sha` → `STALE_BASE`.
   - Worktree `HEAD` ≠ `base_sha` → `MANIFEST_MISMATCH`.
4. **Stage** `base_sha`'s tree plus the listed paths, in a private index: A and M from disk,
   D removed.
5. **Nothing stray.** Untracked files or unlisted changes → `MANIFEST_MISMATCH`. The staged
   set must equal `files_changed`.
6. **Tree matches.** `write-tree` must equal `tree_sha`.
7. **Secrets scan** (see below).
8. **Commit** with `commit-tree`: parent `base_sha`, subject and body from the manifest, then
   the `Task-Id` and `Verified-Base` trailers.
9. **Push** `<new commit SHA>:refs/heads/<branch>`, with no force and no `+` refspec.
   - If it's rejected, or `ls-remote` doesn't show the new commit, return `PUSH_REJECTED`.
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
private index into an empty directory. That's exactly what will be committed. Nothing in the
repo can configure the scan:
- **Repo config files:** `.gitleaks.toml`, `gitleaks.toml` and `.gitleaksignore` are renamed in
  the copy, so they're scanned as content but never act as config.
- **Allow comments:** `gitleaks:allow` comments are ignored (`--ignore-gitleaks-allow`).
- **Config:** the runner's own `gitleaks.toml` is used. It's gitleaks v8.21.2's 195 default
  rules **without the default global path allowlist**. That list would otherwise skip
  `gitleaks.toml`, `go.sum`, `vendor/`, `node_modules/`, lock files, images and PDFs entirely.
- **Encoded secrets:** base64 and similar encodings are decoded, up to 3 levels deep.
- **Size:** a file larger than `OPS_SCAN_MAX_MB` (default 50) is refused rather than skipped.
- **Whole files:** whole files are scanned, not just the changed lines.
- **Submodules:** submodule changes are refused.

A missing scanner fails closed: `HOOK_FAILED`, nothing committed.

## Where this differs from the spec

| Spec | Here | Why |
|---|---|---|
| Commit in the worktree "with the repo's hooks" | `commit-tree` in the runner's repo; no hooks | Worker-written hooks would run with the push credential. Their checks belong in `verify_cmd` and CI |
| Push to `origin` | Push the commit's SHA to `OPS_REMOTE_URL` | The worktree's config can redirect `origin` |
| On failure, `git reset --soft base_sha` in the worktree | The worktree is never touched | Nothing to undo; `HEAD` stays at `base_sha` |
| `ALREADY_COMMITTED` on any commit with the `Task-Id` | Only when tree, parent and `Verified-Base` match too | A `Task-Id` alone can be copied |
| Default gitleaks config | Default rules without the global path allowlist; decoding on | The allowlist skips whole classes of files |
| Coordinator recomputes `tree_sha` after verify | Hashes before verify and requires it unchanged after | A verify step could change the code |
| Every push error → `PUSH_REJECTED`, then rebase and retry | Same status; the coordinator alerts if the remote didn't move | Otherwise a server denial loops to the retry cap |

Pin the gitleaks version in the runner's image. The harness was run with v8.21.2.

## Tests

```bash
OPS_GITLEAKS=/path/to/gitleaks python3 tests/harness.py
```

Each case builds a throwaway bare repo as `origin`, plus a worktree clone, then runs the real
runner as a subprocess or the coordinator loop.
- **The spec's cases:** 1 to 12 are its acceptance tests.
- **Extra guards** found while building and in the security review:
  - E1–E3: a repo-supplied `.gitleaks.toml`, `gitleaks:allow` or `.gitleaksignore` can't
    silence a secret.
  - E4 and E18: `main` is refused, in any letter case.
  - E5: paths that escape the worktree are refused.
  - E6: worker-written hooks never run.
  - E7: hostile worktree git config is ignored (`pushurl`, `insteadOf`, `core.fsmonitor`,
    `trailer.*`).
  - E8: no scanner, which fails closed.
  - E9: two runners on one branch at once.
  - E10: a file listed as deleted but back on disk.
  - E11: a secret in a file gitleaks' default would skip by name.
  - E12: a base64-encoded key.
  - E13: a spoofed `Task-Id`.
  - E14: trailer-like text in the commit message.
  - E15: a verify step that edits the code.
  - E16: a bad lock directory, and an unreadable manifest, still give one JSON answer.
  - E17: a server denial alerts instead of looping.

## Environment

| Variable | Default |
|---|---|
| `OPS_REMOTE_URL` | **required:** the URL the runner fetches from and pushes to |
| `OPS_REPO` | `~/.ops-runner/repo`, created if missing |
| `OPS_GIT_CONFIG` | the runner's global git config, which holds its credential helper. Default: none |
| `OPS_LOCK_DIR` | `/var/lock` |
| `OPS_GITLEAKS` | `gitleaks` on `PATH` |
| `OPS_GITLEAKS_CONFIG` | `gitleaks.toml` next to the runner |
| `OPS_SCAN_MAX_MB` | `50` |
| `OPS_AUTHOR_NAME`, `OPS_AUTHOR_EMAIL` | `ops-runner` |
| `OPS_TEST_PAUSE_FILE` | tests only: makes the runner pause before the push |

Requires Python 3.9+ and git 2.32+.

The runner is only a real boundary once it is the only thing that can push: see
[docs/infrastructure.md](docs/infrastructure.md).
