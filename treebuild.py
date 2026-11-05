"""Builds the tree a chunk would commit, without trusting the worktree's git.

The worktree is written by workers, so its .git/config and hooks are not
trusted: they can run commands (core.fsmonitor, filter.*.clean, hooks),
redirect pushes (pushurl, url.*.insteadOf) or change trailers. Nothing here
runs git with that config. Instead git runs against a repository the caller
owns (git_dir), with the worktree's files read as plain data (--work-tree),
a throwaway index, and no system, global or hook configuration:

    base tree (from git_dir)  +  the listed paths as they are on disk  ->  tree

The runner and the coordinator both build the tree this way, so the same
bytes give the same tree_sha.
"""

import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# An empty hooks directory of our own: no hook from anywhere runs.
EMPTY_HOOKS = tempfile.mkdtemp(prefix='ops-no-hooks-')
SAFE_CONFIG = [
    '-c', f'core.hooksPath={EMPTY_HOOKS}', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
    '-c', 'core.autocrlf=false', '-c', 'core.safecrlf=false', '-c', 'core.symlinks=true',
    '-c', 'protocol.ext.allow=never', '-c', 'trailer.separators=:', '-c', 'status.renames=false',
    '-c', 'diff.renames=false', '-c', 'diff.external=', '-c', 'core.quotePath=false',
]


def isolated_env(global_config=None, extra=None):
    """No system config; a global config the caller chooses (default: none)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': global_config or os.devnull,
                'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '/bin/false', 'SSH_ASKPASS': '/bin/false'})
    if extra:
        env.update(extra)
    return env


def run_git(git_dir, *args, env=None, work_tree=None, index=None, check=True, input=None, binary=False):
    cmd = ['git', '--git-dir', git_dir]
    if work_tree:
        cmd += ['--work-tree', work_tree]
    cmd += SAFE_CONFIG + list(args)
    e = dict(env or isolated_env())
    if index:
        e['GIT_INDEX_FILE'] = index
    p = subprocess.run(cmd, capture_output=True, text=not binary, env=e, input=input,
                       cwd=work_tree or git_dir)
    if check and p.returncode != 0:
        err = p.stderr if not binary else p.stderr.decode(errors='replace')
        raise RuntimeError(f"git {' '.join(args)} failed ({p.returncode}): {err.strip()}")
    return p


def build_tree(git_dir, worktree, base, files, env=None):
    """Stage base + the listed paths into a throwaway index. Returns
    (tree_sha, strays, staged, index_path_cleanup) where strays are porcelain
    lines that are not fully staged (untracked files, unlisted changes) and
    staged is the sorted [(status, path)] actually staged against base.
    The caller must call cleanup() when done with the index."""
    tmpdir = tempfile.mkdtemp(prefix='ops-index-')
    index = os.path.join(tmpdir, 'index')

    def cleanup():
        try:
            os.remove(index)
        except FileNotFoundError:
            pass
        os.rmdir(tmpdir)

    try:
        run_git(git_dir, 'read-tree', base, env=env, index=index)
        add = [p for s, p in files if s in ('A', 'M')]
        rm = [p for s, p in files if s == 'D']
        if add:
            run_git(git_dir, 'add', '--', *add, env=env, work_tree=worktree, index=index)
        if rm:
            run_git(git_dir, 'rm', '--cached', '-q', '--ignore-unmatch', '--', *rm, env=env, work_tree=worktree, index=index)
        st = run_git(git_dir, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignore-submodules=none',
                     env=env, work_tree=worktree, index=index).stdout
        strays = []
        entries = st.split('\0')
        i = 0
        while i < len(entries):
            e = entries[i]
            i += 1
            if not e:
                continue
            x, y, p = e[0], e[1], e[3:]
            if x in 'RC':
                i += 1
            if x not in 'AMD' or y != ' ':
                strays.append(f'{x}{y} {p}')
        diff = run_git(git_dir, 'diff', '--cached', '--name-status', '--no-renames', '-z', base,
                       env=env, index=index).stdout
        parts = [d for d in diff.split('\0') if d != '']
        staged = sorted(((parts[j], parts[j + 1]) for j in range(0, len(parts), 2)), key=lambda x: x[1])
        tree = run_git(git_dir, 'write-tree', env=env, index=index).stdout.strip()
        return tree, strays, staged, index, cleanup
    except Exception:
        cleanup()
        raise


def scratch_repo_for(worktree):
    """A coordinator-owned bare repo that borrows the worktree's objects
    (read-only, via alternates) but none of its config or hooks."""
    common = subprocess.run(['git', '-C', worktree, '-c', 'core.fsmonitor=false', 'rev-parse',
                             '--path-format=absolute', '--git-common-dir'],
                            capture_output=True, text=True, env=isolated_env(), check=True).stdout.strip()
    d = tempfile.mkdtemp(prefix='ops-scratch-')
    subprocess.run(['git', 'init', '-q', '--bare', d], check=True, env=isolated_env(), capture_output=True)
    with open(os.path.join(d, 'objects', 'info', 'alternates'), 'w') as f:
        f.write(os.path.join(common, 'objects') + '\n')
    return d


def worktree_head(worktree):
    """The worktree's HEAD. rev-parse reads refs only; it runs no hooks,
    filters or fsmonitor."""
    p = subprocess.run(['git', '-C', worktree, '-c', 'core.fsmonitor=false', 'rev-parse', '--verify', 'HEAD'],
                       capture_output=True, text=True, env=isolated_env())
    return p.stdout.strip() if p.returncode == 0 else None
