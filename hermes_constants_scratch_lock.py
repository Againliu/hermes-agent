"""Scratch owner locks: which processes the scratch prune must treat as Hermes' own.

A Hermes process holds an exclusive OS lock on ``<default root>/locks/scratch-owners/<pid><ns>.lock``
for as long as it lives. The operating system drops that lock the instant the holder dies (exit,
SIGKILL, TerminateProcess, OOM), so the prune never has to guess who is alive: a held lock is a
live holder, and clocks, reboots and pid reuse cannot fake one.

``<ns>`` is ``.ns<inode>`` of the holder's pid namespace on Linux (empty elsewhere): containers
that share one data dir (the compose gateway and dashboard) have their own pid numbering, so two
holders with equal pids never contend for one name. A held lock from another namespace names a pid
this process cannot use directly: it is resolved through ``/proc/<pid>/status`` ``NSpid`` to the
local pid of a process this prune can see, and dropped when none matches.

POSIX uses ``flock``: the lock belongs to the open file description, so the holder closing some
other descriptor of the same file (a profile clone or backup reading ``locks/`` in-process) does
not drop it, as it would a ``lockf``/``fcntl`` record lock. Hermes opens it non-inheritable, so an
exec'd child never shares it, and a fork child closes its inherited copies at once. Windows uses
``msvcrt.locking`` on a non-inheritable handle. Either way a held file names a live process Hermes
owns: the holder itself, or an unreaped child the holder spawned and vouches for (an unreaped
child's pid cannot be reused).

Who holds one: Hermes entry points for themselves (``process_identity.register_self`` or
:func:`hold_owner_lock`), and spawners for children they track until those stop
(:func:`vouch_for_child`: the process registry's background sessions, MCP stdio helpers),
``<child><ns>.vouch-<spawner>-<n>.lock``, a file of its own so the child taking its own lock never
waits on it. The owner pid is the part of the name before the first dot. Every other process is
owned through its live parent chain (:func:`owning_holder`).
"""
from __future__ import annotations

import atexit
import contextlib
import errno
import itertools
import logging
import os
import sys
import time
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"
if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

OWNERS_SUBDIR = ("locks", "scratch-owners")
_SUFFIX = ".lock"
# A prune probe holds a lock for microseconds: a contended lock is retried until this deadline.
_RETRY_SECONDS = 0.05
_RETRY_PAUSE = 0.001
# The lock is held by someone else. Any other error (ENOLCK on an NFS mount without lockd,
# EOPNOTSUPP) means this filesystem cannot lock at all: retrying cannot help.
_BUSY = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, getattr(errno, "EDEADLK", -1),
         getattr(errno, "EDEADLOCK", -1)}

# lock file path -> fd: this process's own lock (one per root; tests use many roots) and the locks
# it holds for children (:func:`vouch_for_child`).
_held: dict[str, int] = {}
_vouch_seq = itertools.count(1)


def _ours(path: str) -> bool:
    return path in _held


def _forget_inherited() -> None:
    """In a fork child: close the inherited lock descriptors, neither unlocking nor unlinking. The
    parent's flock lives on through its own descriptor, while a fork child that outlived its parent
    would otherwise keep a dead holder's lock held (a false owner once that pid is reused)."""
    for fd in _held.values():
        with contextlib.suppress(OSError):
            os.close(fd)
    _held.clear()


if not _IS_WINDOWS:
    os.register_at_fork(after_in_child=_forget_inherited)


def owners_dir(home: str | Path | None = None) -> Path:
    """The owners dir of *home*'s machine root (all profiles of an install share it)."""
    from hermes_constants import get_default_hermes_root

    root = get_default_hermes_root() if home is None else get_default_hermes_root(home=home)
    return root.joinpath(*OWNERS_SUBDIR)


def _digits(text: str) -> bool:
    # ASCII digits only: str.isdigit() also accepts other scripts' digits ("١٢").
    return text.isascii() and text.isdigit()


def pid_namespace() -> str:
    """``.ns<inode>`` of this process's pid namespace on Linux; empty elsewhere (or unreadable)."""
    if not sys.platform.startswith("linux"):
        return ""
    try:
        return f".ns{os.stat('/proc/self/ns/pid').st_ino}"
    except OSError:
        return ""


def parse_name(name: str) -> tuple[int, str] | None:
    """``(pid, namespace tag)`` a lock file name speaks for (``<pid>[.ns<ino>].lock`` or
    ``<pid>[.ns<ino>].vouch-...lock``); the tag is empty for an untagged name."""
    if not name.endswith(_SUFFIX):
        return None
    parts = name.split(".")
    if not _digits(parts[0]):
        return None
    tag = parts[1] if len(parts) > 2 and parts[1].startswith("ns") and _digits(parts[1][2:]) else ""
    return int(parts[0]), f".{tag}" if tag else ""


def own_lock_path(home: str | Path | None = None) -> str:
    """This process's own owner-lock file for *home*'s root."""
    return os.path.join(owners_dir(home), f"{os.getpid()}{pid_namespace()}{_SUFFIX}")


def _try_lock(fd: int) -> bool:
    """Take the lock without waiting: False when someone else holds it. Raises ``OSError`` when
    the filesystem cannot lock at all."""
    try:
        if _IS_WINDOWS:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _BUSY:
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    with contextlib.suppress(OSError):
        if _IS_WINDOWS:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _still_named(fd: int, path: str) -> bool:
    """False when a prune removed *path* between our open and our lock (we locked a dead inode)."""
    try:
        return os.path.samestat(os.fstat(fd), os.stat(path))
    except OSError:
        return False


def _acquire(path: str) -> str | None:
    """Lock *path* from this process; *path*, or None when another holder keeps it past the retry
    deadline. Raises ``OSError`` when the file cannot be created or the filesystem cannot lock."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.monotonic() + _RETRY_SECONDS
    while True:
        if _ours(path):  # re-checked each try: another thread of this process may have taken it
            return path
        created = not os.path.exists(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            locked = _try_lock(fd) and _still_named(fd, path)
        except OSError:
            os.close(fd)
            if created:  # a filesystem that cannot lock: leave no file behind per start or spawn
                with contextlib.suppress(OSError):
                    os.unlink(path)
            raise
        if locked:
            _held[path] = fd
            return path
        os.close(fd)
        if time.monotonic() >= deadline:
            return None
        time.sleep(_RETRY_PAUSE)


def _release(path: str) -> None:
    """Drop a lock this process holds, and its file. A fork child (atexit included) holds none of
    the parent's locks (:func:`_forget_inherited`) and must not touch the parent's files."""
    if not _ours(path):
        return
    fd = _held.pop(path)
    if not _IS_WINDOWS:
        with contextlib.suppress(OSError):
            os.unlink(path)  # while still holding: no probe can see it free first
    os.close(fd)
    if _IS_WINDOWS:
        with contextlib.suppress(OSError):
            os.unlink(path)


def release_all() -> None:
    """Drop every lock this process holds, and their files. For exit paths that skip ``atexit``
    (``os._exit``): the kernel would drop the locks anyway, but the files would stay behind."""
    for path in list(_held):
        _release(path)


def hold_owner_lock(home: str | Path | None = None) -> bool:
    """Take this process's owner lock for *home*'s root and hold it until the process exits.
    Idempotent. False (logged) when it cannot be taken: the prune will not know this process.
    Never raises: entry points call it first thing, and no lock failure may stop one starting."""
    try:
        path = own_lock_path(home)
        already = _ours(path)
        taken = _acquire(path)
    # health: allow BLE001 -- best-effort by contract: a home that cannot resolve (RuntimeError from Path.home()) must not abort the entry point
    except Exception as exc:
        logger.warning("scratch owner lock not taken (%s): the scratch prune will not know this process", exc)
        return False
    if taken is None:
        logger.warning("scratch owner lock %s is held elsewhere: the scratch prune will not know this process", path)
        return False
    if not already:
        atexit.register(_release, path)
    return True


def vouch_for_child(pid: int, home: str | Path | None = None) -> str | None:
    """Hold a lock naming *pid*, a child this process spawned (or re-adopted) and still tracks,
    until :func:`release_vouch` with the returned handle. It cannot outlive this process. Each call
    gets its own file, so a release only ever drops the vouch it names. Best-effort: None when the
    lock cannot be taken. Never raises: the registry calls it right after a spawn or during
    checkpoint recovery, where an exception would kill the fresh child or abort the recovery."""
    try:
        path = os.path.join(owners_dir(home), f"{pid}{pid_namespace()}.vouch-{os.getpid()}-{next(_vouch_seq)}{_SUFFIX}")
        return _acquire(path)
    # health: allow BLE001 -- best-effort by contract: a failed vouch only leaves the child unprotected from the prune
    except Exception as exc:
        logger.warning("scratch owner vouch for pid %s not taken (%s): the scratch prune will not know it", pid, exc)
        return None


def release_vouch(handle: str | None) -> None:
    """Stop vouching: drop the lock :func:`vouch_for_child` returned. No-op for None or a handle
    this process does not hold (already released, or inherited across a fork)."""
    if handle:
        _release(handle)


def _check_usable(directory: Path) -> None:
    """Raise ``OSError`` unless owner locks can be taken in *directory* right now. An owners dir
    that cannot be created, written or locked means holders may have failed to register: the
    caller must treat ownership as unknown rather than as \"nobody\"."""
    directory.mkdir(parents=True, exist_ok=True)
    probe = os.path.join(directory, f".probe-{os.getpid()}")
    fd = os.open(probe, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _try_lock(fd):
            raise OSError(errno.EAGAIN, "probe lock unexpectedly busy", probe)
        _unlock(fd)
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(probe)


def live_owner_pids(home: str | Path | None = None) -> dict[int, str]:
    """Local pid -> lock file for every owner lock held under *home*'s root, read straight from
    the OS lock state. A lock from another pid namespace counts only once resolved to a process
    this one can see (:func:`_resolve_foreign`). A file whose lock this pass can take is stale (its
    holder died): it is removed. Raises ``OSError`` when ownership cannot be known: the owners dir
    cannot be created, listed or written, or its filesystem cannot lock (the caller fails closed)."""
    directory = owners_dir(home)
    try:
        _check_usable(directory)
        names = os.listdir(directory)
    except OSError as exc:
        raise OSError(exc.errno, f"scratch owner locks unavailable in {directory}: {exc}") from exc
    ours = pid_namespace()
    owners: dict[int, str] = {}
    foreign: dict[tuple[str, int], str] = {}
    for name in sorted(names):
        parsed = parse_name(name)
        path = os.path.join(directory, name)
        if parsed is None or not (_ours(path) or _held_elsewhere(path)):
            continue
        pid, tag = parsed
        if tag in ("", ours):
            owners.setdefault(pid, path)
        else:
            foreign.setdefault((tag, pid), path)
    for pid, path in _resolve_foreign(foreign).items():
        owners.setdefault(pid, path)
    return owners


def sweep_stale_owner_locks(home: str | Path | None = None) -> None:
    """Remove every owner-lock file under *home*'s root whose holder is gone. Creates nothing: a
    root that has no owners dir has nothing to sweep."""
    directory = owners_dir(home)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return
    for name in names:
        path = os.path.join(directory, name)
        if parse_name(name) is not None and not _ours(path):
            _held_elsewhere(path)


def _ns_key(pid: int) -> tuple[str, int] | None:
    """``(namespace tag, pid inside that namespace)`` of a process this one can see (Linux)."""
    try:
        tag = f".ns{os.stat(f'/proc/{pid}/ns/pid').st_ino}"
        with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as status:
            line = next((line for line in status if line.startswith("NSpid:")), "")
        return tag, int(line.split()[-1])
    except (OSError, ValueError, IndexError):
        return None


def _resolve_foreign(foreign: dict[tuple[str, int], str]) -> dict[int, str]:
    """Local pid -> lock file for the held locks of other pid namespaces whose holder is a process
    this one can see (a host prune sees a container's processes). pid 1 of that namespace never
    owns (a container init adopts every orphan); a lock no visible process matches is dropped."""
    if not foreign:
        return {}
    import psutil

    resolved: dict[int, str] = {}
    for pid in psutil.pids():
        key = _ns_key(pid)
        if key is not None and key[1] != 1 and key in foreign:
            resolved[pid] = foreign[key]
    return resolved


def _held_elsewhere(path: str) -> bool:
    """Probe *path*: True when another process holds it (or it cannot be probed: an unknown is
    never read as free). A free file is stale and removed."""
    deadline = time.monotonic() + _RETRY_SECONDS
    while time.monotonic() < deadline:
        verdict = _probe(path)
        if verdict is not None:
            return verdict
    return True


def _probe(path: str) -> bool | None:
    """One probe of :func:`_held_elsewhere`; None when the inode we locked is no longer the one
    *path* names (a concurrent prune removed it and a new holder may have recreated the name)."""
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False  # its holder exited cleanly while we listed
    except OSError:
        return True
    try:
        if not _try_lock(fd):
            return True
        if _IS_WINDOWS:
            _unlock(fd)
        elif not _still_named(fd, path):
            return None  # unlinking by name now would remove the new holder's live file
        else:
            with contextlib.suppress(OSError):
                os.unlink(path)  # under our lock: a new holder re-checks the inode and retries
    except OSError:
        return True
    finally:
        os.close(fd)
    if _IS_WINDOWS:
        # Removal fails while anyone (a new holder of this name) has it open: left alone.
        with contextlib.suppress(OSError):
            os.unlink(path)
    return False


def ancestry(pid: int) -> list[int]:
    """*pid* and its ancestors through raw ``ppid()`` links, nearest first.

    Never ``psutil.parents()``: it drops a parent whose start time is later than the child's,
    which a backward clock step produces. On Linux/macOS a live child's ppid is a live process
    (orphans are reparented). On Windows a ppid can name a dead parent whose pid was reused; the
    chain then errs toward keeping. The seen-set stops a reuse cycle."""
    import psutil

    chain: list[int] = []
    current: int | None = pid
    while current and current not in chain:
        chain.append(current)
        try:
            current = psutil.Process(current).ppid()
        except (psutil.Error, OSError):
            break
    return chain


def owning_holder(pid: int, owners: dict[int, str] | set[int]) -> int | None:
    """The owner-lock holder covering *pid*: itself or the nearest holder on its parent chain.
    pid 1 never owns: a Hermes running as a container init adopts every orphan."""
    return next((p for p in ancestry(pid) if p in owners and p != 1), None)
