"""Memory-aware turn admission: new turns wait while the host is about to run out of memory.

On a small VPS or container the gateway, its turns and their children (browsers, terminals,
``execute_code`` kernels) share one memory ceiling. Crossing it wakes the kernel OOM killer,
which often picks the gateway itself and ends every session on it at once. This module reads
how much memory is really left and makes a NEW turn wait while that headroom is under the floor
for its kind, instead of starting a turn that tips the host over:

- headroom = the tighter of cgroup v2 headroom (``memory.max``/``memory.high`` minus
  ``memory.current``, with reclaimable ``inactive_file`` page cache credited back) along the
  gateway's cgroup chain, and ``/proc/meminfo`` ``MemAvailable``;
- people wait below ``critical``, background work (cron, internal events, kanban spawns) already
  below ``low``, so people are admitted first;
- each admitted turn reserves ``reserve_mb`` for a short window, so a burst of waiters released
  together cannot all start into the same headroom;
- the wait is bounded (``max_wait_seconds``); on expiry the turn is refused with a resend notice.

Running turns are never touched. Config lives under ``gateway.memory_guard`` and is read once
from the launch profile at gateway start: memory is a host fact, so the policy is process-wide.
Non-Linux hosts read ``unknown`` and admit everything.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_MB = 1024 * 1024
_CGROUP_ROOT = Path("/sys/fs/cgroup")
# A turn's own footprint (agent build, first tool spawns) lands in its first seconds.
_RESERVATION_WINDOW_S = 30.0
_POLL_INTERVAL_S = 2.0
_SAMPLE_TTL_S = 1.0
# "auto" floors: a fraction of the ceiling, capped, so a 512 MB container is not permanently
# "critical" just because the gateway itself already uses half of it.
_AUTO_CRITICAL = (0.10, 256)
_AUTO_LOW = (0.20, 512)
PERSON, BACKGROUND = "person", "background"
STATE_FILE = ("state", "memory_guard.json")


@dataclass(frozen=True)
class MemoryReading:
    available_bytes: int
    ceiling_bytes: Optional[int]
    source: str  # "cgroup" when a cgroup limit is the tighter bound, else "meminfo"


@dataclass(frozen=True)
class MemoryGuardSettings:
    enabled: bool = True
    critical_mb: Any = "auto"
    low_mb: Any = "auto"
    reserve_mb: int = 128
    max_wait_seconds: float = 300.0

    def floors_bytes(self, ceiling: Optional[int]) -> tuple[int, int]:
        """(critical, low) floors in bytes for a host whose ceiling is ``ceiling``."""
        critical = _floor_bytes(self.critical_mb, _AUTO_CRITICAL, ceiling)
        return critical, max(critical, _floor_bytes(self.low_mb, _AUTO_LOW, ceiling))


def _floor_bytes(value: Any, auto: tuple[float, int], ceiling: Optional[int]) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value * _MB)
    fraction, cap_mb = auto
    return min(cap_mb * _MB, int(ceiling * fraction)) if ceiling else cap_mb * _MB


def resolve_settings(config: Any) -> MemoryGuardSettings:
    """``gateway.memory_guard`` from a raw config dict (missing keys keep their defaults)."""
    gateway = config.get("gateway") if isinstance(config, dict) else None
    section = gateway.get("memory_guard") if isinstance(gateway, dict) else None
    if not isinstance(section, dict):
        return MemoryGuardSettings()
    defaults = MemoryGuardSettings()

    def number(key: str, default: Any) -> Any:
        value = section.get(key, default)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else default

    return MemoryGuardSettings(
        enabled=section.get("enabled", True) is not False,
        critical_mb=number("critical_mb", defaults.critical_mb),
        low_mb=number("low_mb", defaults.low_mb),
        reserve_mb=int(number("reserve_mb", defaults.reserve_mb)),
        max_wait_seconds=float(number("max_wait_seconds", defaults.max_wait_seconds)),
    )


# -- reading ------------------------------------------------------------------------------------

def _read_int(path: Path) -> Optional[int]:
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _inactive_file(directory: Path) -> int:
    try:
        lines = (directory / "memory.stat").read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return 0
    for line in lines:
        key, _, value = line.partition(" ")
        if key == "inactive_file" and value.strip().isdigit():
            return int(value)
    return 0


def _cgroup_of(pid: Optional[int]) -> Optional[str]:
    try:
        text = Path(f"/proc/{pid or 'self'}/cgroup").read_text(encoding="utf-8-sig")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:].strip() or "/"
    return None


def cgroup_headroom(rel_path: str, root: Path = _CGROUP_ROOT) -> Optional[tuple[int, int]]:
    """``(headroom, limit)`` for the tightest finite memory limit from ``rel_path`` up to the
    root, or None when no level is limited. A limit on an ancestor (a systemd slice, the
    container's namespace root) caps this process just as hard as one on its own cgroup."""
    parts = [p for p in rel_path.strip("/").split("/") if p]
    best: Optional[tuple[int, int]] = None
    for depth in range(len(parts), -1, -1):
        directory = root.joinpath(*parts[:depth])
        limits = [v for v in (_read_int(directory / "memory.max"), _read_int(directory / "memory.high"))
                  if v is not None and 0 < v < (1 << 62)]
        current = _read_int(directory / "memory.current")
        if not limits or current is None:
            continue
        limit = min(limits)
        headroom = limit - max(0, current - _inactive_file(directory))
        if best is None or headroom < best[0]:
            best = (headroom, limit)
    return best


def _meminfo() -> tuple[Optional[int], Optional[int]]:
    """``(MemAvailable, MemTotal)`` in bytes."""
    values: dict[str, int] = {}
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None, None
    for line in lines:
        key, _, rest = line.partition(":")
        fields = rest.split()
        if key in ("MemAvailable", "MemTotal") and fields and fields[0].isdigit():
            values[key] = int(fields[0]) * 1024
    return values.get("MemAvailable"), values.get("MemTotal")


def read_memory(pid: Optional[int] = None) -> Optional[MemoryReading]:
    """Memory left for ``pid`` (default: this process); None when unknowable (non-Linux)."""
    if sys.platform != "linux":
        return None
    available, total = _meminfo()
    rel = _cgroup_of(pid)
    capped = cgroup_headroom(rel) if rel is not None else None
    if capped is not None and (available is None or capped[0] <= available):
        return MemoryReading(max(0, capped[0]), capped[1], "cgroup")
    if available is None:
        return None
    return MemoryReading(available, capped[1] if capped else total, "meminfo")


def level_for(reading: Optional[MemoryReading], settings: MemoryGuardSettings, reserved: int = 0) -> str:
    if reading is None:
        return "unknown"
    critical, low = settings.floors_bytes(reading.ceiling_bytes)
    free = reading.available_bytes - reserved
    return "critical" if free < critical else "low" if free < low else "ok"


# -- the gate -----------------------------------------------------------------------------------

@dataclass
class _Waiter:
    seq: int
    kind: str


@dataclass
class MemoryGate:
    """Process-wide FIFO of turns waiting for memory, people ahead of background work.

    Shared by the event loop (inbound turns) and cron's worker threads, hence the thread lock;
    every method is short and non-blocking — the waiting itself happens in the callers."""

    settings: MemoryGuardSettings = field(default_factory=MemoryGuardSettings)
    status_path: Optional[Path] = None
    reader: Callable[[], Optional[MemoryReading]] = read_memory
    clock: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _waiters: list = field(default_factory=list)
    _reservations: list = field(default_factory=list)  # (expires_at, bytes)
    _seq: Any = field(default_factory=itertools.count)
    _sample: tuple = (0.0, None)
    _published: Optional[tuple] = None

    def _reading(self) -> Optional[MemoryReading]:
        sampled_at, reading = self._sample
        now = self.clock()
        if reading is None or now - sampled_at > _SAMPLE_TTL_S:
            reading = self.reader()
            self._sample = (now, reading)
        return reading

    def _reserved(self) -> int:
        now = self.clock()
        self._reservations = [r for r in self._reservations if r[0] > now]
        return sum(size for _, size in self._reservations)

    def _first_in_line(self, waiter: Optional[_Waiter], kind: str) -> bool:
        for other in self._waiters:
            if other is waiter:
                return True
            if other.kind == PERSON or kind == BACKGROUND:
                return False
        return True

    def try_admit(self, kind: str, waiter: Optional[_Waiter] = None) -> bool:
        """Admit now when headroom clears ``kind``'s floor and nobody is ahead in line."""
        with self._lock:
            if not self.settings.enabled:
                return True
            reading = self._reading()
            level = level_for(reading, self.settings, self._reserved())
            ok = level in ("ok", "unknown") or (kind == PERSON and level == "low")
            if ok and self._first_in_line(waiter, kind):
                if waiter is not None:
                    self._waiters.remove(waiter)
                if reading is not None:
                    self._reservations.append((self.clock() + _RESERVATION_WINDOW_S, self.settings.reserve_mb * _MB))
                admitted = True
            else:
                admitted = False
            self._publish_locked(level, reading)
            return admitted

    def enqueue(self, kind: str) -> _Waiter:
        with self._lock:
            waiter = _Waiter(next(self._seq), kind)
            self._waiters.append(waiter)
            self._publish_locked()
            return waiter

    def leave(self, waiter: _Waiter) -> None:
        with self._lock:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            self._publish_locked()

    def people_waiting(self) -> int:
        """People queued for memory right now; background spawners (kanban) yield while > 0."""
        with self._lock:
            return sum(1 for w in self._waiters if w.kind == PERSON)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(level_for(self._reading(), self.settings, self._reserved()), self._sample[1])

    def _snapshot_locked(self, level: str, reading: Optional[MemoryReading]) -> dict[str, Any]:
        people = sum(1 for w in self._waiters if w.kind == PERSON)
        return {
            "enabled": self.settings.enabled, "level": level, "pid": os.getpid(),
            "waiting_people": people, "waiting_background": len(self._waiters) - people,
            "available_mb": reading.available_bytes // _MB if reading else None,
            "ceiling_mb": reading.ceiling_bytes // _MB if reading and reading.ceiling_bytes else None,
            "source": reading.source if reading else None, "updated_at": time.time(),
        }

    def _publish_locked(self, level: Optional[str] = None, reading: Optional[MemoryReading] = None) -> None:
        """Write the status file for ``hermes status`` when level or queue depth changed."""
        if self.status_path is None:
            return
        if level is None:
            reading = self._sample[1]
            level = level_for(reading, self.settings, self._reserved())
        snap = self._snapshot_locked(level, reading)
        key = (snap["level"], snap["waiting_people"], snap["waiting_background"])
        if key == self._published:
            return
        self._published = key
        try:
            from hermes_constants import mkdir_under_hermes_home
            from utils import atomic_json_write

            mkdir_under_hermes_home(self.status_path.parent)
            atomic_json_write(self.status_path, snap)
        except OSError:
            logger.debug("memory guard: status file write failed", exc_info=True)


_gate = MemoryGate()


def get_gate() -> MemoryGate:
    return _gate


def configure(config: Any, home: Path) -> MemoryGate:
    """Install the process-wide gate from the launch profile's config (gateway start)."""
    global _gate
    _gate = MemoryGate(settings=resolve_settings(config), status_path=home.joinpath(*STATE_FILE))
    return _gate


def read_status(home: Path) -> Optional[dict[str, Any]]:
    """The gateway's last published status, or None when no live gateway wrote one."""
    from gateway.lifecycle_ledger import _read_json

    data = _read_json(home.joinpath(*STATE_FILE))
    pid = data.get("pid") if data else None
    if not isinstance(pid, int):
        return None
    from gateway.status import _pid_exists

    return data if _pid_exists(pid) else None


def status_line(config: Any, home: Path) -> str:
    """One line for ``hermes status``: level, headroom and the waiting count of the live gateway
    (read through the gateway's own cgroup, which under systemd differs from this shell's)."""
    settings = resolve_settings(config)
    published = read_status(home)
    reading = (read_memory(published["pid"]) if published else None) or read_memory()
    level = level_for(reading, settings)
    parts = [level]
    if reading is not None:
        ceiling = f" of {reading.ceiling_bytes // _MB} MB" if reading.ceiling_bytes else ""
        parts.append(f"{reading.available_bytes // _MB} MB free{ceiling} ({reading.source})")
    if published:
        people, background = published.get("waiting_people", 0), published.get("waiting_background", 0)
        parts.append(f"{people} waiting" + (f" + {background} background" if background else ""))
    else:
        parts.append("no gateway running")
    if not settings.enabled:
        parts.append("guard off")
    return " · ".join(parts)


# -- waiting ------------------------------------------------------------------------------------

async def wait_async(kind: str, *, on_hold: Callable[[], Any], still_wanted: Callable[[], bool],
                     gate: Optional[MemoryGate] = None) -> str:
    """Wait on the event loop until admitted. Returns ``admitted``, ``timeout`` or ``abandoned``
    (``still_wanted`` went False: /stop, /new, shutdown). ``on_hold`` runs once, awaited if async,
    when the turn first has to wait."""
    gate = gate or _gate
    if gate.try_admit(kind):
        return "admitted"
    waiter = gate.enqueue(kind)
    deadline = gate.clock() + gate.settings.max_wait_seconds
    try:
        notice = on_hold()
        if asyncio.iscoroutine(notice):
            await notice
        while True:
            if not still_wanted():
                return "abandoned"
            if gate.try_admit(kind, waiter):
                return "admitted"
            if gate.clock() >= deadline:
                return "timeout"
            await asyncio.sleep(_POLL_INTERVAL_S)
    finally:
        gate.leave(waiter)


def wait_blocking(kind: str, *, still_wanted: Callable[[], bool] = lambda: True,
                  gate: Optional[MemoryGate] = None, sleep: Optional[Callable[[float], Any]] = None) -> str:
    """Thread-side twin of :func:`wait_async` for cron's worker threads."""
    gate, sleep = gate or _gate, sleep or time.sleep
    if gate.try_admit(kind):
        return "admitted"
    waiter = gate.enqueue(kind)
    deadline = gate.clock() + gate.settings.max_wait_seconds
    try:
        while True:
            if not still_wanted():
                return "abandoned"
            if gate.try_admit(kind, waiter):
                return "admitted"
            if gate.clock() >= deadline:
                return "timeout"
            sleep(_POLL_INTERVAL_S)
    finally:
        gate.leave(waiter)
