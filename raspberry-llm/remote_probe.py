#!/usr/bin/env python3
"""Verify the real HTTPS route. Reads secrets from a private file, logs none."""
import argparse
import http.client
import json
from pathlib import Path
import ssl
import time
from urllib.parse import urlencode, urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("api", "access", "stream", "cancel"))
    parser.add_argument("--base", default="https://skin-cancer-ai.ru")
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    address = urlsplit(args.base)
    if address.scheme != "https":
        raise SystemExit("HTTPS is required")
    credentials = json.loads(args.credentials.read_text())
    authorization = {"Authorization": "Bearer " + credentials["api_keys"]["personal"]}
    results = {"base": args.base, "mode": args.mode, "tls_verified": True, "checks": []}

    def connect():
        return http.client.HTTPSConnection(address.hostname, address.port or 443, timeout=650, context=ssl.create_default_context())

    def request(method, path, body=None, headers=None):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            headers = {"Content-Type": "application/json", **(headers or {})}
        connection = connect()
        started = time.monotonic()
        connection.request(method, path, body=body, headers={"tuna-skip-browser-warning": "1", **(headers or {})})
        response = connection.getresponse()
        status, reply_headers, reply_body = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, reply_headers, reply_body, time.monotonic() - started

    def check(name, actual, expected):
        row = {"check": name, "status": actual, "expected": expected, "ok": actual == expected}
        results["checks"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if actual != expected:
            args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
            raise SystemExit("External verification failed: " + name)

    payload = {"model": "raspberry-llm", "messages": [{"role": "user", "content": "Скажи одним словом: готово."}], "max_tokens": 8, "stream": False}
    if args.mode == "api":
        status, _, body, elapsed = request("GET", "/v1/models", headers=authorization)
        check("authenticated_models", status, 200)
        assert any(item["id"] == "raspberry-llm" for item in json.loads(body)["data"])
        status, _, body, elapsed = request("POST", "/v1/chat/completions", payload, authorization)
        check("authenticated_completion", status, 200)
        results["completion"] = {"wall_seconds": elapsed, "usage": json.loads(body).get("usage")}
    elif args.mode == "access":
        check("unauthenticated_models", request("GET", "/v1/models")[0], 401)
        check("unauthenticated_completion", request("POST", "/v1/chat/completions", payload)[0], 401)
        check("invalid_bearer", request("GET", "/v1/models", headers={"Authorization": "Bearer invalid"})[0], 401)
        status, headers, _, _ = request("GET", "/llm/")
        check("unauthenticated_chat", status, 303)
        assert headers.get("Location") == "/llm/login"
        form = urlencode({"username": credentials["username"], "password": credentials["password"]})
        status, headers, _, _ = request("POST", "/llm/login", form, {"Content-Type": "application/x-www-form-urlencoded", "Origin": args.base})
        check("web_login", status, 200)
        cookie_header = headers["Set-Cookie"]
        assert all(value in cookie_header for value in ("Secure", "HttpOnly", "SameSite=Strict"))
        cookie = cookie_header.split(";")[0]
        check("authenticated_chat", request("GET", "/llm/", headers={"Cookie": cookie})[0], 200)
        check("csrf_rejected", request("POST", "/v1/chat/completions", payload, {"Cookie": cookie, "Origin": "https://invalid.example"})[0], 403)
        check("protected_tools", request("POST", "/llm/tools", {}, authorization)[0], 404)
        check("protected_metrics", request("GET", "/llm/metrics", headers=authorization)[0], 404)
        check("web_logout", request("POST", "/llm/logout", b"", {"Cookie": cookie, "Origin": args.base})[0], 200)
        check("old_session_revoked", request("GET", "/v1/models", headers={"Cookie": cookie})[0], 401)
        check("api_independent_of_web_logout", request("GET", "/v1/models", headers=authorization)[0], 200)
        oversized = {**payload, "messages": [{"role": "user", "content": "context " * 4000}]}
        check("context_overflow_rejected", request("POST", "/v1/chat/completions", oversized, authorization)[0], 400)
        check("output_limit_rejected", request("POST", "/v1/chat/completions", {**payload, "max_tokens": 9999}, authorization)[0], 400)
    elif args.mode == "cancel":
        connection = connect()
        payload.update(messages=[{"role":"user","content":"Служебная заметка для проверки отмены. " * 100}],stream=True,cache_prompt=False)
        connection.request("POST", "/v1/chat/completions", json.dumps(payload).encode(),headers={**authorization,"Content-Type":"application/json"})
        response = connection.getresponse()
        check("cancel_stream_started",response.status,200)
        response.readline()
        time.sleep(1)
        response.close()
        connection.close()
        closed = time.monotonic()
        retries = 0
        while time.monotonic() - closed < 20:
            status, _, _, _ = request("POST", "/v1/chat/completions", {"model":"raspberry-llm","messages":[{"role":"user","content":"Скажи: да."}],"max_tokens":2},authorization)
            if status == 200:
                break
            if status != 429:
                raise SystemExit("Unexpected response after cancellation")
            retries += 1
            time.sleep(.5)
        check("inference_available_after_cancel",status,200)
        results["cancellation"]={"recovered_seconds":time.monotonic()-closed,"busy_retries":retries}
        print(json.dumps(results["cancellation"]),flush=True)
    else:
        prompt = "Используй только факты из заметок. Ответь одним числом.\n" + "Заметка: сервер обрабатывает запросы приложений локально.\n" * 70 + "Факт: сервис Зефир работает на порту 8123. Какой порт у Зефира?"
        payload.update(messages=[{"role": "user", "content": prompt}], stream=True, cache_prompt=False, stream_options={"include_usage": True})
        connection = connect()
        started = time.monotonic()
        connection.request("POST", "/v1/chat/completions", json.dumps(payload).encode(), headers={**authorization, "Content-Type": "application/json", "tuna-skip-browser-warning": "1"})
        response = connection.getresponse()
        check("stream_status", response.status, 200)
        assert response.getheader("Content-Type").startswith("text/event-stream")
        results["headers_seconds"] = time.monotonic() - started
        check("concurrent_request", request("POST", "/v1/chat/completions", {**payload, "stream": False}, authorization)[0], 429)
        content, arrivals, comments, usage, first, finished = "", [], 0, None, None, False
        for line in response:
            elapsed = time.monotonic() - started
            if line.startswith(b":"):
                comments += 1
            if not line.startswith(b"data: "):
                continue
            raw = line[6:].strip()
            if raw == b"[DONE]":
                finished = True
                break
            event = json.loads(raw)
            if event.get("error"):
                raise SystemExit("SSE returned an error")
            usage = event.get("usage") or usage
            for choice in event.get("choices", []):
                text = choice.get("delta", {}).get("content") or ""
                if text:
                    if first is None:
                        first = elapsed
                    arrivals.append(elapsed)
                    content += text
        connection.close()
        assert finished and content.strip() == "8123" and len(arrivals) > 1
        results["stream"] = {"ttft_seconds": first, "wall_seconds": time.monotonic() - started,
                             "content_event_times": arrivals, "keepalive_comments": comments, "usage": usage,
                             "done_received": finished, "correct": True}
        print(json.dumps(results["stream"], ensure_ascii=False), flush=True)
        check("service_available_after_completion", request("GET", "/v1/models", headers=authorization)[0], 200)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
