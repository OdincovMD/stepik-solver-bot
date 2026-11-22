#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
task_user="$(id -un)"
if [[ "$EUID" == 0 ]]; then
  echo 'Run this script as the normal bot user; it invokes sudo itself.' >&2
  exit 1
fi
cd "$task_root"
task_enable="${1:-}"
if [[ -n "$task_enable" && "$task_enable" != --enable ]]; then
  echo 'Usage: scripts/install-systemd.sh [--enable]' >&2
  exit 1
fi
if [[ "$task_enable" == --enable ]]; then
.venv/bin/python - <<'PY'
import os
from pathlib import Path
from config import DEFAULT_ENV, DEFAULT_STATE, load_env
from storage import load_state
from solver import configured_model
load_env(DEFAULT_ENV)
state = load_state(Path(os.getenv('STATE_DIR', str(DEFAULT_STATE))) / 'progress.json')
marker = state.get('canary', {})
trial = state.get('trial', {})
model = configured_model().preflight()['digest']
account = int(os.getenv('STEPIK_EXPECTED_USER_ID', '0'))
if (not marker.get('submission_id') or not marker.get('user_verified')
    or marker.get('account') != account or marker.get('model_digest') != model
    or trial.get('account') != account or trial.get('model_digest') != model
    or len(trial.get('steps', {})) < 3
    or any(j.get('phase') == 'sending' for j in state['jobs'].values())):
    raise SystemExit('Require account audit, successful canary, your --approve-canary, and three confirmed --trial solutions.')
PY
fi
task_tmp="$(mktemp -d)"
trap 'rm -rf "$task_tmp"' EXIT
python3 - "$task_root" "$task_user" "$task_tmp" "$HOME" <<'PY'
import sys
from pathlib import Path
root, user, output, home = sys.argv[1:]
if any(c in root + home for c in '\n\r"\\%'):
    raise SystemExit('Unsupported characters in project path')
for name in ('stepik-daily-bot.service', 'stepik-daily-bot.timer'):
    text = (Path(root) / name).read_text().replace('@PROJECT_DIR@', root).replace('@USER@', user).replace('@HOME_DIR@', home)
    (Path(output) / name).write_text(text)
PY
systemd-analyze verify "$task_tmp/stepik-daily-bot.service" "$task_tmp/stepik-daily-bot.timer"
sudo install -m 644 "$task_tmp/stepik-daily-bot.service" /etc/systemd/system/stepik-daily-bot.service
sudo install -m 644 "$task_tmp/stepik-daily-bot.timer" /etc/systemd/system/stepik-daily-bot.timer
sudo systemctl daemon-reload
if [[ "$task_enable" == --enable ]]; then
  sudo systemctl enable --now stepik-daily-bot.timer
else
  sudo systemctl disable --now stepik-daily-bot.timer
  echo 'Installed and verified. Timer remains disabled pending account trials.'
fi
systemctl list-timers stepik-daily-bot.timer --no-pager
