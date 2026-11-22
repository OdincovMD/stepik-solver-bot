"""Live local gateway auth/streaming/contention probe. Never contacts Stepik."""
import json
import resource
import threading
import time
from pathlib import Path

import requests

from config import DEFAULT_ENV, DEFAULT_STATE, load_env
from solver import LOCAL, configured_model
from storage import save_state


def main():
    load_env(DEFAULT_ENV)
    solver = configured_model()
    result = {"stepik_accessed": False, "model": solver.preflight()}
    session = requests.Session()
    session.trust_env = False
    for name, headers in (("missing_auth", {}), ("wrong_auth", {"Authorization": "Bearer invalid"}), ("stepik_auth", solver.headers())):
        with session.get(LOCAL + "/models", headers=headers, timeout=(5, 15), allow_redirects=False) as response:
            result[name] = response.status_code
    ready = threading.Event()

    def occupy():
        try:
            with session.post(LOCAL + "/chat/completions", headers=solver.headers(),
                              json={"model": solver.model, "messages": [{"role": "user", "content": "Перечисли числа от 1 до 100 через запятую."}],
                                    "max_tokens": 96, "stream": True, "temperature": 0},
                              stream=True, timeout=(5, 650), allow_redirects=False) as response:
                result["first_request_status"] = response.status_code
                ready.set()
                result["first_stream_complete"] = any(line.strip() == b"data: [DONE]" for line in response.iter_lines(chunk_size=1))
        except requests.RequestException:
            result["first_request_error"] = "transport"
        finally:
            ready.set()

    worker = threading.Thread(target=occupy, daemon=True)
    worker.start()
    if not ready.wait(30):
        raise ValueError("gateway_probe_start_timeout")
    started = time.monotonic()
    answer = solver.solve({"block": {"name": "number", "text": "Вычисли 17 * 23."}}, {})
    worker.join(timeout=650)
    result.update(second_answer_correct=answer == {"number": "391"}, second_metrics=solver.metrics,
                  elapsed_seconds=round(time.monotonic() - started, 3), client_peak_rss_MiB=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1))
    result["passed"] = (result["missing_auth"] == 401 and result["wrong_auth"] == 401 and result["stepik_auth"] == 200
                        and result.get("first_request_status") == 200 and result.get("first_stream_complete")
                        and result["second_answer_correct"] and result["second_metrics"]["busy_retries"] > 0)
    save_state(DEFAULT_STATE / "gateway-check.json", result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    from bot import safe_error
    import sys
    try:
        sys.exit(main())
    except Exception as exc:
        print("gateway_check_error: " + safe_error(exc), file=sys.stderr)
        sys.exit(1)
