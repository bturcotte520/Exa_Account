import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    api_key: str = field(default="", repr=False)
    db_path: Path = ROOT / "data" / "account-watch.sqlite3"
    secrets_path: Path = ROOT / "data" / "secrets"
    receiver_url: str = ""
    signature_tolerance: int = 300


def load_settings(env_file=None):
    # Never discover other dotenv files or load unrelated credentials.
    values = dotenv_values(env_file) if env_file else {}
    def value(key, default=""):
        return os.environ.get(key, values.get(key) or default)
    return Settings(
        api_key=value("exa_key"),
        db_path=Path(value("ACCOUNT_WATCH_DB", str(ROOT / "data/account-watch.sqlite3"))),
        secrets_path=Path(value("ACCOUNT_WATCH_SECRETS", str(ROOT / "data/secrets"))),
        receiver_url=value("ACCOUNT_WATCH_RECEIVER_URL"),
        signature_tolerance=int(value("ACCOUNT_WATCH_SIGNATURE_TOLERANCE", "300")),
    )
