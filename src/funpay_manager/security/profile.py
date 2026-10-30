import hashlib
from pathlib import Path


def credential_profile(database: Path) -> str:
    return hashlib.sha256(str(database.absolute()).casefold().encode()).hexdigest()[:24]
