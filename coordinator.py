"""Coordinator-side helpers for the ops runner.

The coordinator verifies a chunk, computes its tree_sha, writes the manifest
and runs the runner. On STALE_BASE or PUSH_REJECTED it owns the recovery:
rebase onto the remote tip read ONCE, hash the tree, re-verify, check the
tree didn't change, resubmit. A rebase conflict or a failed re-verify goes
back to the worker; a MANIFEST_MISMATCH quarantines the worktree and alerts
Evan.

Before each run, the worktree's HEAD is exactly base_sha and the chunk's
changes are uncommitted. The runner never touches the worktree; it commits in
its own repository.

Like the runner, these helpers never run git with the worktree's config or
hooks (workers write those): every git call here uses treebuild's isolated
settings.
"""

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import treebuild  # noqa: E402

MAX_ATTEMPTS = 5


def wgit(wt, *args, check=True):
    """git in the worktree, without its config's commands or its hooks."""
    p = subprocess.run(['git', '-C', wt, *treebuild.SAFE_CONFIG, *args], capture_output=True, text=True,
                       env=treebuild.isolated_env())
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p


def files_of(m):
    return sorted(((f['status'], f['path']) for f in m['files_changed']), key=lambda x: x[1])


def tree_of(m):
    """The tree the runner will build: base_sha's tree plus the listed paths as
    they are on disk, computed outside the worktree's git. Raises if there are
    strays or the staged set isn't files_changed (the runner would refuse)."""
    scratch = treebuild.scratch_repo_for(m['worktree'])
    try:
        tree, strays, staged, _, cleanup = treebuild.build_tree(scratch, m['worktree'], m['base_sha'], files_of(m))
        cleanup()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if strays:
        raise ValueError(f'stray or unlisted changes: {strays[:20]}')
    if staged != files_of(m):
        raise ValueError(f'staged changes {staged} are not files_changed {files_of(m)}')
    return tree


def head(wt):
    return treebuild.worktree_head(wt)


def fetch_tip(wt, branch, remote='origin'):
    """The remote tip, read once."""
    wgit(wt, 'fetch', '--no-tags', '--quiet', remote, f'+refs/heads/{branch}:refs/remotes/{remote}/{branch}')
    return wgit(wt, 'rev-parse', '--verify', f'refs/remotes/{remote}/{branch}').stdout.strip()


def rebase_worktree(m, new_base):
    """Move the uncommitted chunk onto new_base. Returns None on success (the
    changes uncommitted on new_base), or a reason: 'conflict ...' with the
    rebase aborted and the changes uncommitted on the old base again, or
    'rebase failed ...' when it couldn't start."""
    wt, old = m['worktree'], m['base_sha']
    try:
        add = [p for s, p in files_of(m) if s in ('A', 'M')]
        rm = [p for s, p in files_of(m) if s == 'D']
        if add:
            wgit(wt, 'add', '--', *add)
        if rm:
            wgit(wt, 'rm', '--cached', '-q', '--ignore-unmatch', '--', *rm)
        wgit(wt, '-c', 'user.name=ops-coordinator', '-c', 'user.email=ops@localhost',
             'commit', '-q', '-m', 'wip', '--no-gpg-sign')
    except RuntimeError as e:
        wgit(wt, 'reset', '-q', '--soft', old, check=False)
        return f'rebase failed before starting: {e}'
    p = wgit(wt, '-c', 'user.name=ops-coordinator', '-c', 'user.email=ops@localhost', 'rebase', new_base, check=False)
    if p.returncode != 0:
        conflicted = os.path.exists(os.path.join(wt, '.git', 'rebase-merge')) or \
            os.path.exists(os.path.join(wt, '.git', 'rebase-apply'))
        wgit(wt, 'rebase', '--abort', check=False)
        wgit(wt, 'reset', '-q', '--soft', old)
        return ('conflict on ' if conflicted else 'rebase failed on ') + new_base + ': ' + (p.stdout + p.stderr).strip()[-500:]
    wgit(wt, 'reset', '-q', '--soft', new_base)
    return None


def run_verify(wt, verify_cmd):
    return subprocess.run(verify_cmd, shell=True, cwd=wt).returncode == 0


def verified_tree(m):
    """Hash the tree, run verify_cmd, and require the tree unchanged: the
    pushed bytes are the verified bytes, even if verify writes files. Returns
    (tree_sha, None) or (None, reason)."""
    try:
        before = tree_of(m)
    except ValueError as e:
        return None, str(e)
    if not run_verify(m['worktree'], m['verify_cmd']):
        return None, 'verify failed'
    try:
        after = tree_of(m)
    except ValueError as e:
        return None, f'verify changed the worktree: {e}'
    if after != before:
        return None, 'verify changed the listed files'
    return before, None


def run_ops(m, runner=None, env=None):
    """Run the ops runner on manifest m; returns its JSON result."""
    runner = runner or [sys.executable, os.path.join(HERE, 'ops_reconcile.py')]
    p = subprocess.run(runner, input=json.dumps(m), capture_output=True, text=True, env=env)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        # No result (killed, crashed): a lost response. The next run's
        # idempotency check tells whether the push landed.
        return {'status': 'NO_RESULT', 'detail': p.stderr.strip()[-500:]}


def land_chunk(m, run_ops=run_ops, send_to_worker=None, quarantine=None, alert=None, done=None,
               max_attempts=MAX_ATTEMPTS):
    """The coordinator loop from the build spec. Returns (outcome, detail):
    'done' | 'worker' | 'quarantined' | 'push_error' | 'gave_up'."""
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
            if new_base == m['base_sha']:
                # Nothing moved: not a race. Auth, network or a server rule.
                alert(m, r)
                return 'push_error', r.get('detail', '')
            why = rebase_worktree(m, new_base)
            if why:
                send_to_worker(m, why)
                return 'worker', why
            m['base_sha'] = new_base                                       # the SHA we rebased onto
            tree, why = verified_tree(m)
            if why:
                send_to_worker(m, f'{why} after rebase on {new_base}')
                return 'worker', f'{why} after rebase on {new_base}'
            m['tree_sha'] = tree
            continue
        if s == 'HOOK_FAILED':
            send_to_worker(m, r.get('detail', ''))
            return 'worker', r.get('detail', '')
        quarantine(m['worktree'])
        alert(m, r)
        return 'quarantined', r.get('detail', '') if s == 'MANIFEST_MISMATCH' else f'unknown status {s}'
    alert(m, f'gave up after {max_attempts} attempts')
    return 'gave_up', f'gave up after {max_attempts} attempts'


def manifest(task_id, worktree, target_branch, files_changed, verify_cmd, commit):
    """The manifest for a chunk: HEAD is the base; the tree is hashed, verify
    runs, and the tree must not have changed. Raises ValueError otherwise."""
    m = {'task_id': task_id, 'worktree': os.path.realpath(worktree), 'target_branch': target_branch,
         'files_changed': files_changed, 'verify_cmd': verify_cmd, 'commit': commit}
    m['base_sha'] = head(worktree)
    tree, why = verified_tree(m)
    if why:
        raise ValueError(why)
    m['tree_sha'] = tree
    return m


if __name__ == '__main__':
    print(__doc__)
