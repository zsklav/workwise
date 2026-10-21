"""Generate isolated Docker credentials without displaying or overwriting them."""

import argparse
import secrets

from workwise.core.paths import ROOT_DIR


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--add-missing", action="store_true", help="Append new keys while preserving all existing values")
    arguments = parser.parse_args()
    destination = ROOT_DIR / ".env.docker"
    values = {name: secrets.token_urlsafe(48) for name in (
        "POSTGRES_PASSWORD", "DB_PASSWORD", "MIGRATION_DB_PASSWORD", "JWT_SECRET",
        "RABBITMQ_PASSWORD", "NOTIFICATION_ADMIN_PASSWORD", "NOTIFICATION_DB_PASSWORD",
        "NOTIFICATION_API_DB_PASSWORD", "NOTIFICATION_SERVICE_SECRET",
    )}
    if arguments.add_missing and destination.exists():
        existing = {line.split("=", 1)[0].strip() for line in destination.read_text(encoding="utf-8").splitlines() if "=" in line}
        with destination.open("a", encoding="utf-8") as stream:
            stream.write("\n")
            for name, value in values.items():
                if name not in existing:
                    stream.write(f"{name}={value}\n")
        print("Added missing Docker credential keys; existing values were preserved.")
        return
    try:
        with destination.open("x", encoding="utf-8") as stream:
            for name, value in values.items():
                stream.write(f"{name}={value}\n")
            stream.write("API_PORT=8001\n")
    except FileExistsError:
        raise SystemExit(".env.docker already exists; existing credentials were preserved.") from None
    print("Created ignored .env.docker with random credentials. Values were not displayed.")


if __name__ == "__main__":
    main()
