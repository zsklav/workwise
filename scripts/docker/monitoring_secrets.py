"""Initialize the isolated Prometheus credential volume without logging the token."""
import os
from pathlib import Path
import tempfile

from workwise.operations.metrics import MonitoringSettings


def write_token(directory):
    token = MonitoringSettings().metrics_token
    if token is None:
        raise RuntimeError("METRICS_TOKEN is required")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="ascii") as stream:
            stream.write(token.get_secret_value())
        os.chmod(name, 0o444)
        destination = directory / "metrics-token"
        if destination.exists():
            destination.chmod(0o600)  # Windows also permits replacing the read-only file.
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


if __name__ == "__main__":
    write_token("/secrets")
    print("Prometheus credential file initialized.")
