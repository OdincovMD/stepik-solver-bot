"""Single-process lock and fsync'd atomic journal; old prototype counts are unverified."""
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def load_state(path):
    if not path.exists():
        return {"version": 2, "account": None, "solved": {}, "attempted": {}, "jobs": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid_state")
    if "version" not in data:
        # Keep the archive, but never trust its day or old correct-only counter.
        return {"version": 2, "account": None, "solved": {}, "attempted": data.get("attempted", {}),
                "jobs": {}, "legacy_solved": data.get("solved", {})}
    if data.get("version") != 2 or any(not isinstance(data.get(x), dict) for x in ("solved", "attempted", "jobs")):
        raise ValueError("unsupported_or_invalid_state")
    return data


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def state_lock(folder):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / ".lock").open("a") as lock:
        os.chmod(folder / ".lock", 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another_bot_is_running") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
