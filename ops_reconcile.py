#!/usr/bin/env python3
"""The ops runner: pushes only the exact bytes the coordinator verified, on the
exact base it verified them against, and reports every outcome as a fixed
status. It never repairs anything: no rebase, no reset --hard, no clean, no
stash, no file edits, no tests.

Usage:
    ops_reconcile.py MANIFEST.json      # or the manifest on stdin
Prints exactly one JSON object to stdout. Exit 0 for PUSHED and
ALREADY_COMMITTED, 1 for everything else. Logs go to stderr.

Checks run in this order, and the first failure returns:
  1. validate the manifest, take a lock per target branch
  2. already committed?  (a remote commit carries this Task-Id)
  3. stale base?         (remote tip != base_sha; worktree HEAD != base_sha)
  4. stage the listed paths only
  5. nothing stray       (every porcelain line fully staged; staged set == files_changed)
  6. tree matches        (git write-tree == tree_sha)
  7. secrets scan        (gitleaks on the staged contents, redacted, with the
                          runner's own config: the repo can't relax it)
  8. commit              (repo hooks run; never --no-verify)
  9. push                (no force, no + refspec)

Environment:
  OPS_REMOTE            remote name (default "origin")
  OPS_LOCK_DIR          lock directory (default /var/lock)
  OPS_GITLEAKS          gitleaks binary (default "gitleaks" on PATH)
  OPS_GITLEAKS_CONFIG   gitleaks config (default gitleaks.toml next to this file)
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile

PUSHED = 'PUSHED'
ALREADY_COMMITTED = 'ALREADY_COMMITTED'
STALE_BASE = 'STALE_BASE'
PUSH_REJECTED = 'PUSH_REJECTED'
HOOK_FAILED = 'HOOK_FAILED'
MANIFEST_MISMATCH = 'MANIFEST_MISMATCH'

SHA_RE = re.compile(r'^[0-9a-f]{40}$')
TASK_RE = re.compile(r'^[A-Za-z0-9._-]{1,200}$')
# A branch name git would accept, kept to a conservative set for the lock file name.
BRANCH_RE = re.compile(r'^(?!/)(?!.*//)(?!.*\.\.)(?!.*@\{)[A-Za-z0-9._/-]{1,200}(?<!/)(?<!\.lock)$')
TYPE_RE = re.compile(r'^[a-z]{2,20}$')
SCOPE_RE = re.compile(r'^[A-Za-z0-9._/-]{0,40}$')
FILE_STATUSES = ('A', 'M', 'D')
HERE = os.path.dirname(os.path.abspath(__file__))


def log(*a):
    print('[ops]', *a, file=sys.stderr, flush=True)


class Result(Exception):
    """Raised to end the run with a status."""

    def __init__(self, status, detail, commit_sha=None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.commit_sha = commit_sha


def git(wt, *args, check=True, env=None):
    """Run git in the worktree. Returns (returncode, stdout, stderr)."""
    p = subprocess.run(['git', '-C', wt, *args], capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed ({p.returncode}): {p.stderr.strip()}")
    return p.returncode, p.stdout, p.stderr


# ---------------------------------------------------------------- 1. validate

def validate(m):
    """Raises Result(MANIFEST_MISMATCH) on anything malformed. Returns the
    normalised manifest."""
    def bad(why):
        raise Result(MANIFEST_MISMATCH, f'invalid manifest: {why}')

    if not isinstance(m, dict):
        bad('not an object')
    for k in ('task_id', 'worktree', 'base_sha', 'target_branch', 'files_changed', 'tree_sha', 'commit'):
        if k not in m:
            bad(f'missing {k}')
    if not isinstance(m['task_id'], str) or not TASK_RE.match(m['task_id']):
        bad('task_id must be 1-200 characters of letters, digits, . _ -')
    for k in ('base_sha', 'tree_sha'):
        if not isinstance(m[k], str) or not SHA_RE.match(m[k]):
            bad(f'{k} must be a full 40-character lowercase SHA')
    if not isinstance(m['target_branch'], str) or not BRANCH_RE.match(m['target_branch']):
        bad('target_branch is not an acceptable branch name')
    if m['target_branch'] in ('main', 'master'):
        bad('the runner never pushes to main')
    wt = m['worktree']
    if not isinstance(wt, str) or not os.path.isabs(wt) or not os.path.isdir(wt):
        bad('worktree must be an existing absolute directory')
    wt = os.path.realpath(wt)

    files = m['files_changed']
    if not isinstance(files, list) or not files:
        bad('files_changed must be a non-empty list')
    seen = set()
    norm = []
    for f in files:
        if not isinstance(f, dict) or set(f) != {'path', 'status'}:
            bad('each files_changed entry is {path, status}')
        path, status = f['path'], f['status']
        if status not in FILE_STATUSES:
            bad(f'status of {path!r} must be one of A, M, D (renames are a D and an A)')
        if not isinstance(path, str) or not path or path.startswith('/') or '\\' in path or '\0' in path:
            bad(f'path {path!r} must be relative')
        parts = path.split('/')
        if any(p in ('', '.', '..') for p in parts) or parts[0] == '.git':
            bad(f'path {path!r} must stay inside the worktree')
        full = os.path.realpath(os.path.join(wt, path))
        if not (full == wt or full.startswith(wt + os.sep)):
            bad(f'path {path!r} resolves outside the worktree')
        if path in seen:
            bad(f'path {path!r} listed twice')
        seen.add(path)
        norm.append((status, path))

    c = m['commit']
    if not isinstance(c, dict):
        bad('commit must be an object')
    for k in ('type', 'summary', 'body'):
        if not isinstance(c.get(k), str):
            bad(f'commit.{k} must be a string')
    if not TYPE_RE.match(c['type']):
        bad('commit.type must be a short lowercase word')
    if not isinstance(c.get('scope', ''), str) or not SCOPE_RE.match(c.get('scope', '')):
        bad('commit.scope must be a short word')
    if not c['summary'].strip() or '\n' in c['summary'] or len(c['summary']) > 100:
        bad('commit.summary must be one line of 1-100 characters')
    if not c['body'].strip():
        bad('commit.body (the why) is required')
    if re.search(r'(?im)^\s*(task-id|verified-base)\s*:', c['body']):
        bad('commit.body must not carry Task-Id or Verified-Base trailers; the runner adds them')

    return {**m, 'worktree': wt, 'files': sorted(norm, key=lambda x: x[1])}


def lock(branch):
    d = os.environ.get('OPS_LOCK_DIR', '/var/lock')
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, 'ops-' + branch.replace('/', '__') + '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # held until the process exits
    return fd


# ---------------------------------------------------------------- the run

def run(m):
    remote = os.environ.get('OPS_REMOTE', 'origin')
    wt, branch, base, task = m['worktree'], m['target_branch'], m['base_sha'], m['task_id']
    tracking = f'refs/remotes/{remote}/{branch}'
    out = {'remote_sha': None}

    # 2. Already committed? Fetch the branch, then look for this Task-Id.
    code, _, err = git(wt, 'fetch', '--no-tags', remote, f'+refs/heads/{branch}:{tracking}', check=False)
    if code != 0:
        raise Result(MANIFEST_MISMATCH, f'cannot fetch {remote}/{branch} (the target branch must exist): {err.strip()}')
    _, tip, _ = git(wt, 'rev-parse', '--verify', tracking)
    tip = tip.strip()
    out['remote_sha'] = tip
    _, logs, _ = git(wt, 'log', tracking, '--format=%H %(trailers:key=Task-Id,valueonly,separator=%x2C)')
    for line in logs.splitlines():
        sha, _, ids = line.partition(' ')
        if task in [i.strip() for i in ids.split(',')]:
            raise Result(ALREADY_COMMITTED, f'{tracking} already has a commit with Task-Id {task}', commit_sha=sha)

    # 3. Stale base? Remote first, then the worktree.
    if tip != base:
        raise Result(STALE_BASE, f'remote tip {tip} is not base_sha {base}')
    _, head, _ = git(wt, 'rev-parse', '--verify', 'HEAD')
    if head.strip() != base:
        raise Result(MANIFEST_MISMATCH, f'worktree HEAD {head.strip()} is not base_sha {base}')

    # 4. Stage exactly the listed paths: A and M with git add, D with
    # git rm --cached (a no-op if the deletion is already staged; it never
    # touches the working copy, so a D file still on disk shows up as stray).
    try:
        stage(wt, m['files'])
    except RuntimeError as e:
        raise Result(MANIFEST_MISMATCH, f'cannot stage the listed paths: {e}')

    # 5. Nothing stray: every line fully staged, and the staged set is exactly the list.
    _, st, _ = git(wt, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignore-submodules=none')
    entries = st.split('\0')
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if not e:
            continue
        x, y, p = e[0], e[1], e[3:]
        if x == 'R' or x == 'C':
            i += 1  # the original path follows
        if x not in 'AMDR' or y != ' ':
            raise Result(MANIFEST_MISMATCH, f'stray or unstaged change: {x}{y} {p}')
    _, diff, _ = git(wt, 'diff', '--cached', '--name-status', '--no-renames', '-z', base)
    parts = [d for d in diff.split('\0') if d != '']
    staged = sorted(((parts[j], parts[j + 1]) for j in range(0, len(parts), 2)), key=lambda x: x[1])
    if staged != m['files']:
        raise Result(MANIFEST_MISMATCH, f'staged changes {staged} are not files_changed {m["files"]}')

    # 6. Tree matches: the authority.
    _, tree, _ = git(wt, 'write-tree')
    if tree.strip() != m['tree_sha']:
        raise Result(MANIFEST_MISMATCH, f'staged tree {tree.strip()} is not tree_sha {m["tree_sha"]}')

    # 7. Secrets scan, with the runner's own config: the repo's .gitleaks.toml,
    # .gitleaksignore and gitleaks:allow comments can't relax it.
    scan(wt, m['files'])

    # 8. Commit, with the repo's hooks.
    c = m['commit']
    subject = f"{c['type']}({c['scope']}): {c['summary']}" if c.get('scope') else f"{c['type']}: {c['summary']}"
    code, _, err = git(wt, 'commit', '-m', subject, '-m', c['body'],
                       '--trailer', f'Task-Id: {task}', '--trailer', f'Verified-Base: {base}', check=False)
    if code != 0:
        soft_reset(wt, base)
        raise Result(HOOK_FAILED, f'commit failed (a hook refused it?): {err.strip()[-2000:]}')
    _, new_sha, _ = git(wt, 'rev-parse', '--verify', 'HEAD')
    new_sha = new_sha.strip()
    # The commit must hold exactly the verified tree.
    _, ctree, _ = git(wt, 'rev-parse', f'{new_sha}^{{tree}}')
    if ctree.strip() != m['tree_sha']:
        soft_reset(wt, base)
        raise Result(MANIFEST_MISMATCH, f'a hook changed the commit: tree {ctree.strip()} is not tree_sha {m["tree_sha"]}')

    # 9. Push: no force, no + refspec.
    code, so, err = git(wt, 'push', '--porcelain', remote, f'HEAD:refs/heads/{branch}', check=False)
    if code != 0:
        soft_reset(wt, base)
        reason = 'non-fast-forward' if re.search(r'non-fast-forward|fetch first|\[rejected\]', so + err) else 'push error'
        raise Result(PUSH_REJECTED, f'{reason}: {(so + err).strip()[-2000:]}')

    raise Result(PUSHED, f'pushed {new_sha} to refs/heads/{branch}', commit_sha=new_sha)


def stage(wt, files):
    add = [p for s, p in files if s in ('A', 'M')]
    rm = [p for s, p in files if s == 'D']
    if add:
        git(wt, 'add', '--', *add)
    if rm:
        git(wt, 'rm', '--cached', '-q', '--ignore-unmatch', '--', *rm)


def soft_reset(wt, base):
    """Back to the verified base with the changes staged. Never --hard."""
    git(wt, 'reset', '--soft', base, check=False)


CONFIG_NAMES = ('.gitleaksignore', '.gitleaks.toml', '.gitleaks.tml')


def scan(wt, files):
    """Scan exactly what will be committed: the staged (index) contents of
    every added or modified file, copied into an empty directory. The copy is
    what gitleaks sees, so nothing in the repo can configure the scan: config
    and ignore files are renamed in the copy (still scanned as content),
    gitleaks:allow comments are ignored, and the runner's own config is used.
    Whole files are scanned, not only the changed lines."""
    binary = os.environ.get('OPS_GITLEAKS', 'gitleaks')
    config = os.environ.get('OPS_GITLEAKS_CONFIG', os.path.join(HERE, 'gitleaks.toml'))
    with tempfile.TemporaryDirectory() as tree, tempfile.TemporaryDirectory() as empty, \
            tempfile.NamedTemporaryFile('r', suffix='.json') as report:
        for status, path in files:
            if status == 'D':
                continue
            parts = path.split('/')
            parts = [p + '.scanned' if p in CONFIG_NAMES else p for p in parts]
            dest = os.path.join(tree, *parts)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            blob = subprocess.run(['git', '-C', wt, 'show', f':{path}'], capture_output=True)
            if blob.returncode != 0:
                raise Result(MANIFEST_MISMATCH, f'cannot read the staged {path}')
            with open(dest, 'wb') as f:
                f.write(blob.stdout)
        cmd = [binary, 'dir', '--redact', '--no-banner', '--ignore-gitleaks-allow',
               '--config', config, '--gitleaks-ignore-path', empty,
               '--report-format', 'json', '--report-path', report.name, '--exit-code', '3', tree]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            raise Result(HOOK_FAILED, f'secrets scanner not found ({binary}); nothing was committed')
        if p.returncode == 0:
            return
        if p.returncode == 3:
            try:
                findings = json.load(open(report.name))
            except Exception:
                findings = []
            summary = [{'rule': f.get('RuleID'),
                        'file': os.path.relpath(f.get('File', ''), tree).replace('.scanned', ''),
                        'line': f.get('StartLine'), 'secret': f.get('Secret'), 'match': f.get('Match')}
                       for f in findings]
            raise Result(HOOK_FAILED, json.dumps({'secrets_found': summary}))
        raise Result(HOOK_FAILED, f'secrets scanner failed ({p.returncode}): {p.stderr.strip()[-1000:]}')


# ---------------------------------------------------------------- main

def main(argv):
    raw = open(argv[1]).read() if len(argv) > 1 and argv[1] != '-' else sys.stdin.read()
    result = {'task_id': None, 'status': None, 'commit_sha': None, 'remote_ref': None, 'remote_sha': None, 'detail': ''}
    try:
        try:
            m = json.loads(raw)
        except json.JSONDecodeError as e:
            raise Result(MANIFEST_MISMATCH, f'invalid manifest: not JSON ({e})')
        if isinstance(m, dict) and isinstance(m.get('task_id'), str):
            result['task_id'] = m['task_id']
        m = validate(m)
        result['remote_ref'] = f"refs/heads/{m['target_branch']}"
        lock(m['target_branch'])
        try:
            run(m)
        except Result:
            raise
        except Exception as e:  # a git failure the checks didn't name
            raise Result(MANIFEST_MISMATCH, f'unexpected error: {e}')
    except Result as r:
        result['status'] = r.status
        result['detail'] = r.detail
        result['commit_sha'] = r.commit_sha
        if 'm' in locals() and isinstance(m, dict) and 'worktree' in m and os.path.isdir(m.get('worktree', '')):
            remote = os.environ.get('OPS_REMOTE', 'origin')
            code, tip, _ = git(m['worktree'], 'rev-parse', '--verify', '--quiet',
                               f"refs/remotes/{remote}/{m['target_branch']}", check=False)
            result['remote_sha'] = tip.strip() or None if code == 0 else None
    log(result['status'], '-', result['detail'][:300])
    print(json.dumps(result))
    return 0 if result['status'] in (PUSHED, ALREADY_COMMITTED) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
