"""Process-loop heartbeat for Docker; event delivery is checked separately."""
from pathlib import Path
import time

HEARTBEAT = Path("/tmp/workwise-heartbeat")


def beat():
    HEARTBEAT.touch()


if __name__ == "__main__":
    raise SystemExit(0 if HEARTBEAT.exists() and time.time() - HEARTBEAT.stat().st_mtime < 45 else 1)
