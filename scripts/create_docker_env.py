"""Generate isolated Docker credentials without displaying or overwriting them."""

from pathlib import Path
import secrets


def main() -> None:
    destination = Path(__file__).resolve().parents[1] / ".env.docker"
    values = {name: secrets.token_urlsafe(48) for name in (
        "POSTGRES_PASSWORD", "DB_PASSWORD", "MIGRATION_DB_PASSWORD", "JWT_SECRET",
    )}
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
