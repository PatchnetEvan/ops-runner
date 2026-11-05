# Infrastructure checklist

Credentials and GitHub rules are the real boundary. The coordinator's prompts and deny rules
are only a backup. Until every box below is ticked, the coordinator can still push directly
(its git credentials and the GitHub connector), so the runner is a discipline, not a control.

For Evan to set up; the coordinator can't change credentials or repo settings.

## Push credentials

- [ ] **The runner gets its own identity.** Prefer a GitHub App installation token, scoped to
      the one repository with `contents: write`, or else a deploy key with write access.
      Fine-grained tokens can't be limited to branches: branch limits come from the ruleset
      below.
- [ ] **The runner runs where only it holds that credential:** a separate container or VM. The
      coordinator hands it a manifest and reads back the JSON, and never sees the credential.
- [ ] **The coordinator's and workers' environments have no push credential:** no SSH keys, no
      `GITHUB_TOKEN`, no credential helper.
- [ ] **The coordinator's GitHub connector can't write either.** Today the Claude Code session's
      GitHub app can push files and merge pull requests without git, and a `git push` deny rule
      doesn't cover that. Either limit the app to read access on this repository, or accept
      that, and rely on the ruleset to block what matters.

## GitHub ruleset (on `main` and `feature/*`)

- [ ] Block force pushes and branch deletion on both.
- [ ] `main`: changes only through a pull request; nobody pushes to it directly.
- [ ] `main`: required status checks on pull requests. CI is still the integration gate across
      all chunks.
- [ ] `feature/*`: allow the runner's identity to update. Optionally restrict updates to it
      alone.
- [ ] **Signed commits:** decide whether `feature/*` requires them. If it does, the runner
      gets its own signing key, scoped like the push credential.

## The runner's machine (bundle mode)

- [ ] A machine only the runner uses, with a user `ops-runner`.
- [ ] Python 3.9+, git 2.38+, `openssl`, and gitleaks pinned to v8.21.2. `gitleaks.toml` is that
      version's default rules without the global path allowlist; regenerate it if you change
      versions.
- [ ] This repository checked out at `/opt/ops-runner`, read-only for `ops-runner`.
- [ ] The GitHub App's `.pem` key, readable only by `ops-runner` (`chmod 600`).
- [ ] `/etc/ops-runner/env`, readable only by `ops-runner`, setting:
      - `OPS_REMOTE_URL`;
      - `OPS_REPO`, `OPS_LOCK_DIR`, `OPS_GITLEAKS`;
      - `OPS_GIT_CONFIG`, a git config whose credential helper is `gh_app_credential.py`;
      - `OPS_GH_APP_ID`, `OPS_GH_INSTALLATION_ID`, `OPS_GH_APP_KEY`, `OPS_GH_REPO`;
      - `OPS_AUTHOR_NAME`, `OPS_AUTHOR_EMAIL`.
- [ ] `~ops-runner/.ssh/authorized_keys` holds the coordinator's key, and only with
      `restrict,command="/opt/ops-runner/deploy/ssh-entry.sh"`. That key can submit a bundle
      and read the answer, nothing else.
- [ ] As `ops-runner`, run `python3 tests/harness.py`. Every case must pass.
- [ ] The coordinator's environment sets `OPS_RUNNER_CMD` (for example
      `ssh -i KEY ops-runner@HOST`) and `OPS_REMOTE_URL` (read-only).
- [ ] **No submodules in worktrees.** The coordinator still refuses any `.git` inside a
      worktree other than its own.

Bundle mode never reads a worktree, so no worker can change what's pushed during a run. If the
runner ever runs in worktree mode instead (without `--bundle`), the worker must be stopped
while its chunk lands. There, the planted-repository check runs a few milliseconds before git
reads the files.

## Claude Code deny rules (coordinator and workers)

The backup layer. Workers and the coordinator never push, so deny `git push` outright rather
than trying to match force flags:

```json
{ "permissions": { "deny": [
    "Bash(git push:*)",
    "Bash(git commit --no-verify:*)",
    "Bash(git reset --hard:*)"
]}}
```

Check the pattern syntax against the installed Claude Code version. These rules only cover the
Bash tool, not the GitHub connector.

## Not covered by the runner

Deploys, D1 migrations and secrets (wrangler) aren't git pushes, and this pipeline doesn't
govern them. Until there's a separate rule, they stay as they are: only on Evan's explicit go,
run by the coordinator.
