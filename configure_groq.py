"""Configure Groq on Pi in your own terminal. Never pass a key in argv or chat."""
import argparse
import getpass
import json
import os
import re
import sys
import tempfile
import warnings
from pathlib import Path

from config import DEFAULT_ENV, load_env
from groq_solver import BASE, MODEL, GroqModel


def private_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.is_symlink():
        raise ValueError("private_config_symlink_forbidden")
    fd, name = tempfile.mkstemp(prefix=".provider-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def activate(env_file, key_file, proxy_url):
    """Validate the provider first; preserve all existing Stepik OAuth lines."""
    env_file = Path(env_file)
    load_env(env_file)
    identity = GroqModel(key_file=key_file, proxy_url=proxy_url).preflight()
    updates = {"LLM_PROVIDER": "groq", "LLM_MODEL": MODEL, "LLM_BASE_URL": BASE,
               "LLM_API_KEY_FILE": str(key_file), "LLM_MAX_TOKENS": "2048", "DAILY_TARGET": "25"}
    if proxy_url:
        updates["LLM_PROXY_URL"] = proxy_url
    if any(not re.fullmatch(r"[^\s'\"`$\\]+", value) for value in updates.values()):
        raise ValueError("unsupported_private_config_value")
    owned = set(updates) | {"LLM_PROXY_URL"}
    rows = [line for line in env_file.read_text().splitlines() if line.partition("=")[0] not in owned]
    rows.extend(key + "=" + value for key, value in updates.items())
    private_text(env_file, "\n".join(rows) + "\n")
    return identity


def install_key(env_file, key_file, key, proxy_url):
    """A rejected replacement must not overwrite a previously working key."""
    load_env(env_file)
    key_file = Path(key_file)
    if key_file.is_symlink():
        raise ValueError("private_config_symlink_forbidden")
    key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".groq-check-", dir=key_file.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        GroqModel(key_file=name, proxy_url=proxy_url).preflight()
        private_text(key_file, key + "\n")
        return activate(env_file, key_file, proxy_url)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--key-file", type=Path, default=Path.home() / ".config/stepik-daily-bot/groq.key")
    parser.add_argument("--proxy-url", default="http://127.0.0.1:10809")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--use-existing-key", action="store_true", help="Validate and activate a key already saved privately")
    modes.add_argument("--check-only", action="store_true", help="GET-only authentication check; no config or Stepik changes")
    args = parser.parse_args()
    try:
        if args.check_only:
            identity = GroqModel(key_file=args.key_file, proxy_url=args.proxy_url or None).preflight()
        else:
            if not args.use_existing_key:
                if not sys.stdin.isatty():
                    raise ValueError("interactive_pi_terminal_required")
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("Groq API key (hidden): ").strip()
                if not re.fullmatch(r"gsk_[A-Za-z0-9_-]{20,}", key):
                    raise ValueError("invalid_groq_key_format")
                identity = install_key(args.env_file, args.key_file, key, args.proxy_url or None)
            else:
                identity = activate(args.env_file, args.key_file, args.proxy_url or None)
        print(json.dumps({"provider": identity["provider"], "model": identity["name"],
                          "authenticated": True, "activated": not args.check_only, "daily_target": 25}))
        return 0
    except Exception as exc:
        # Never echo third-party exception strings or HTTP bodies.
        reason = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[a-z_0-9]+", str(exc)) else type(exc).__name__
        print("groq_configuration_error: " + reason, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
