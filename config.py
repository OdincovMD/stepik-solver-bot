"""Minimal local configuration. Values are never echoed or included in errors."""
import os
import re
import stat
from pathlib import Path

ALLOWED = {"STEPIK_CLIENT_ID", "STEPIK_CLIENT_SECRET", "STEPIK_EXPECTED_USER_ID",
           "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY_FILE",
           "LLM_MAX_TOKENS", "DAILY_TARGET", "STATE_DIR"}

DEFAULT_ENV = Path.home() / ".config/stepik-daily-bot/stepik.env"
DEFAULT_STATE = Path.home() / ".local/state/stepik-daily-bot"


def load_env(path):
    path = Path(path)
    if not path.exists():
        return  # systemd may supply the environment instead
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("env_file_must_be_private_chmod_600")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in ALLOWED or not re.fullmatch(r"[^\s'\"`$\\]+", value):
            raise ValueError("invalid_env_file")
        os.environ.setdefault(key, value)
