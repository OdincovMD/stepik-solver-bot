#!/usr/bin/env python3
"""Manage local credentials. Secrets are written to private files, never stdout."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets


DEFAULT_DIR = Path.home() / ".config/raspberry-llm"
ITERATIONS = 600_000


def write_private(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def password_record(password):
    salt = secrets.token_hex(16)
    return {"salt": salt, "iterations": ITERATIONS,
            "hash": hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "set-password", "add-key", "revoke-key", "set-origin"))
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--name", default="personal")
    parser.add_argument("--origin")
    args = parser.parse_args()
    config_path = args.directory / "access.json"
    credentials_path = args.directory / "credentials.json"
    if args.command == "init":
        if config_path.exists() or credentials_path.exists():
            raise SystemExit("Credentials already exist; use rotation commands")
        password = secrets.token_urlsafe(24)
        api_key = "llm_" + secrets.token_urlsafe(32)
        config = {"origin": args.origin or "https://localhost", "prefix": "/llm", "username": "owner",
                  "password": password_record(password), "session_secret": secrets.token_hex(32),
                  "api_keys": {"personal": hashlib.sha256(api_key.encode()).hexdigest()}}
        credentials = {"username": "owner", "password": password, "api_keys": {"personal": api_key}}
    else:
        config = json.loads(config_path.read_text())
        credentials = json.loads(credentials_path.read_text())
        if args.command == "set-password":
            password = getpass.getpass("Новый пароль: ")
            if len(password) < 12 or password != getpass.getpass("Повторите пароль: "):
                raise SystemExit("Password must match and have at least 12 characters")
            config["password"] = password_record(password)
            config["session_secret"] = secrets.token_hex(32)
            credentials["password"] = password
        elif args.command == "add-key":
            if args.name in config["api_keys"]:
                raise SystemExit("Key name already exists; revoke it first")
            key = "llm_" + secrets.token_urlsafe(32)
            config["api_keys"][args.name] = hashlib.sha256(key.encode()).hexdigest()
            credentials["api_keys"][args.name] = key
        elif args.command == "revoke-key":
            config["api_keys"].pop(args.name, None)
            credentials["api_keys"].pop(args.name, None)
        elif args.command == "set-origin":
            from urllib.parse import urlsplit
            value = urlsplit(args.origin or "")
            if value.scheme != "https" or not value.hostname or value.path or value.query or value.fragment or value.username:
                raise SystemExit("Expected an HTTPS origin without path or credentials")
            config["origin"] = args.origin
            config["session_secret"] = secrets.token_hex(32)
    write_private(config_path, config)
    write_private(credentials_path, credentials)
    print("Access configuration saved; credentials remain in", credentials_path)


if __name__ == "__main__":
    main()
