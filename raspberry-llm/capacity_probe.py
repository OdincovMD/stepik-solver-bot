#!/usr/bin/env python3
"""Check streamed inference and a longer prompt on the deployed CPU service."""
import json
from pathlib import Path
import time
import urllib.request


def main():
    prompt = (
        "Используй только факты из заметок. Ответь одним числом.\n"
        + "Заметка: сервер обрабатывает запросы приложений локально.\n" * 70
        + "Факт: сервис Зефир работает на порту 8123. Какой порт у Зефира?"
    )
    data = json.dumps({
        "model": "raspberry-llm", "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2, "seed": 42, "max_tokens": 24, "stream": True,
        "stream_options": {"include_usage": True}, "cache_prompt": False,
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:11434/v1/chat/completions", data=data,
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    first_token = None
    chunks = []
    usage = None
    with urllib.request.urlopen(req, timeout=180) as response:
        for line in response:
            if not line.startswith(b"data: "):
                continue
            raw = line[6:].strip()
            if raw == b"[DONE]":
                break
            event = json.loads(raw)
            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices", []):
                content = choice.get("delta", {}).get("content", "")
                if content:
                    if first_token is None:
                        first_token = time.monotonic() - started
                    chunks.append(content)
    result = {
        "case": "long_prompt_streaming", "content": "".join(chunks),
        "ttft_seconds": first_token, "wall_seconds": time.monotonic() - started,
        "usage": usage, "correct": "".join(chunks).strip() == "8123",
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    Path("results/capacity.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    if not result["correct"]:
        raise SystemExit("Unexpected answer to the provided fact")


if __name__ == "__main__":
    main()
