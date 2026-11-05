"""Coordinator-side helpers for the ops runner.

The coordinator verifies a chunk, computes its tree_sha, writes the manifest
and runs the runner. On STALE_BASE or PUSH_REJECTED it owns the recovery:
rebase onto the remote tip read ONCE, re-verify, recompute tree_sha, resubmit.
A rebase conflict or a failed re-verify goes back to the worker; a
MANIFEST_MISMATCH quarantines the worktree and alerts Evan.

Before each run, the worktree's HEAD is exactly base_sha and the chunk's changes
are uncommitted. The runner makes the only real commit.
"""

import json
import os
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_ATTEMPTS = 5


def git(wt, *args, check=True):
    p = subprocess.run(['git', '-C', wt, *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p


def paths_of(m):
    return [f['path'] for f in m['files_changed']]


def stage(m):
    """Stage the listed paths: A and M with git add, D with git rm --cached."""
    add = [f['path'] for f in m['files_changed'] if f['status'] in ('A', 'M')]
    rm = [f['path'] for f in m['files_changed'] if f['status'] == 'D']
    if add:
        git(m['worktree'], 'add', '--', *add)
    if rm:
        git(m['worktree'], 'rm', '--cached', '-q', '--ignore-unmatch', '--', *rm)


def stage_and_write_tree(m):
    """Stage the listed paths (deletions too) and return git write-tree."""
    stage(m)
    return git(m['worktree'], 'write-tree').stdout.strip()


def head(wt):
    return git(wt, 'rev-parse', '--verify', 'HEAD').stdout.strip()


def fetch_tip(wt, branch, remote='origin'):
    """The remote tip, read once."""
    git(wt, 'fetch', '--no-tags', remote, f'+refs/heads/{branch}:refs/remotes/{remote}/{branch}')
    return git(wt, 'rev-parse', '--verify', f'refs/remotes/{remote}/{branch}').stdout.strip()


def rebase_worktree(m, new_base):
    """Move the uncommitted chunk onto new_base. True on success, with the
    changes uncommitted on new_base. False on a conflict, with the rebase
    aborted and the changes uncommitted on the old base again."""
    wt, old = m['worktree'], m['base_sha']
    stage(m)
    git(wt, 'commit', '-q', '-m', 'wip', '--no-gpg-sign')
    p = git(wt, 'rebase', new_base, check=False)
    if p.returncode != 0:
        git(wt, 'rebase', '--abort', check=False)
        git(wt, 'reset', '--soft', old)
        return False
    git(wt, 'reset', '--soft', new_base)
    return True


def run_verify(wt, verify_cmd):
    return subprocess.run(verify_cmd, shell=True, cwd=wt).returncode == 0


def run_ops(m, runner=None, env=None):
    """Run the ops runner on manifest m; returns its JSON result."""
    runner = runner or [sys.executable, os.path.join(HERE, 'ops_reconcile.py')]
    p = subprocess.run(runner, input=json.dumps(m), capture_output=True, text=True, env=env)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        # No result (killed, crashed): treat as a lost response. The next
        # run's idempotency check tells whether the push landed.
        return {'status': 'NO_RESULT', 'detail': p.stderr.strip()[-500:]}


def land_chunk(m, run_ops=run_ops, send_to_worker=None, quarantine=None, alert=None, done=None,
               max_attempts=MAX_ATTEMPTS):
    """The coordinator loop from the build spec. Returns ('done' | 'worker' |
    'quarantined' | 'gave_up', detail)."""
    send_to_worker = send_to_worker or (lambda m, why: None)
    quarantine = quarantine or (lambda wt: None)
    alert = alert or (lambda m, r: None)
    done = done or (lambda m, sha: None)
    for _ in range(max_attempts):
        r = run_ops(m)
        s = r.get('status')
        if s in ('PUSHED', 'ALREADY_COMMITTED'):
            done(m, r['commit_sha'])
            return 'done', r['commit_sha']
        if s == 'NO_RESULT':
            continue  # rerun: step 2 catches a push that landed
        if s in ('STALE_BASE', 'PUSH_REJECTED'):
            new_base = fetch_tip(m['worktree'], m['target_branch'])      # read ONCE
            if not rebase_worktree(m, new_base):
                send_to_worker(m, 'rebase conflict on ' + new_base)
                return 'worker', 'rebase conflict on ' + new_base
            if not run_verify(m['worktree'], m['verify_cmd']):
                send_to_worker(m, 'verify failed after rebase on ' + new_base)
                return 'worker', 'verify failed after rebase on ' + new_base
            m['base_sha'] = new_base                                       # the SHA we rebased onto
            m['tree_sha'] = stage_and_write_tree(m)
            continue
        if s == 'HOOK_FAILED':
            send_to_worker(m, r.get('detail', ''))
            return 'worker', r.get('detail', '')
        if s == 'MANIFEST_MISMATCH':
            quarantine(m['worktree'])
            alert(m, r)
            return 'quarantined', r.get('detail', '')
        alert(m, r)
        return 'quarantined', f'unknown status {s}'
    alert(m, f'gave up after {max_attempts} attempts')
    return 'gave_up', f'gave up after {max_attempts} attempts'


def manifest(task_id, worktree, target_branch, files_changed, verify_cmd, commit):
    """Build a manifest for a verified chunk: HEAD is the base, the listed
    changes are staged and their tree hashed."""
    m = {'task_id': task_id, 'worktree': os.path.realpath(worktree), 'target_branch': target_branch,
         'files_changed': files_changed, 'verify_cmd': verify_cmd, 'commit': commit}
    m['base_sha'] = head(worktree)
    m['tree_sha'] = stage_and_write_tree(m)
    return m


if __name__ == '__main__':
    print(__doc__)
