"""Run in an interactive terminal ON THE PI, never through a chat or recorded session."""
import getpass
import argparse
import os
import re
import sys
import warnings
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path.home() / ".config/stepik-daily-bot/stepik.env")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise ValueError("interactive_pi_terminal_required")
    path = args.env_file.expanduser()
    if path.exists():
        raise ValueError("env_exists_edit_locally_instead")
    warnings.simplefilter("error", getpass.GetPassWarning)
    # Both values stay out of shell history and terminal output.
    client = getpass.getpass("Stepik Client ID (hidden): ")
    secret = getpass.getpass("Stepik Client secret (hidden): ")
    expected = input("Your numeric Stepik user ID from your profile URL: ").strip()
    if not expected.isdigit() or int(expected) <= 0:
        raise ValueError("invalid_user_id")
    if any(not re.fullmatch(r"[^\s'\"`$\\]+", x) for x in (client, secret)):
        raise ValueError("invalid_oauth_value")
    private = Path.home() / ".config/stepik-daily-bot"
    state = Path.home() / ".local/state/stepik-daily-bot"
    body = (f"STEPIK_CLIENT_ID={client}\nSTEPIK_CLIENT_SECRET={secret}\n"
            f"STEPIK_EXPECTED_USER_ID={expected}\nLLM_MODEL=raspberry-llm\n"
            "LLM_BASE_URL=http://127.0.0.1:18085/v1\n"
            f"LLM_API_KEY_FILE={private / 'llm.key'}\nLLM_MAX_TOKENS=384\n"
            f"DAILY_TARGET=25\nSTATE_DIR={state}\n")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    # O_EXCL also prevents accidental overwrite in another setup process.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(body)
        output.flush()
        os.fsync(output.fileno())
    print("Saved private configuration with mode 600:", path)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("Configuration not completed; check local file/terminal and input format.", file=sys.stderr)
        sys.exit(1)
