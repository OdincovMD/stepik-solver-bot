#!/usr/bin/env python3
"""Browser QA against the public HTTPS URL; credentials never printed."""
import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as BrowserTimeout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="https://skin-cancer-ai.ru")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--tools-on", action="store_true")
    args = parser.parse_args()
    credentials = json.loads(args.credentials.read_text())
    failures, request_shapes, errors = [], [], []
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/snap/bin/chromium", headless=True,
                                             args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-quic", "--no-proxy-server"])
        context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True,
                                      has_touch=True, locale="ru-RU", reduced_motion="reduce")
        if args.tools_on:
            context.add_init_script("localStorage.setItem('LlamaUi.disabledToolCategories', '[]')")
        page = context.new_page()

        def response_received(response):
            if response.status >= 400:
                failures.append({"path": urlsplit(response.url).path, "status": response.status})
            if response.request.method == "POST" and "/v1/" in response.url:
                try:
                    body = response.request.post_data_json
                    request_shapes.append({"path": urlsplit(response.url).path, "keys": sorted(body.keys()),
                                           "status": response.status,
                                           "settings": {key: value for key, value in body.items() if key not in ("messages", "tools", "conversation_ids")},
                                           "tools_type": type(body.get("tools")).__name__, "tools_count": len(body.get("tools") or []),
                                           "tool_names": [tool.get("function", {}).get("name") for tool in body.get("tools", []) if isinstance(tool,dict)] if isinstance(body.get("tools"),list) else [],
                                           "message_shapes": [{"role": message.get("role"), "content_type": type(message.get("content")).__name__,
                                                               "parts": [part.get("type") for part in message.get("content", []) if isinstance(part, dict)] if isinstance(message.get("content"), list) else []}
                                                              for message in body.get("messages", [])]})
                except Exception:
                    pass

        page.on("response", response_received)
        page.on("pageerror", lambda error: errors.append(str(error)[:300]))
        page.on("requestfailed", lambda request: errors.append(urlsplit(request.url).path + ": " + str(request.failure)))

        def comparable_request(route):
            body = route.request.post_data_json
            body.update(cache_prompt=False, seed=42, temperature=0.2)
            route.continue_(post_data=json.dumps(body))

        page.route("**/v1/chat/completions", comparable_request)
        try:
            page.goto(args.base + "/llm/login", wait_until="domcontentloaded", timeout=30000)
        except BrowserTimeout:
            print(json.dumps({"stage":"initial_navigation", "url":page.url, "title":page.title(), "body":page.locator("body").inner_text()[:1000], "errors":errors},ensure_ascii=False),flush=True)
            page.screenshot(path=str(args.output / "navigation-failure.png"))
            raise
        page.get_by_role("button", name="Войти в чат", exact=True).click()
        assert page.locator("#feedback").inner_text() == "Введите имя пользователя и пароль."
        page.locator("#password").fill(credentials["password"])
        page.get_by_role("button", name="Показать", exact=True).click()
        assert page.locator("#password").get_attribute("type") == "text"
        page.get_by_role("button", name="Скрыть", exact=True).click()
        page.screenshot(path=str(args.output / "login-mobile.png"), full_page=True)
        page.get_by_role("button", name="Войти в чат", exact=True).click()
        page.wait_for_url(args.base + "/llm/", timeout=60000)
        page.wait_for_timeout(5000)
        if args.send:
            editor = page.locator('[contenteditable="true"]').first
            editor.wait_for(state="visible", timeout=20000)
            editor.fill("Назови столицу Франции одним словом.")
            sent = time.monotonic()
            page.get_by_role("button", name="Send", exact=True).click()
            try:
                page.wait_for_function("document.body.innerText.includes('Париж')", timeout=90000)
            except BrowserTimeout:
                print(json.dumps({"stage":"response_timeout","body_text":page.locator("body").inner_text()[:6000],"failures":failures,"request_shapes":request_shapes},ensure_ascii=False),flush=True)
                raise
            word_seconds = time.monotonic() - sent
            page.screenshot(path=str(args.output / "chat-response-mobile.png"), full_page=True)
            print(json.dumps({"stage":"after_send","body_text":page.locator("body").inner_text()[:6000],"failures":failures,"request_shapes":request_shapes},ensure_ascii=False),flush=True)
            assert "Париж" in page.locator("body").inner_text(), "The assistant response was not rendered"
            page.get_by_role("button", name="Send", exact=True).wait_for(state="visible", timeout=30000)
            editor.fill("Перечисли все числа от 1 до 1000.")
            page.get_by_role("button", name="Send", exact=True).click()
            page.get_by_role("button", name="Stop", exact=True).wait_for(state="visible", timeout=15000)
            page.wait_for_timeout(1500)
            page.get_by_role("button", name="Stop", exact=True).click()
            page.get_by_role("button", name="Send", exact=True).wait_for(state="visible", timeout=15000)
            print(json.dumps({"stage":"stop_button", "send_restored":True}),flush=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.wait_for_timeout(1000)
            page.screenshot(path=str(args.output / "chat-desktop.png"), full_page=True)
        page.screenshot(path=str(args.output / "chat-mobile.png"), full_page=True)
        state = {"url": page.url, "body_text": page.locator("body").inner_text()[:6000],
                 "textareas": page.locator("textarea").count(), "errors": errors,
                 "editors": page.locator('[contenteditable="true"]').count(),
                 "failures": failures, "request_shapes": request_shapes,
                 "horizontal_overflow": page.evaluate("document.documentElement.scrollWidth > innerWidth")}
        if args.send:
            state["first_answer_word_seconds"] = word_seconds
            state["tools_enabled_for_test"] = args.tools_on
            state["stop_verified"] = True
        (args.output / "browser-initial.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(state, ensure_ascii=False), flush=True)
        browser.close()


if __name__ == "__main__":
    main()
