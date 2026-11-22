#!/usr/bin/env python3
"""Bounded API probes using only the Python standard library."""
import argparse
import json
from pathlib import Path
import time
import urllib.request


def request(base, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(request(args.base_url, "/health")))
    print(json.dumps(request(args.base_url, "/v1/models")))
    cases = [
        ("russian", "Объясни по-русски в трёх коротких предложениях, что такое API."),
        ("extraction", 'Извлеки имя и город из текста: «Анна живёт в Казани». '
         'Верни только JSON с ключами name и city.'),
        ("grounding", 'Используй только факт: «Сервис Зефир работает на порту 8123». '
         'Какой порт у Зефира? Ответь одним числом.'),
    ]
    rows = []
    for name, prompt in cases:
        payload = {
            "model": "raspberry-llm",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2, "seed": 42, "max_tokens": 96,
            "stream": False, "cache_prompt": False,
        }
        if name == "extraction":
            payload["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        response = request(args.base_url, "/v1/chat/completions", payload)
        elapsed = time.monotonic() - started
        row = {
            "case": name, "wall_seconds": round(elapsed, 3),
            "usage": response.get("usage"), "timings": response.get("timings"),
            "message": response["choices"][0]["message"],
            "finish_reason": response["choices"][0].get("finish_reason"),
        }
        if name == "extraction":
            row["parsed_json"] = json.loads(row["message"]["content"])
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    if args.output:
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
