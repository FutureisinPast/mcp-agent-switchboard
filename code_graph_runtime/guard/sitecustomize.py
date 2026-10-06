"""Network guard for graphify child processes (loaded automatically when this dir is first on PYTHONPATH).
Raises on socket.connect / socket.getaddrinfo / urllib.Request in every Python child, including
multiprocessing workers (they inherit PYTHONPATH). Non-Python children (git, cmd) are NOT covered."""
import sys

_BLOCKED = {"socket.connect", "socket.getaddrinfo", "urllib.Request"}


def _guard(event, args):
    if event in _BLOCKED:
        raise RuntimeError(f"gfy child network guard blocked {event}")


if not getattr(sys, "_gfy_child_guard", False):
    sys._gfy_child_guard = True
    sys.addaudithook(_guard)
