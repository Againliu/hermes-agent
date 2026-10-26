"""Whether a recorded host pid is still the background process the registry spawned.

Light on purpose: the scratch prune reads ``processes.json`` with the same rule
``recover_from_checkpoint`` re-adopts by, and must not import the process registry for it (that
import loads config, seeds the home and runs plugin discovery). Only ``gateway.status`` is
imported, lazily.
"""
from typing import Optional

# The registry's checkpoint file in each profile home (crash recovery of background processes).
CHECKPOINT_FILENAME = "processes.json"


class HostPidFateMixin:
    """The registry's same-process rule for recorded host pids (no instance state)."""

    @staticmethod
    def _is_host_pid_alive(pid: Optional[int]) -> bool:
        """Best-effort liveness check for host-visible PIDs."""
        if not pid:
            return False
        # ``os.kill(pid, 0)`` is NOT a no-op on Windows (bpo-14484) — use the
        # cross-platform existence check.
        from gateway.status import _pid_exists
        return _pid_exists(pid)

    @staticmethod
    def _safe_host_start_time(pid: Optional[int]) -> Optional[int]:
        """Kernel start ticks for a host PID, or None when unavailable."""
        try:
            from gateway.status import get_process_start_time
            return get_process_start_time(pid) if pid else None
        except Exception:
            return None

    @classmethod
    def _host_pid_is_ours(cls, pid: Optional[int], expected_start: Optional[int]) -> bool:
        """True only if ``pid`` is alive AND still the process we spawned.
        The kernel recycles PIDs, so a stored number can later name an unrelated
        process (seen in the wild: a browser's session leader tree-killed). The kernel
        start time captured at spawn must match the live one; with no baseline
        (legacy checkpoints, no ``/proc``) degrade to a bare liveness check."""
        return cls._is_host_pid_alive(pid) and (
            expected_start is None or cls._safe_host_start_time(pid) == expected_start)

    def _detached_host_fate(self, pid: Optional[int], expected_start: Optional[int]) -> str:
        """How a recovered host PID should be supervised.

        ``running`` — alive and still ours (start time matches, no baseline, or
        the start-time probe could not be read). Re-attach; do not invent an exit.
        ``reused`` — alive, and the start time positively differs. The number
        belongs to someone else; close our entry and never signal that PID.
        ``gone`` — the PID is not alive. Prune it; no exit status was collected.
        """
        if self._host_pid_is_ours(pid, expected_start):
            return "running"
        if not pid or not self._is_host_pid_alive(pid):
            return "gone"
        if expected_start is None:
            return "running"
        current = self._safe_host_start_time(pid)
        if current is None or current == expected_start:
            return "running"
        return "reused"


def detached_host_fate(pid: Optional[int], expected_start: Optional[int]) -> str:
    """:meth:`HostPidFateMixin._detached_host_fate` without a registry."""
    return HostPidFateMixin()._detached_host_fate(pid, expected_start)
