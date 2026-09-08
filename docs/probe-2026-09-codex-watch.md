# Probe: can `shim watch` sit in front of Codex?

**Date:** 8 September 2026 · **Codex:** `codex-cli 0.151.0` · **Platform:** macOS 26.4.0
arm64 · **Auth:** ChatGPT sign-in, no API key configured · **Harness:**
`scripts/probe/probe_codex_watch.py` · **Capture:**
`tests/fixtures/probe/codex-watch/probe-2026-09-08.json`

## Verdict

**The transport works. The shipped implementation does not use it.**

`chatgpt.com` accepted a request re-sent by Python's `http.client`, the WebSocket
attempt fell back to HTTP cleanly, and every request the client made arrived at
the proxy — but only when the base URL was passed as a **config override**.
`shim watch -- codex` sets the environment variable `OPENAI_BASE_URL`, which
Codex 0.151.0 ignores, so today the command starts a proxy, runs the session
straight past it, and reports nothing measured while exiting 0.

So: `go` for PRD-13, and `shim watch -- codex` must stop claiming to measure
until PRD-13 lands. PRD-08's listing may say **prompt hook** for Codex and may
not say `shim watch`.

## Method

A loopback HTTP/1.1 server standing in for the proxy: it answers any request
carrying `Upgrade: websocket` with `426 Upgrade Required`, and forwards
everything else with `http.client.HTTPSConnection` and a default `ssl`
context — the same client and the same context `watch/proxy.py` uses, because
the open question was whether that client's TLS handshake would be rejected.
Requests carrying `ChatGPT-Account-ID` go to `chatgpt.com`; the rest would go to
`api.openai.com` with `/backend-api/codex/` rewritten to `/v1/`.

Recorded per exchange: method, path, header *names*, whether the account header
was present, upstream host and status, whether `cf-ray` came back, content type,
SSE event names in order, and the key names of any `usage` object. No token, no
account id, no request or response body.

## T1 — Routing

| Run | `-c openai_base_url` | `OPENAI_BASE_URL` | Reached the proxy |
| --- | --- | --- | --- |
| config-override | set | set | **yes** |
| environment-only | not set | set | **no** — nothing arrived at all |

With the override, two paths arrived, both under the prefix the client was
given:

- `GET /backend-api/codex/models?client_version=0.151.0`
- `POST /backend-api/codex/responses`

**Q7.1 is answered: the shipped build does not honour `OPENAI_BASE_URL`.** The
source reading in PRD-07 said the variable is read only by the network-proxy
credential broker, and the run agrees. `shim watch` sets exactly that variable
and nothing else, which is why this had to be measured rather than assumed.

## T2 — WebSocket

An upgrade **was** attempted, on the same path the turn later used:

| At (s) | Event |
| --- | --- |
| 2.081 | `GET /backend-api/codex/responses` with `Upgrade: websocket` → answered `426` |
| 2.683 | `POST /backend-api/codex/responses` over HTTP |

The fallback took **0.602 s** and reached the same path. Answering 426 is
sufficient and the client did not retry the upgrade. A proxy that refuses the
upgrade any other way would end the turn as an error, so 426 is not an
implementation detail but a requirement on PRD-13.

## T3 — Upstream acceptance

**This was the risk that could have sunk the approach, and it did not.**

| Request | Upstream | Status | `cf-ray` |
| --- | --- | --- | --- |
| `GET …/models` | `chatgpt.com` | **200** | yes |
| `POST …/responses` | `chatgpt.com` | 429 | yes |

Cloudflare fronted both — `cf-ray` came back on each — and neither was a
challenge page or a 403. The `GET` returned a 200 with the real model catalogue,
which is a complete round trip through Python's `http.client` with a stock TLS
context. **There is no TLS-fingerprint rejection**, and Q7.2 does not need its
`curl` replay.

The 429 is the account's own quota (`usage_limit_reached`), which is the
application answering a request it accepted and routed. It is evidence for the
transport, not against it.

## T4 — Usage shape

**Not captured.** The quota rejection carries no `usage` object, so the event
that reports usage and its key names — including whether
`input_tokens_details.cached_tokens` is present — remain unknown. PRD-13 R5 and
PRD-16 still need this. It needs one successful Codex turn, which needs quota
that resets in October, or an API key.

## T5 — API-key mode

**Not run.** `~/.codex/auth.json` holds ChatGPT tokens and no API key, and none
was set in the environment. The `/v1/` rewrite path in the harness was therefore
never exercised against a real upstream.

## T6 — Codex hook events

**Not run.** A tool-using turn needs a model response, which the quota refused.
Separately, and outside this harness, Codex 0.151.0's prompt hook was exercised
end to end the same day: see the Codex row in `docs/compatibility.md`.

## T7 — Claude header shapes (R7b)

One Claude Code request through the same harness, forwarded to
`api.anthropic.com`, status **200**. A subscription sign-in sends:

- `authorization` — present
- `x-api-key` — **absent**
- `anthropic-beta`, `anthropic-version`, `anthropic-dangerous-direct-browser-access`

So PRD-16 can classify billing mode from the auth header shape: a bearer in
`authorization` with no `x-api-key` is a subscription. The API-key shape is not
captured here and should not be assumed to be its mirror image until it is.

## What this changes now

`cli/watch.py` refuses `codex` with the reason, rather than starting a proxy the
client will not use. The README and `docs/compatibility.md` say prompt hook only
for Codex. PRD-13 is scheduled with T1 to T3 as its foundation and T4 as its
first open question.
