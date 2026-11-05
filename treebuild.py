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
import stat
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
    '-c', f'core.attributesFile={os.devnull}',
]
# No .gitattributes from the worktree applies (no filters, ident, encodings or
# eol conversion): the bytes hashed are the bytes on disk. Our own git-dir's
# info/attributes is the only other source, and it is empty.
EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
CONFIG_FILES = ('.gitignore', '.gitattributes')


class Refused(RuntimeError, ValueError):
    """The worktree holds something git must not process at all."""


def refuse_embedded_repos(worktree, files):
    """Before any git command reads the worktree's files: refuse any .git
    other than the worktree's own, anywhere in it, and any listed path that
    is a directory.

    A git repository planted inside the worktree has its own config and
    info/attributes, which git honours when it processes that directory
    (e.g. re-hashing a file through that repository's filter during
    `git add`), and nothing passed with -c or GIT_ATTR_SOURCE overrides
    them. So such a directory must never reach git."""
    root = os.path.realpath(worktree)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath == root and '.git' in dirnames + filenames:
            dirnames[:] = [d for d in dirnames if d != '.git']
            filenames = [f for f in filenames if f != '.git']
        if '.git' in dirnames or '.git' in filenames:
            rel = os.path.relpath(dirpath, root)
            raise Refused(f'{rel} is an embedded git repository; nothing was staged')
    for status, path in files:
        full = os.path.join(root, path)
        if status in ('A', 'M') and os.path.isdir(full) and not os.path.islink(full):
            raise Refused(f'{path} is a directory; list files only')


def isolated_env(global_config=None, extra=None):
    """No system config; a global config the caller chooses (default: none)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': global_config or os.devnull,
                'GIT_ATTR_SOURCE': EMPTY_TREE,
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
    refuse_embedded_repos(worktree, files)
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
        # An ignored .gitignore or .gitattributes (one that ignores itself) is
        # invisible to status but still changes what the tests ran against.
        ign = run_git(git_dir, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard', '--directory',
                      env=env, work_tree=worktree, index=index).stdout
        for p in ign.split('\0'):
            if p and os.path.basename(p.rstrip('/')) in CONFIG_FILES:
                strays.append(f'!! {p}')
        diff = run_git(git_dir, 'diff', '--cached', '--name-status', '--no-renames', '-z', base,
                       env=env, index=index).stdout
        parts = [d for d in diff.split('\0') if d != '']
        staged = sorted(((parts[j], parts[j + 1]) for j in range(0, len(parts), 2)), key=lambda x: x[1])
        tree = run_git(git_dir, 'write-tree', env=env, index=index).stdout.strip()
        return tree, strays, staged, index, cleanup
    except Exception:
        cleanup()
        raise


def worktree_dirs(worktree):
    """(git dir, common dir) of the worktree. rev-parse reads paths only."""
    p = subprocess.run(['git', '-C', worktree, '-c', 'core.fsmonitor=false', 'rev-parse', '--path-format=absolute',
                        '--git-dir', '--git-common-dir'], capture_output=True, text=True, env=isolated_env(), check=True)
    gd, common = p.stdout.split('\n')[:2]
    return gd, common


def scratch_repo_for(worktree):
    """A coordinator-owned bare repo (its own config, refs and hooks: none)
    that reads and writes the worktree's object store directly. Returns
    (scratch_dir, env): use env for every git call on it."""
    _, common = worktree_dirs(worktree)
    d = tempfile.mkdtemp(prefix='ops-scratch-')
    subprocess.run(['git', 'init', '-q', '--bare', d], check=True, env=isolated_env(), capture_output=True)
    return d, isolated_env(extra={'GIT_OBJECT_DIRECTORY': os.path.join(common, 'objects')})


def worktree_head(worktree):
    """The worktree's HEAD. rev-parse reads refs only; it runs no hooks,
    filters or fsmonitor."""
    p = subprocess.run(['git', '-C', worktree, '-c', 'core.fsmonitor=false', 'rev-parse', '--verify', 'HEAD'],
                       capture_output=True, text=True, env=isolated_env())
    return p.stdout.strip() if p.returncode == 0 else None


BLOB_MODES = ('100644', '100755', '120000')


def build_tree_from_blobs(git_dir, base, files, blobs, env=None):
    """Bundle mode: base's tree plus the listed paths, from bytes the caller
    hands over (blobs: path -> (mode, bytes)), with no worktree at all. Nothing
    reads a file, so no worktree config, attributes, filters, hooks or planted
    repository can take part, and nothing can change the bytes mid-run.

    Returns (tree_sha, staged, index, cleanup) like build_tree. Raises
    RuntimeError if git refuses a path (a .git component, a file where the base
    has a directory, ...)."""
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
        for status, path in files:
            if status == 'D':
                # mode 0 removes the entry; works without a work tree.
                run_git(git_dir, 'update-index', '-z', '--index-info', env=env, index=index, binary=True,
                        input=b'0 ' + b'0' * 40 + b'\t' + path.encode() + b'\0')
                continue
            mode, data = blobs[path]
            if mode not in BLOB_MODES:
                raise RuntimeError(f'{path}: mode {mode} is not a file or symlink mode')
            sha = run_git(git_dir, 'hash-object', '-w', '--no-filters', '--stdin', env=env, binary=True,
                          input=data).stdout.decode().strip()
            # --cacheinfo checks the path itself: no .git component, no file
            # where the base has a directory (or the reverse).
            run_git(git_dir, 'update-index', '--add', '--cacheinfo', f'{mode},{sha},{path}', env=env, index=index)
        diff = run_git(git_dir, 'diff', '--cached', '--name-status', '--no-renames', '-z', base,
                       env=env, index=index).stdout
        parts = [d for d in diff.split('\0') if d != '']
        staged = sorted(((parts[j], parts[j + 1]) for j in range(0, len(parts), 2)), key=lambda x: x[1])
        tree = run_git(git_dir, 'write-tree', env=env, index=index).stdout.strip()
        return tree, staged, index, cleanup
    except Exception:
        cleanup()
        raise


def read_blobs(worktree, files):
    """The bytes of the listed A/M paths as git would record them, read once:
    a symlink is its target, a regular file its contents and the owner's
    execute bit. Anything else (a directory, a device, a missing file) is
    refused. Returns {path: (mode, bytes)}."""
    root = os.path.realpath(worktree)
    blobs = {}
    for status, path in files:
        if status == 'D':
            continue
        full = os.path.join(root, path)
        if not (os.path.realpath(os.path.dirname(full)) + os.sep).startswith(root + os.sep):
            raise Refused(f'{path} resolves outside the worktree')
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            raise Refused(f'{path} is listed as {status} but is not on disk')
        if stat.S_ISLNK(st.st_mode):
            blobs[path] = ('120000', os.fsencode(os.readlink(full)))
        elif stat.S_ISREG(st.st_mode):
            with open(full, 'rb') as f:
                blobs[path] = ('100755' if st.st_mode & stat.S_IXUSR else '100644', f.read())
        else:
            raise Refused(f'{path} is not a regular file or a symlink')
    return blobs
