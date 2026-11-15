"""Local-disk resume storage. Files are named by a random key; the user's filename is never used."""
import os
from pathlib import Path
from uuid import uuid4

from workwise.core.security import auth_settings


def directory() -> Path:
    path = Path(auth_settings.resume_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def path_for(key: str) -> Path:
    if not key.isalnum():  # keys are uuid hex; anything else would be a traversal attempt
        raise ValueError("Invalid storage key")
    return directory() / f"{key}.pdf"


def save(chunks) -> tuple[str, Path]:
    """Write chunks to a temp file, then move it into place. Returns (key, final path)."""
    key = uuid4().hex
    final = path_for(key)
    temp = final.with_suffix(".part")
    try:
        with open(temp, "wb") as stream:
            for chunk in chunks:
                stream.write(chunk)
        os.replace(temp, final)
    finally:
        temp.unlink(missing_ok=True)
    return key, final


def remove(key: str) -> None:
    path_for(key).unlink(missing_ok=True)
