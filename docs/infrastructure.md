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

## The runner's image

- [ ] Pin gitleaks to v8.21.2. `gitleaks.toml` is that version's default rules without the
      global path allowlist; regenerate it if you change versions.
- [ ] Set `OPS_REMOTE_URL` to the repository's URL, and `OPS_GIT_CONFIG` to a git config that
      holds the runner's credential helper. Nothing else on the machine should hold it.
- [ ] Give the runner its own `OPS_REPO` directory (a bare repository it creates).
- [ ] Python 3.9+, git 2.38+.
- [ ] The runner reads the worktree's files, so it needs read access to the worktrees. It
      needs no write access to them.
- [ ] A lock directory the runner can write: `OPS_LOCK_DIR`, default `/var/lock`.
- [ ] Run `python3 tests/harness.py` in the image before first use. Every case must pass.
- [ ] The coordinator's environment sets `OPS_REMOTE_URL` too, with read-only access.

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
