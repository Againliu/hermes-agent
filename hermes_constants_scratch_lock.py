"""Scratch owner locks: which processes the scratch prune must treat as Hermes' own.

A Hermes process holds an exclusive OS lock on ``<default root>/locks/scratch-owners/<pid>.lock``
for as long as it lives. The operating system drops that lock the instant the holder dies (exit,
SIGKILL, TerminateProcess, OOM), so the prune never has to guess who is alive: a held lock is a
live holder, and clocks, reboots and pid reuse cannot fake one.

POSIX uses ``fcntl.lockf`` record locks: owned by the process, never inherited by ``fork()``
children, kept across ``exec()``. Windows uses ``msvcrt.locking`` on a non-inheritable handle; the
lock belongs to the process that took it. Either way a lock never moves to another process, so a
held ``<pid>.lock`` names a live process Hermes owns: the holder itself, or an unreaped child the
holder spawned and vouches for (an unreaped child's pid cannot be reused).

Who holds one: every long-lived entry point (``process_identity.register_self``) for itself, and
the process registry for each background session it spawned (:func:`vouch_for_child`). Every
other process is owned through its live parent chain (:func:`owning_holder`).
"""
from __future__ import annotations

import atexit
import contextlib
import os
import sys
import time
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"
if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl

OWNERS_SUBDIR = ("locks", "scratch-owners")
_SUFFIX = ".lock"
# A prune probe holds a lock for microseconds: an owner retries for up to ~2 s before giving up.
_ACQUIRE_TRIES = 200
_ACQUIRE_PAUSE = 0.01

# lock file path -> (fd, pid of the process that took the lock): its own lock (one per root; tests
# use many roots) and the locks it holds for children it spawned (:func:`vouch_for_child`). A fork
# child inherits this dict but not the locks, so every use checks the pid.
_held: dict[str, tuple[int, int]] = {}


def _ours(path: str) -> bool:
    return _held.get(path, (None, None))[1] == os.getpid()


def owners_dir(home: str | Path | None = None) -> Path:
    """The owners dir of *home*'s machine root (all profiles of an install share it)."""
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root(home=home).joinpath(*OWNERS_SUBDIR)


def _try_lock(fd: int) -> bool:
    try:
        if _IS_WINDOWS:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    with contextlib.suppress(OSError):
        if _IS_WINDOWS:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.lockf(fd, fcntl.LOCK_UN)


def _still_named(fd: int, path: str) -> bool:
    """False when a prune removed *path* between our open and our lock (we locked a dead inode)."""
    try:
        return os.path.samestat(os.fstat(fd), os.stat(path))
    except OSError:
        return False


def _acquire(home: str | Path | None, pid: int) -> str | None:
    """Lock ``<pid>.lock`` in *home*'s owners dir from this process; its path, or None."""
    directory = owners_dir(home)
    path = os.path.join(directory, f"{pid}{_SUFFIX}")
    if _ours(path):
        return path
    directory.mkdir(parents=True, exist_ok=True)
    for _ in range(_ACQUIRE_TRIES):
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        if _try_lock(fd) and _still_named(fd, path):
            _held[path] = (fd, os.getpid())
            return path
        os.close(fd)
        time.sleep(_ACQUIRE_PAUSE)
    return None


def _release(path: str) -> None:
    """Drop a lock this process holds, and its file. A fork child (atexit included) holds none of
    the parent's locks and must not touch the parent's files."""
    if not _ours(path):
        return
    fd, _ = _held.pop(path)
    if not _IS_WINDOWS:
        with contextlib.suppress(OSError):
            os.unlink(path)  # while still holding: no probe can see it free first
    os.close(fd)
    if _IS_WINDOWS:
        with contextlib.suppress(OSError):
            os.unlink(path)


def hold_owner_lock(home: str | Path | None = None) -> bool:
    """Take this process's owner lock for *home*'s root and hold it until the process exits.
    Idempotent. False when it cannot be taken."""
    pid = os.getpid()
    already = _ours(os.path.join(owners_dir(home), f"{pid}{_SUFFIX}"))
    path = _acquire(home, pid)
    if path and not already:
        atexit.register(_release, path)
    return path is not None


def vouch_for_child(pid: int, home: str | Path | None = None) -> None:
    """Hold ``<pid>.lock`` for a child this process spawned and has not reaped, until
    :func:`release_child`. The child is owned for exactly as long as this process is alive and
    tracking it; a lock cannot outlive this process, and an unreaped child's pid cannot be reused
    (POSIX keeps it as a zombie, Windows while its Popen handle is open). Best-effort."""
    with contextlib.suppress(OSError):
        _acquire(home, pid)


def release_child(pid: int, home: str | Path | None = None) -> None:
    """The child *pid* finished: stop vouching for it. No-op when it was never vouched for."""
    _release(os.path.join(owners_dir(home), f"{pid}{_SUFFIX}"))


def live_owner_pids(home: str | Path | None = None) -> set[int]:
    """Pids holding an owner lock under *home*'s root, read straight from the OS lock state.
    A file whose lock this pass can take is stale (its holder died): it is removed. Raises
    ``OSError`` when the owners dir exists but cannot be listed (the caller fails closed)."""
    directory = owners_dir(home)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return set()
    owners: set[int] = set()
    for name in names:
        stem = name[: -len(_SUFFIX)] if name.endswith(_SUFFIX) else ""
        if not stem.isdigit():
            continue
        path = os.path.join(directory, name)
        if _ours(path):
            # Ours: probing it would succeed (a process never conflicts with its own lock) and
            # the close after the probe would drop the lock on POSIX.
            owners.add(int(stem))
            continue
        try:
            fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            continue  # its holder exited cleanly while we listed
        except OSError:
            owners.add(int(stem))  # cannot probe: never read an unknown as free
            continue
        try:
            if not _try_lock(fd):
                owners.add(int(stem))
                continue
            if _IS_WINDOWS:
                _unlock(fd)
            else:
                with contextlib.suppress(OSError):
                    os.unlink(path)  # under our lock: a new holder re-checks the inode and retries
        finally:
            os.close(fd)
        if _IS_WINDOWS:
            # Removal fails while anyone (a new holder with this pid) has it open: left alone.
            with contextlib.suppress(OSError):
                os.unlink(path)
    return owners


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


def owning_holder(pid: int, owners: set[int]) -> int | None:
    """The owner-lock holder covering *pid*: itself or the nearest holder on its parent chain.
    pid 1 never owns: a Hermes running as a container init adopts every orphan."""
    return next((p for p in ancestry(pid) if p in owners and p != 1), None)
