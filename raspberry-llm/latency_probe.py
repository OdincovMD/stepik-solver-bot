#!/usr/bin/env python3
"""Comparable, uncached/cached latency probes with process and thermal samples."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.request


def process_sample(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().split()
    memory = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in line)
    temperature = float(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    return {"monotonic": time.monotonic(), "cpu_seconds": (int(fields[13]) + int(fields[14])) / os.sysconf("SC_CLK_TCK"),
            "rss_kib": int(memory["VmRSS"].split()[0]), "temperature_c": temperature}


def probe(base, prompt, cached, label, pid):
    payload = {"model": "raspberry-llm", "messages": [{"role": "user", "content": prompt}], "max_tokens": 32,
               "temperature": 0.2, "seed": 42, "cache_prompt": cached, "stream": True, "stream_options": {"include_usage": True}}
    req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    samples = [process_sample(pid)]
    stopped = threading.Event()

    def monitor():
        while not stopped.wait(1):
            samples.append(process_sample(pid))

    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    started = time.monotonic()
    first = None
    content = ""
    usage = None
    timings = None
    try:
        with urllib.request.urlopen(req, timeout=600) as response:
            for line in response:
                if not line.startswith(b"data: "):
                    continue
                raw = line[6:].strip()
                if raw == b"[DONE]":
                    break
                data = json.loads(raw)
                usage = data.get("usage") or usage
                timings = data.get("timings") or timings
                for choice in data.get("choices", []):
                    text = choice.get("delta", {}).get("content") or ""
                    if text and first is None:
                        first = time.monotonic() - started
                    content += text
    finally:
        stopped.set()
        monitor_thread.join(timeout=2)
        samples.append(process_sample(pid))
    elapsed = time.monotonic() - started
    return {"case": label, "cache_prompt": cached, "ttft_seconds": first, "wall_seconds": elapsed, "usage": usage,
            "timings": timings, "content": content,
            "cpu_percent_mean": (samples[-1]["cpu_seconds"] - samples[0]["cpu_seconds"]) / elapsed * 100,
            "rss_peak_kib": max(sample["rss_kib"] for sample in samples),
            "temperature_peak_c": max(sample["temperature_c"] for sample in samples), "samples": samples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pid = int(subprocess.check_output(["systemctl", "show", "raspberry-llm", "--value", "-p", "MainPID"]))
    short = "Объясни по-русски одним предложением, что такое API."
    long = "Используй только факты из заметок. Ответь одним числом.\n" + "Заметка: сервер обрабатывает запросы приложений локально.\n" * 70 + "Факт: сервис Зефир работает на порту 8123. Какой порт у Зефира?"
    rows = []
    for label, prompt, cached in (("short_uncached_1", short, False), ("short_uncached_2", short, False),
                                   ("long_uncached_1", long, False), ("long_uncached_2", long, False), ("long_cached", long, True)):
        row = probe(args.base_url, prompt, cached, label, pid)
        rows.append(row)
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({key: value for key, value in row.items() if key != "samples"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
