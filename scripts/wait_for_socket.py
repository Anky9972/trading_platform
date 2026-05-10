"""
Poll for a Unix socket to become available.
Used by start.sh instead of sleep.
Actual synchronization primitive — not a guess.

Usage:
  python scripts/wait_for_socket.py /path/to/socket [timeout_seconds]
Exit codes:
  0 = socket ready
  1 = timeout
"""
import socket
import sys
import time
import os
import json


def wait_for_socket(path: str, timeout: int = 10, interval: float = 0.25) -> bool:
    """Block until Unix socket at path accepts connections."""
    deadline = time.time() + timeout

    while time.time() < deadline:
        if not os.path.exists(path):
            time.sleep(interval)
            continue

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(path)
            sock.sendall(b'{"action":"health"}')
            response = sock.recv(4096)
            sock.close()
            if response:
                return True
        except (ConnectionRefusedError, FileNotFoundError, socket.timeout):
            time.sleep(interval)
        except Exception:
            time.sleep(interval)

    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/wait_for_socket.py SOCKET_PATH [TIMEOUT]")
        sys.exit(1)

    path = sys.argv[1]
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    if wait_for_socket(path, timeout):
        print(f"Socket {path} is ready")
        sys.exit(0)
    else:
        print(f"TIMEOUT: Socket {path} not ready after {timeout}s", file=sys.stderr)
        sys.exit(1)
