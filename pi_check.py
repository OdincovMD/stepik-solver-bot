"""Hardware inventory and synthetic local model benchmark, without any Stepik access."""
import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from bot import safe_error
from solver import configured_model
from config import DEFAULT_ENV, DEFAULT_STATE, load_env
from storage import save_state


class LoadMonitor:
    """Sample the already running llama process; never alter its scheduling."""
    def __init__(self):
        self.done = threading.Event()
        self.values = []
        self.pid = command("systemctl", "show", "raspberry-llm", "-p", "MainPID", "--value")
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def sample(self):
        while not self.done.is_set():
            try:
                fields = Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()
                mem = {line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
                       if line.startswith(("MemAvailable:", "SwapFree:"))}
                self.values.append((time.monotonic(), (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
                                    int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
                                    int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000,
                                    mem["MemAvailable"], mem["SwapFree"]))
            except (OSError, ValueError, KeyError, IndexError):
                pass
            self.done.wait(1)

    def start(self):
        self.thread.start()

    def finish(self):
        self.done.set()
        self.thread.join(timeout=2)
        if not self.values:
            return {"load_monitor": "unavailable"}
        first, last = self.values[0], self.values[-1]
        elapsed = last[0] - first[0]
        return {"llama_peak_rss_MiB": round(max(x[2] for x in self.values) / 2**20, 1),
                "llama_mean_cpu_percent": round(100 * (last[1] - first[1]) / elapsed, 1) if elapsed else None,
                "peak_temperature_C": max(x[3] for x in self.values),
                "min_available_memory_MiB": round(min(x[4] for x in self.values) / 1024, 1),
                "min_swap_free_MiB": round(min(x[5] for x in self.values) / 1024, 1)}


def command(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
        return result.stdout.strip() if result.returncode == 0 else "unavailable"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def resources():
    memory = {}
    if Path("/proc/meminfo").exists():
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                memory[key + "_KiB"] = int(value.split()[0])
    model = Path("/proc/device-tree/model")
    temp = Path("/sys/class/thermal/thermal_zone0/temp")
    disk = shutil.disk_usage(".")
    return {"architecture": platform.machine(), "python": platform.python_version(), "cpu_count": os.cpu_count(),
            "board": model.read_text().strip("\x00\n") if model.exists() else "not_detected_as_pi",
            "memory": memory, "disk_free_bytes": disk.free,
            "temperature_C": int(temp.read_text().strip()) / 1000 if temp.exists() else None,
            "throttled": command("vcgencmd", "get_throttled"),
            "clock_synchronized": command("timedatectl", "show", "-p", "NTPSynchronized", "--value")}


def samples():
    return [
        ("number", "Вычисли 17 * 23. Верни целое число.", None, {"number": "391"}),
        ("choice", "Выбери единственное простое число.",
         {"options": ["9", "11", "21"], "is_multiple_choice": False}, {"choices": [False, True, False]}),
        ("choice", "Выбери все чётные числа.",
         {"options": ["2", "3", "8"], "is_multiple_choice": True}, {"choices": [True, False, True]}),
        ("sorting", "Расположи числа по возрастанию.", {"options": ["30", "10", "20"]}, {"ordering": [1, 2, 0]}),
        ("matching", "Сопоставь названия чисел на русском и английском.",
         {"pairs": [{"first": "один", "second": "two"}, {"first": "два", "second": "one"}]}, {"ordering": [1, 0]}),
        ("string", "Напиши слово Python наоборот, сохрани регистр букв.", None, {"text": "nohtyP"}),
        ("code", "Прочитай два целых числа из stdin, выведи их сумму.", None, None),
    ]


def benchmark(solver, repeats):
    result = []
    for repeat in range(repeats):
        for index, (kind, text, dataset, expected) in enumerate(samples()):
            step = {"block": {"name": kind, "text": text}}
            if kind == "code":
                step["block"]["options"] = {"code_templates": {"python3": "# stdin -> stdout"}}
            item = {"repeat": repeat, "sample": index, "type": kind}
            monitor = LoadMonitor()
            monitor.start()
            try:
                answer = solver.solve(step, {"dataset": dataset})
                item.update(solver.metrics)
                item["semantic_correct"] = answer == expected if expected is not None else None
                item["validation"] = "syntax_only_not_executed" if kind == "code" else "compared_with_known_answer"
                timings = item.get("timings", {})
                item["tokens_per_second"] = timings.get("predicted_per_second")
            except Exception as exc:
                item["error"] = safe_error(exc)
                item["semantic_correct"] = False if expected is not None else None
            finally:
                item.update(monitor.finish())
            result.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--output", type=Path, default=DEFAULT_STATE / "hardware.json")
    args = parser.parse_args()
    load_env(args.env_file)
    if not 1 <= args.repeats <= 5:
        parser.error("repeats must be 1..5")
    report = {"at": datetime.now(timezone.utc).isoformat(), "before": resources(),
              "stepik_accessed": False, "model_measured": False}
    exit_code = 0
    print(json.dumps(report, ensure_ascii=False), flush=True)
    try:
        if args.benchmark:
            solver = configured_model()
            report["model"] = solver.preflight()
            report["samples"] = benchmark(solver, args.repeats)
            report["model_measured"] = True
            judged = [x for x in report["samples"] if x.get("semantic_correct") is not None]
            report["synthetic_correct"] = sum(x["semantic_correct"] for x in judged)
            report["synthetic_total"] = len(judged)
            elapsed = [x["wall_seconds"] for x in report["samples"] if "wall_seconds" in x]
            report["median_seconds_per_sample"] = round(statistics.median(elapsed), 2) if elapsed else None
            rate = report["synthetic_correct"] / len(judged) if judged else 0
            report["optimistic_hours_for_26"] = round(statistics.mean(elapsed) * 26 / rate / 3600, 2) if elapsed and rate else None
            report["estimate_note"] = "Synthetic tasks only; excludes Stepik API, harder tasks and skipped tasks; not a guarantee."
            if any("error" in x for x in report["samples"]):
                exit_code = 2
    except Exception as exc:
        report["error"] = safe_error(exc)
        exit_code = 1
    finally:
        report["after"] = resources()
        save_state(args.output, report)
        print(json.dumps({k: v for k, v in report.items() if k != "samples"}, ensure_ascii=False), flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
