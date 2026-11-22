#!/usr/bin/env python3
"""Optional client QA; OpenAI SDK is not a gateway/server dependency."""
import argparse
import json
from pathlib import Path
import time

import openai
from openai import OpenAI


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    credentials = json.loads(args.credentials.read_text())
    client = OpenAI(base_url="https://skin-cancer-ai.ru/v1", api_key=credentials["api_keys"]["personal"], timeout=650, max_retries=0)
    assert "raspberry-llm" in [model.id for model in client.models.list().data]
    started = time.monotonic()
    content, count = "", 0
    for event in client.chat.completions.create(model="raspberry-llm", messages=[{"role":"user","content":"Скажи одним словом: готово."}], max_tokens=12, stream=True):
        if event.choices:
            value = event.choices[0].delta.content or ""
            if value:
                content += value
                count += 1
    assert content and count > 1
    report = {"sdk_version":openai.__version__,"models_list":True,"streaming_completion":True,"content_events":count,"wall_seconds":time.monotonic()-started}
    args.output.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report),flush=True)
    client.close()


if __name__ == "__main__":
    main()
