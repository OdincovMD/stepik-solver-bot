# Personal chat contract

Source: current owner request; docs/README.md; docs/remote-access.md.
Locale of owned UI: ru. Chat is the bundled llama.cpp UI; no local fork.

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Form | web/login.html | Owner request: private chat | Login | test_gateway.py and browser |
| Scrollbar | web/login.html :root | DESIGN.md | Login; upstream chat retained | Narrow viewport/browser |
| Feedback | #feedback live region | Login implementation | Validation, authentication, network | Browser and test_gateway.py |
| Streaming | llama.cpp chat and gateway.py | OpenAI-compatible API | SSE, nonstream API | Real external requests/browser |

## Access and data

Browser: separate password, 12-hour Secure/HttpOnly/SameSite=Strict host cookie.
Machine clients: separate named Bearer keys, individually revocable. A wrong
Bearer key must not fall through to a browser cookie. Browser mutations require
the exact HTTPS Origin. This is independent of dashboard PAM authorization.
Credentials are stored outside the source tree, with mode 0600. No prompts,
request bodies, cookies or authorization headers are written to access logs.

API error shape: {error:{message,type,code}} before streaming. One generation
is active; another authorized request receives 429 plus Retry-After, without
an unbounded queue. Errors after SSE headers are SSE error events, not a second
HTTP response. Browser disconnect closes upstream inference within a heartbeat.
Browser tool categories default to disabled through the pinned UI's own local
preference key. Existing owner preferences are preserved; backend tools remain
unpublished regardless of browser choices. A gateway restart invalidates all
web sessions, while API keys remain valid.

## Flow ledger

- Login → pending label → chat; failure preserves username, clears password,
  announces an error and focuses the password field. No duplicate request.
- Chat submission/cancel/history: upstream built-in behavior, checked in browser.
- Session rotation → previous cookies rejected; API keys remain independent.
- Sign out through the account/login page → expire cookie and return to login.
- API key revocation → next request receives 401 without a service restart.

## State and retention

There is no new server-side chat database. Browser history behavior belongs to
the bundled UI and must be documented after inspection. The gateway does not
retain conversations. Configurations and test artifacts must contain no secrets.
