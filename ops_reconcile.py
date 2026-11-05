#!/usr/bin/env python3
"""The ops runner: pushes only the exact bytes the coordinator verified, on the
exact base it verified them against, and reports every outcome as a fixed
status. It never repairs anything: no rebase, no reset --hard, no clean, no
stash, no file edits, no tests.

Usage:
    ops_reconcile.py MANIFEST.json      # or the manifest on stdin
    ops_reconcile.py --bundle           # a bundle on stdin (see below)
Prints exactly one JSON object to stdout. Exit 0 for PUSHED and
ALREADY_COMMITTED, 1 for everything else. Logs go to stderr.

The worktree is written by workers, so the runner never runs git with the
worktree's config or hooks (they could run commands with the runner's push
credentials, redirect the push or forge trailers). It works in its own
repository (OPS_REPO), reads the worktree's files as plain data, commits with
commit-tree (no hooks), and pushes to the URL it was configured with
(OPS_REMOTE_URL), never to a remote the worktree names. The worktree itself is
left exactly as it was.

Bundle mode (--bundle) is for a runner on its own machine. Stdin holds
    {"manifest": {...}, "blobs": {"<path>": {"mode": "100644", "data": "<base64>"}}}
with one blob for every listed A or M path and none other; "worktree" is not
needed. The runner never reads a worktree: it hashes those bytes straight into
its own repository, so no worker can change them mid-run, and no worktree
config, attribute, filter, hook or planted repository takes part. Modes are
100644, 100755 (executable) or 120000 (a symlink; data is its target).

Checks run in this order, and the first failure returns:
  1. validate the manifest, take a lock per target branch
  2. already committed?  a remote commit with this Task-Id, the verified tree,
                         Verified-Base = base_sha and parent base_sha
  3. stale base?         remote tip != base_sha; worktree HEAD != base_sha
  4. stage               base_sha's tree + the listed paths, in a private index
  5. nothing stray       no untracked or unlisted change; staged set == files_changed
  6. tree matches        write-tree == tree_sha
  7. secrets scan        gitleaks on exactly the staged contents, the runner's config
  8. commit              commit-tree: parent base_sha, the runner's own message
  9. push                the new commit's SHA, no force, no + refspec; confirmed
                         with ls-remote

Environment:
  OPS_REMOTE_URL        required: the URL the runner fetches from and pushes to
  OPS_REPO              the runner's own bare repo (default ~/.ops-runner/repo;
                        created if missing)
  OPS_GIT_CONFIG        the runner's global git config (its credential helper;
                        default: none)
  OPS_LOCK_DIR          lock directory (default /var/lock)
  OPS_GITLEAKS          gitleaks binary (default "gitleaks" on PATH)
  OPS_GITLEAKS_CONFIG   gitleaks config (default gitleaks.toml next to this file)
  OPS_SCAN_MAX_MB       largest file the scan reads (default 50); bigger = refused
  OPS_AUTHOR_NAME/EMAIL the commit identity (default "ops-runner")
  OPS_BUNDLE_MAX_MB     largest bundle read from stdin (default 100)
"""

import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import treebuild  # noqa: E402
from treebuild import run_git  # noqa: E402

PUSHED = 'PUSHED'
ALREADY_COMMITTED = 'ALREADY_COMMITTED'
STALE_BASE = 'STALE_BASE'
PUSH_REJECTED = 'PUSH_REJECTED'
HOOK_FAILED = 'HOOK_FAILED'
MANIFEST_MISMATCH = 'MANIFEST_MISMATCH'

SHA_RE = re.compile(r'^[0-9a-f]{40}$')
TASK_RE = re.compile(r'^[A-Za-z0-9._-]{1,200}$')
BRANCH_RE = re.compile(r'^(?!/)(?!.*//)(?!.*\.\.)(?!.*@\{)[A-Za-z0-9._/-]{1,200}(?<!/)(?<!\.lock)$')
PROTECTED = ('main', 'master')
TYPE_RE = re.compile(r'^[a-z]{2,20}$')
SCOPE_RE = re.compile(r'^[A-Za-z0-9._/-]{0,40}$')
FILE_STATUSES = ('A', 'M', 'D')
CONFIG_NAMES = ('.gitleaksignore', '.gitleaks.toml', '.gitleaks.tml', 'gitleaks.toml')
# A trailer-like line in the body, with any separator git might accept.
TRAILERISH = re.compile(r'(?im)^\s*(task-id|verified-base)\s*[:=#]')


def log(*a):
    print('[ops]', *a, file=sys.stderr, flush=True)


class Result(Exception):
    def __init__(self, status, detail, commit_sha=None):
        super().__init__(detail)
        self.status, self.detail, self.commit_sha = status, detail, commit_sha


# ---------------------------------------------------------------- 1. validate

def validate(m, bundle=False):
    def bad(why):
        raise Result(MANIFEST_MISMATCH, f'invalid manifest: {why}')

    if not isinstance(m, dict):
        bad('not an object')
    for k in ('task_id', 'base_sha', 'target_branch', 'files_changed', 'tree_sha', 'commit') + \
            (() if bundle else ('worktree',)):
        if k not in m:
            bad(f'missing {k}')
    if not isinstance(m['task_id'], str) or not TASK_RE.match(m['task_id']):
        bad('task_id must be 1-200 characters of letters, digits, . _ -')
    for k in ('base_sha', 'tree_sha'):
        if not isinstance(m[k], str) or not SHA_RE.match(m[k]):
            bad(f'{k} must be a full 40-character lowercase SHA')
    b = m['target_branch']
    if not isinstance(b, str) or not BRANCH_RE.match(b):
        bad('target_branch is not an acceptable branch name')
    if b.lower() in PROTECTED:
        bad('the runner never pushes to main')
    wt = None
    if not bundle:
        wt = m['worktree']
        if not isinstance(wt, str) or not os.path.isabs(wt) or not os.path.isdir(wt):
            bad('worktree must be an existing absolute directory')
        wt = os.path.realpath(wt)

    files = m['files_changed']
    if not isinstance(files, list) or not files:
        bad('files_changed must be a non-empty list')
    seen, norm = set(), []
    for f in files:
        if not isinstance(f, dict) or set(f) != {'path', 'status'}:
            bad('each files_changed entry is {path, status}')
        path, status = f['path'], f['status']
        if status not in FILE_STATUSES:
            bad(f'status of {path!r} must be one of A, M, D (renames are a D and an A)')
        if not isinstance(path, str) or not path or path.startswith('/') or '\\' in path or '\0' in path \
                or '\n' in path:
            bad(f'path {path!r} must be relative')
        parts = path.split('/')
        if any(p in ('', '.', '..') for p in parts) or any(p.lower() == '.git' for p in parts):
            bad(f'path {path!r} must stay inside the worktree')
        if not bundle:
            full = os.path.realpath(os.path.join(wt, path))
            if not full.startswith(wt + os.sep):
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
    if TRAILERISH.search(c['body']) or TRAILERISH.search(c['summary']):
        bad('commit text must not carry Task-Id or Verified-Base lines; the runner adds them')

    return {**m, 'worktree': wt, 'files': sorted(norm, key=lambda x: x[1])}


def read_bundle(raw):
    """Split a bundle into its manifest and {path: (mode, bytes)}. The blobs
    must be exactly the listed A and M paths."""
    def bad(why):
        raise Result(MANIFEST_MISMATCH, f'invalid bundle: {why}')

    if not isinstance(raw, dict) or set(raw) != {'manifest', 'blobs'}:
        bad('expected {"manifest": ..., "blobs": ...}')
    m = validate(raw['manifest'], bundle=True)
    given = raw['blobs']
    if not isinstance(given, dict):
        bad('blobs must be an object')
    want = {p for s, p in m['files'] if s != 'D'}
    if set(given) != want:
        missing, extra = sorted(want - set(given)), sorted(set(given) - want)
        bad(f'blobs must be exactly the listed A and M paths (missing {missing[:10]}, unlisted {extra[:10]})')
    blobs = {}
    for path, b in given.items():
        if not isinstance(b, dict) or set(b) != {'mode', 'data'}:
            bad(f'{path}: each blob is {{mode, data}}')
        if b['mode'] not in treebuild.BLOB_MODES:
            bad(f'{path}: mode must be one of {", ".join(treebuild.BLOB_MODES)}')
        try:
            data = base64.b64decode(b['data'], validate=True)
        except (binascii.Error, TypeError, ValueError):
            bad(f'{path}: data is not base64')
        blobs[path] = (b['mode'], data)
    return m, blobs


def lock(branch):
    d = os.environ.get('OPS_LOCK_DIR', '/var/lock')
    os.makedirs(d, exist_ok=True)
    name = 'ops-' + hashlib.sha256(branch.encode()).hexdigest()[:32] + '.lock'
    fd = os.open(os.path.join(d, name), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # held until the process exits
    return fd


# ---------------------------------------------------------------- the runner's repo

def ops_repo():
    d = os.environ.get('OPS_REPO') or os.path.expanduser('~/.ops-runner/repo')
    if not os.path.isdir(os.path.join(d, 'objects')):
        os.makedirs(d, exist_ok=True)
        subprocess.run(['git', 'init', '-q', '--bare', d], check=True, capture_output=True,
                       env=treebuild.isolated_env())
    return d


def env():
    return treebuild.isolated_env(os.environ.get('OPS_GIT_CONFIG'))


def remote_url():
    url = os.environ.get('OPS_REMOTE_URL')
    if not url:
        raise Result(MANIFEST_MISMATCH, 'the runner has no OPS_REMOTE_URL configured')
    return url


def ls_remote(repo, url, branch):
    p = run_git(repo, 'ls-remote', '--heads', url, f'refs/heads/{branch}', env=env(), check=False)
    if p.returncode != 0:
        return None
    for line in p.stdout.splitlines():
        sha, _, ref = line.partition('\t')
        if ref == f'refs/heads/{branch}':
            return sha
    return None


# ---------------------------------------------------------------- the run

def run(m, out, blobs=None):
    repo, url = ops_repo(), remote_url()
    wt, branch, base, task, tree_sha = m['worktree'], m['target_branch'], m['base_sha'], m['task_id'], m['tree_sha']
    tracking = 'refs/ops/' + hashlib.sha256(url.encode()).hexdigest()[:16] + '/' + branch

    # 2. Already committed? Fetch the branch from the configured URL.
    p = run_git(repo, 'fetch', '--no-tags', '--quiet', url, f'+refs/heads/{branch}:{tracking}', env=env(), check=False)
    if p.returncode != 0:
        raise Result(MANIFEST_MISMATCH, f'cannot fetch {branch} (the target branch must exist): {p.stderr.strip()}')
    tip = run_git(repo, 'rev-parse', '--verify', tracking, env=env()).stdout.strip()
    out['remote_sha'] = tip
    logs = run_git(repo, 'log', tracking, '--format=%H %T %P%x09%(trailers:key=Task-Id,valueonly,separator=%x2C)%x09'
                   '%(trailers:key=Verified-Base,valueonly,separator=%x2C)', env=env()).stdout
    for line in logs.splitlines():
        head, ids, vbase = (line.split('\t') + ['', ''])[:3]
        if task not in [i.strip() for i in ids.split(',')]:
            continue
        sha, ctree, *parents = head.split()
        if ctree == tree_sha and parents == [base] and vbase.strip() == base:
            raise Result(ALREADY_COMMITTED, f'{branch} already has {sha} for Task-Id {task}', commit_sha=sha)
        raise Result(MANIFEST_MISMATCH, f'Task-Id {task} is already used on {branch} by {sha}, '
                                        f'which is not this chunk (tree, parent or Verified-Base differ)')

    # 3. Stale base? Remote first, then the worktree (bundle mode has none).
    if tip != base:
        raise Result(STALE_BASE, f'remote tip {tip} is not base_sha {base}')
    if blobs is None:
        head = treebuild.worktree_head(wt)
        if head != base:
            raise Result(MANIFEST_MISMATCH, f'worktree HEAD {head} is not base_sha {base}')

    # 4-6. Stage base + the listed paths in a private index; no strays; tree matches.
    try:
        if blobs is None:
            tree, strays, staged, index, cleanup = treebuild.build_tree(repo, wt, base, m['files'], env=env())
        else:
            # Only the listed paths exist in a bundle: nothing can be stray.
            tree, staged, index, cleanup = treebuild.build_tree_from_blobs(repo, base, m['files'], blobs, env=env())
            strays = []
    except RuntimeError as e:
        raise Result(MANIFEST_MISMATCH, f'cannot stage the listed paths: {e}')
    try:
        if strays:
            raise Result(MANIFEST_MISMATCH, f'stray or unlisted changes: {strays[:20]}')
        if staged != m['files']:
            raise Result(MANIFEST_MISMATCH, f'staged changes {staged} are not files_changed {m["files"]}')
        if tree != tree_sha:
            raise Result(MANIFEST_MISMATCH, f'staged tree {tree} is not tree_sha {tree_sha}')

        # 7. Secrets scan of exactly the staged contents.
        scan(repo, index, m['files'])
    finally:
        cleanup()

    # 8. Commit: the verified tree on base_sha, with the runner's own message.
    c = m['commit']
    subject = f"{c['type']}({c['scope']}): {c['summary']}" if c.get('scope') else f"{c['type']}: {c['summary']}"
    message = f"{subject}\n\n{c['body'].strip()}\n\nTask-Id: {task}\nVerified-Base: {base}\n"
    ident = {'GIT_AUTHOR_NAME': os.environ.get('OPS_AUTHOR_NAME', 'ops-runner'),
             'GIT_AUTHOR_EMAIL': os.environ.get('OPS_AUTHOR_EMAIL', 'ops-runner@localhost'),
             'GIT_COMMITTER_NAME': os.environ.get('OPS_AUTHOR_NAME', 'ops-runner'),
             'GIT_COMMITTER_EMAIL': os.environ.get('OPS_AUTHOR_EMAIL', 'ops-runner@localhost')}
    new_sha = run_git(repo, 'commit-tree', tree_sha, '-p', base, env={**env(), **ident}, input=message).stdout.strip()
    # Belt and braces: the commit is exactly what was checked.
    info = run_git(repo, 'show', '-s', '--format=%T %P', new_sha, env=env()).stdout.split()
    if info != [tree_sha, base]:
        raise Result(MANIFEST_MISMATCH, f'commit {new_sha} is not tree {tree_sha} on {base}')

    # 9. Push that exact SHA: no force, no + refspec.
    pause_for_tests()
    ref = f'refs/heads/{branch}'
    p = run_git(repo, 'push', '--porcelain', url, f'{new_sha}:{ref}', env=env(), check=False)
    # --porcelain: "<flag>\t<from>:<to>\t<summary>"; ' ' updated, '*' new ref.
    accepted = p.returncode == 0 and any(
        len(f) >= 2 and f[0] in (' ', '*') and f[1] == f'{new_sha}:{ref}'
        for f in (line.split('\t') for line in p.stdout.splitlines()))
    out['remote_sha'] = ls_remote(repo, url, branch)
    if accepted or landed(repo, url, branch, tracking, new_sha):
        raise Result(PUSHED, f'pushed {new_sha} to {ref}', commit_sha=new_sha)
    text = (p.stdout + p.stderr).strip()[-2000:]
    raise Result(PUSH_REJECTED, f'push of {new_sha} not accepted (remote is at {out["remote_sha"]}): {text}')


def landed(repo, url, branch, tracking, sha):
    """Whether sha is on the remote branch now, even if the push's answer
    didn't say so (e.g. another commit landed right after ours)."""
    if run_git(repo, 'fetch', '--no-tags', '--quiet', url, f'+refs/heads/{branch}:{tracking}', env=env(),
               check=False).returncode != 0:
        return False
    return run_git(repo, 'merge-base', '--is-ancestor', sha, tracking, env=env(), check=False).returncode == 0


ARCHIVE_MAGIC = (b'\x1f\x8b', b'PK\x03\x04', b'PK\x05\x06', b'BZh', b'\xfd7zXZ\x00', b'7z\xbc\xaf\x27\x1c',
                 b'\x28\xb5\x2f\xfd', b'Rar!\x1a\x07')


def is_archive(blob):
    return blob.startswith(ARCHIVE_MAGIC) or blob[257:262] == b'ustar'


def pause_for_tests():
    """Test hook for the race between fetch and push: if OPS_TEST_PAUSE_FILE
    names a file, create it and wait (up to 30 s) until it is removed. It can
    only delay a push, never change one."""
    f = os.environ.get('OPS_TEST_PAUSE_FILE')
    if not f:
        return
    open(f, 'w').close()
    end = time.time() + 30
    while os.path.exists(f) and time.time() < end:
        time.sleep(0.05)


def scan(repo, index, files):
    """Scan exactly what will be committed: the staged blob of every added or
    modified file, copied into an empty directory, with the runner's own
    config. Nothing in the repo can configure the scan: config and ignore
    files are renamed in the copy (still scanned as content), gitleaks:allow
    comments are ignored, encoded secrets are decoded, and a file too large
    to scan is refused rather than skipped."""
    binary = os.environ.get('OPS_GITLEAKS', 'gitleaks')
    config = os.environ.get('OPS_GITLEAKS_CONFIG', os.path.join(HERE, 'gitleaks.toml'))
    max_mb = int(os.environ.get('OPS_SCAN_MAX_MB', '50'))
    with tempfile.TemporaryDirectory() as tree, tempfile.TemporaryDirectory() as empty, \
            tempfile.NamedTemporaryFile('r', suffix='.json') as report:
        for n, (status, path) in enumerate(files):
            if status == 'D':
                continue
            entry = run_git(repo, 'ls-files', '-s', '-z', '--', path, env=env(), index=index).stdout.split('\0')[0]
            mode = entry.split(' ')[0] if entry else ''
            if mode == '160000':
                raise Result(MANIFEST_MISMATCH, f'{path} is a submodule; the runner does not push submodule changes')
            blob = run_git(repo, 'show', f':{path}', env=env(), index=index, binary=True).stdout
            if len(blob) > max_mb * 1024 * 1024:
                raise Result(HOOK_FAILED, f'{path} is larger than {max_mb} MB and cannot be scanned; nothing was committed')
            if is_archive(blob):
                raise Result(HOOK_FAILED, f'{path} is a compressed archive, which the scan cannot see inside; nothing was committed')
            # Each file in its own numbered directory: two listed paths can never
            # land on the same copy (e.g. ".gitleaks.toml" renamed and a real
            # ".gitleaks.toml.scanned").
            parts = [p + '.scanned' if p in CONFIG_NAMES else p for p in path.split('/')]
            dest = os.path.join(tree, str(n), *parts)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as f:
                f.write(blob)
        cmd = [binary, 'dir', '--redact', '--no-banner', '--ignore-gitleaks-allow', '--max-decode-depth', '3',
               '--max-target-megabytes', str(max_mb + 1), '--config', config, '--gitleaks-ignore-path', empty,
               '--report-format', 'json', '--report-path', report.name, '--exit-code', '3', tree]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, 'GITLEAKS_CONFIG': config})
        except FileNotFoundError:
            raise Result(HOOK_FAILED, f'secrets scanner not found ({binary}); nothing was committed')
        if p.returncode == 0:
            return
        if p.returncode == 3:
            try:
                findings = json.load(open(report.name))
            except Exception:
                findings = []
            def listed(found):
                first = os.path.relpath(found or '', tree).split(os.sep)[0]
                return files[int(first)][1] if first.isdigit() and int(first) < len(files) else found
            summary = [{'rule': f.get('RuleID'), 'file': listed(f.get('File')), 'line': f.get('StartLine'),
                        'secret': f.get('Secret'), 'match': f.get('Match')} for f in findings]
            raise Result(HOOK_FAILED, json.dumps({'secrets_found': summary}))
        raise Result(HOOK_FAILED, f'secrets scanner failed ({p.returncode}): {p.stderr.strip()[-1000:]}')


# ---------------------------------------------------------------- main

def main(argv):
    result = {'task_id': None, 'status': None, 'commit_sha': None, 'remote_ref': None, 'remote_sha': None, 'detail': ''}
    out = {}
    bundle = '--bundle' in argv[1:]
    args = [a for a in argv[1:] if a != '--bundle']
    blobs = None
    try:
        try:
            if bundle:
                limit = int(os.environ.get('OPS_BUNDLE_MAX_MB', '100')) * 1024 * 1024
                raw = sys.stdin.buffer.read(limit + 1)
                if len(raw) > limit:
                    raise Result(MANIFEST_MISMATCH, f'invalid bundle: larger than {limit // 1024 // 1024} MB')
            else:
                raw = open(args[0]).read() if args and args[0] != '-' else sys.stdin.read()
            m = json.loads(raw)
        except (OSError, ValueError) as e:
            raise Result(MANIFEST_MISMATCH, f'invalid manifest: cannot read it as JSON ({e})')
        mm = m.get('manifest') if bundle and isinstance(m, dict) else m
        if isinstance(mm, dict) and isinstance(mm.get('task_id'), str):
            result['task_id'] = mm['task_id']
        if bundle:
            m, blobs = read_bundle(m)
        else:
            m = validate(m)
        result['remote_ref'] = f"refs/heads/{m['target_branch']}"
        try:
            lock(m['target_branch'])
            run(m, out, blobs)
        except Result:
            raise
        except Exception as e:  # anything the checks didn't name
            raise Result(MANIFEST_MISMATCH, f'unexpected error: {e}')
    except Result as r:
        result.update(status=r.status, detail=r.detail, commit_sha=r.commit_sha)
    result['remote_sha'] = out.get('remote_sha')
    log(result['status'], '-', result['detail'][:300])
    print(json.dumps(result))
    return 0 if result['status'] in (PUSHED, ALREADY_COMMITTED) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
