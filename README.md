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
- `filter.*` with `.gitattributes` can run a command on every file, and change the bytes hashed;
- `trailer.*` settings can change the trailers;
- a git repository planted inside the worktree brings its own config and attributes, which
  git honours when it processes that directory, whatever the outer settings say.

So the runner never runs git with the worktree's config or hooks:
- Before git reads any file, it **refuses a worktree that contains any `.git` besides its
  own** (a planted repository, or a `.git` file pointing at one), and any listed path that's
  a directory.
- It works in **its own bare repository** (`OPS_REPO`), with no system config, its own
  global config (`OPS_GIT_CONFIG`, where its credential lives) and an empty hooks directory.
- It builds the tree as **`base_sha`'s tree plus the listed paths, read from the worktree
  as plain files** (`--work-tree`, with a throwaway index). No `.gitattributes` applies
  (`GIT_ATTR_SOURCE` is the empty tree), so no filter, `ident` or line-ending conversion: the
  bytes hashed are the bytes on disk.
- It commits with **`commit-tree`**: the verified tree, parent `base_sha`, the runner's
  own message. No hooks run.
- It pushes **the new commit's SHA** to **`OPS_REMOTE_URL`**, never to a remote name the
  worktree defines. Then it confirms with `ls-remote`.
- It **never modifies the worktree.** On success or failure, `HEAD` stays at `base_sha`
  with the changes uncommitted.

The coordinator computes `tree_sha` the same way, in a scratch repository that uses the
worktree's object store, so the same bytes give the same tree. Its rebase recovery is also
config-free:
1. **Fetch** the tip from `OPS_REMOTE_URL` (never the worktree's `origin`).
2. **Merge** with `git merge-tree` in the scratch repository.
3. **On a conflict**, leave the worktree untouched and send it back to the worker.
4. **Otherwise**, write the merged files with a two-tree `read-tree -u`, move `HEAD` with
   `update-ref` (hooks emptied) and reset the worktree's index to the new base.

No worktree filter, hook or fsmonitor runs at any step.

## Roles

| Role | Does | Never does |
|---|---|---|
| Worker | Writes code and tests in its own worktree | Commits to the remote; holds credentials |
| Coordinator | Splits the work, runs `verify_cmd`, computes `tree_sha`, writes the manifest, recovers from stale bases | Pushes |
| Ops runner (`ops_reconcile.py`) | Checks the manifest against the worktree, commits, pushes, returns a status | Edits files, runs tests, rebases, force-pushes, pushes to `main` |

## Use

```bash
python3 ops_reconcile.py manifest.json      # or the manifest on stdin
python3 ops_reconcile.py --bundle < bundle.json
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

## Bundle mode: the runner on its own machine

In production the runner lives on a machine of its own, where only it holds the push
credential. It can't read the coordinator's worktrees there, so the coordinator sends each
chunk as one bundle on stdin:

```json
{"manifest": {"task_id": "...", "base_sha": "...", "target_branch": "...", "files_changed": [...],
              "tree_sha": "...", "commit": {...}},
 "blobs": {"src/routes/auth.py": {"mode": "100644", "data": "<base64 of the file's bytes>"}}}
```

How bundles are built and checked:
- **Contents:** one blob for every listed `A` or `M` path and no other. The mode is `100644`,
  `100755` (executable) or `120000` (a symlink; the data is its target). `worktree` isn't needed.
- **No worktree at all:** the runner hashes the bytes straight into its own repository
  (`hash-object --no-filters`, `update-index --cacheinfo`) and never reads a file. No worktree
  config, attribute, filter, hook or planted repository takes part, and no worker can change
  the bytes mid-run. That closes the race the worktree-mode check leaves open.
- **Same checks after that:** the same tree, scan, commit and push checks as worktree mode run
  on those bytes. Bytes that don't hash to `tree_sha` are refused.
- **Coordinator side:** `coordinator.make_bundle(m)` reads each listed file once, after verify.
  It refuses to send anything if those bytes no longer hash to the verified `tree_sha`.
- **When it's used:** set `OPS_RUNNER_CMD` (for example `ssh -i KEY ops-runner@HOST`) and
  `run_ops` and `land_chunk` send bundles there.
- **On the runner's machine:** `deploy/ssh-entry.sh` is the SSH forced command. It loads
  `/etc/ops-runner/env` (a fixed path the client can't change) and runs
  `ops_reconcile.py --bundle`, ignoring whatever command the client asked for.
- **Size:** bundles over `OPS_BUNDLE_MAX_MB` (default 100) are refused.

### Push credential: the GitHub App helper

`gh_app_credential.py` is a git credential helper. On each push it signs a JWT with the
runner's GitHub App key and exchanges it for a one-hour installation token, limited to one
repository with `contents: write`. Nothing is cached. Put it in the git config that
`OPS_GIT_CONFIG` names:

```ini
[credential "https://github.com"]
    helper = !python3 /opt/ops-runner/gh_app_credential.py
```

It needs `OPS_GH_APP_ID`, `OPS_GH_INSTALLATION_ID`, `OPS_GH_APP_KEY` (the `.pem` path) and
`OPS_GH_REPO`. It signs with the `openssl` command. For any host but `github.com` it gives no
credential.

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
5. **Nothing stray.** Untracked files or unlisted changes → `MANIFEST_MISMATCH`. So is a
   `.gitignore` or `.gitattributes` that ignores itself. The staged set must equal
   `files_changed`.
6. **Tree matches.** `write-tree` must equal `tree_sha`.
7. **Secrets scan** (see below).
8. **Commit** with `commit-tree`: parent `base_sha`, subject and body from the manifest, then
   the `Task-Id` and `Verified-Base` trailers.
9. **Push** `<new commit SHA>:refs/heads/<branch>`, with no force and no `+` refspec.
   - `PUSHED` if the push reports the ref updated, or a fresh fetch shows the new commit on
     the branch (another commit may already be on top of it).
   - Otherwise `PUSH_REJECTED`.
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
private index into an empty directory, and on the **commit message** (with the `Task-Id` and
branch). That's exactly what will be committed.

**Every byte is read, or nothing is committed.** gitleaks reads files in 10,000-byte chunks
that don't overlap, and stops reading a file at the first chunk that looks binary (a `%PDF` or
`%!PS` prefix is enough). So the runner doesn't hand it whole files:
- **Windows:** each blob is copied as overlapping windows of 8,000 bytes, starting every 4,000
  bytes. Any match up to 4,000 bytes long lies whole inside one window.
- **Header and canary:** each window starts with a 1,024-byte header holding a fresh canary key.
  The header covers every offset file-type sniffing looks at.
- **Proof of reading:** the canary must be found in every window. If any window wasn't read,
  for whatever reason, nothing is committed.
- **Private keys:** the runner refuses any private key itself, because an encoded key block
  can be longer than a window. It looks for the words `PRIVATE KEY`:
  - as a PEM header;
  - base64-encoded at every alignment (standard or URL-safe);
  - URL- or form-encoded;
  - as hex.

  It matches those words rather than `-----BEGIN`, so base64 certificates (as in a kubeconfig)
  still pass.
- **Known limit:** an encoded secret inside a single base64 run longer than 4,000 bytes may not
  decode in any window. GitHub's own secret scanning is the backstop there.

Nothing in the repo can configure the scan:
- **Repo config files:** `.gitleaks.toml`, `gitleaks.toml` and `.gitleaksignore` are renamed in
  the copy, so they're scanned as content but never act as config. Each file is copied into
  its own directory, so a renamed copy can't overwrite another file.
- **Allow comments:** `gitleaks:allow` comments are ignored (`--ignore-gitleaks-allow`).
- **Config:** the runner's own `gitleaks.toml` is used. It's gitleaks v8.21.2's 195 default
  rules **without the default global path allowlist**. That list would otherwise skip
  `gitleaks.toml`, `go.sum`, `vendor/`, `node_modules/`, lock files, images and PDFs entirely.
  The `true|false|null` allowlist regex is grouped, so it matches only those exact values. The
  two per-rule path allowlists are removed too: a file named `@octokit/auth-token/README.md`
  would otherwise hide GitHub tokens.
- **Encoded secrets:** gitleaks 8.21.2 decodes base64 only, up to 3 levels deep. Other
  encodings are caught only for private keys, by the runner's own check.
- **Size:** a file larger than `OPS_SCAN_MAX_MB` (default 50, in millions of bytes) is refused
  rather than skipped.
- **File count:** a chunk listing more than `OPS_MAX_FILES` paths (default 5,000) is refused
  before any git work.
- **Archives:** gzip, zip (including `.docx`, `.xlsx` and `.jar`), bzip2, xz, 7z, zstd, rar and tar
  files are refused, since the scan can't see inside them. Add such files by hand, outside the
  pipeline.
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
| Coordinator rebases in the worktree | `merge-tree` in a scratch repository, then files written back | `git rebase` in the worktree runs its filters and hooks |
| (not covered) | Compressed archives refused | The scan can't see inside them |
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
  - E19: a renamed config file's scan copy can't collide with a real file.
  - E20: the `true|false|null` allowlist matches whole values only.
  - E21: a worktree filter, `ident` or hook never changes or runs on what's pushed.
  - E22: a `.gitignore` or `.gitattributes` that ignores itself is a stray.
  - E23: a push that landed is `PUSHED` even if another commit followed it at once.
  - E24 and E25: the coordinator's rebase runs no worktree filter, hook, fsmonitor or remote,
    and a conflict leaves the worktree as it was.
  - E26: gzip and tar files are refused.
  - E27: a planted git repository with its own filter is refused before git touches it, listed
    or not, in the runner and the coordinator.
- **Bundle mode:**
  - B1: a clean chunk is `PUSHED` and the same bundle again is `ALREADY_COMMITTED`.
  - B2: executable files, symlinks and deletions give the same tree as worktree mode.
  - B3: tampered bytes are refused.
  - B4: malformed bundles are refused, each with one JSON answer:
    - a missing or unlisted blob;
    - a submodule or directory mode;
    - bad base64;
    - a `.git` path;
    - a file where the base has a directory;
    - `main`.
  - B5: a secret is caught.
  - B6: the worktree is never read, even with a planted repository in it, or with it deleted.
  - B7: the size limit holds.
  - B8: a file changed after verify means no bundle is sent.
  - B9: the coordinator loop works through `OPS_RUNNER_CMD` and the SSH entry script,
    including stale-base recovery.
- **Bundle review fixes:**
  - R1: a key behind a PDF, RTF or PostScript prefix, binary magic later in the file, or a
    key across a 10 KB or window boundary or far into a large file, is found. A PDF-like
    file with no secret still passes.
  - R2: the size limit is the scan's limit.
  - R3: a key in the commit body, or a token-shaped `Task-Id`, is found.
  - R4: deeply nested JSON still gets one JSON answer.
  - R5: `.git` look-alikes (HFS-ignored characters, `GIT~1`, `.git.`) are refused.
  - R6: the file-count cap holds, and 1,000 files stage quickly.
  - R7: private key blocks are refused, including ones longer than a window. A GitHub token in
    `@octokit/auth-token/README.md` is found.
  - R8: a 4096-bit key is refused base64-encoded at any offset, URL-encoded, form-encoded or as
    hex. A base64 certificate still passes.
- **Credential helper:**
  - C1: the JWT is signed by the App key and names the App.
  - C1: the token asked for is one repository with `contents: write` only.
  - C1: other hosts get no credential.
  - C1: a failure leaks neither the key nor the JWT.

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
| `OPS_BUNDLE_MAX_MB` | `100`: the largest bundle read from stdin |
| `OPS_GH_APP_ID`, `OPS_GH_INSTALLATION_ID`, `OPS_GH_APP_KEY`, `OPS_GH_REPO` | the credential helper's App, installation, key path and repository |
| `OPS_MAX_FILES` | `5000`: the most paths one chunk may list |
| `OPS_RUNNER_CMD` | coordinator side: the command that reaches the runner's machine; bundles are sent when it is set |
| `OPS_TEST_PAUSE_FILE` | tests only: makes the runner pause before the push |

The coordinator reads `OPS_REMOTE_URL` too (or `land_chunk(..., remote_url=...)`). It needs
read access only.

Requires Python 3.9+, git 2.38+ (for `merge-tree --write-tree`) and, for the credential helper,
the `openssl` command.

The runner is only a real boundary once it is the only thing that can push: see
[docs/infrastructure.md](docs/infrastructure.md).
