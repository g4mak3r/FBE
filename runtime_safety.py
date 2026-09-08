"""Process-level DEMO backstop, installed once before constructing clients."""
from __future__ import annotations
import socket
import sys

_demo_locked = False


def demo_locked() -> bool:
    return _demo_locked


def print_is_dry_run(config: dict) -> bool:
    return _demo_locked or bool(config.get("mock_mode")) or bool(config.get("dry_run_print", True))


def install_demo_guard() -> None:
    global _demo_locked
    if _demo_locked:
        return
    _demo_locked = True

    def audit(event, args):
        # A process spawned from DEMO could bypass Python HTTP guards or send a
        # physical print job. External renderers are intentionally unavailable.
        if event in {"subprocess.Popen", "os.system", "os.startfile", "os.posix_spawn"}:
            raise RuntimeError("DEMO MODE: external processes are disabled")
        if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname"}:
            if event == "socket.connect" and args[0].family == socket.AF_UNIX:
                return  # local asyncio/test transports, never production HTTP
            raise RuntimeError("DEMO MODE: outbound network is disabled")
    sys.addaudithook(audit)
