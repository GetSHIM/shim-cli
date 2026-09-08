# Compatibility and evidence

## Supported surface

| Area | Status |
| --- | --- |
| Python | CPython 3.10 through 3.13 for the package; the plugin archive runs on 3.9 through 3.13 |
| Operating systems | macOS and Linux target |
| Prompt hooks | Codex CLI, Claude Code, and GitHub Copilot CLI |
| Tool hooks | Claude Code `PreToolUse` and `PostToolUse` only |
| `shim watch` | Claude Code only. Codex is refused: it reads its endpoint from its own configuration, so the proxy is bypassed and the session measured as empty ([probe](probe-2026-09-codex-watch.md)). Copilot out of scope because a custom endpoint removes GitHub authentication |

## Deprecated names

The package was renamed from `shim_guard` to `shim_cli` in 0.3.0. Three names
survive so that an install written by 0.2.0 keeps working, and all three are
removed in **0.5.0**, the second minor release after 0.3.0:

| Name | Replacement |
| --- | --- |
| the importable `shim_guard` package, including `python -m shim_guard.hook` | `shim_cli`, `python -m shim_cli.hook` |
| the `shim-guard-hook` console script | `shim-hook` |
| the `SHIM_GUARD_CONFIG` variable | `SHIM_CONFIG`, which outranks it |
| the `shim-guard` marketplace entry in both plugin manifests | the `shim-cli` entry |

The compatibility package re-exports and does nothing else. It emits no
deprecation warning: the hook is a cold-start subprocess whose stderr the
client shows to the user, and a warning on every event is noise.

The plugin marketplaces carry a second `shim-guard` entry pointing at the same
directory, also removed in 0.5.0. On Claude Code that entry is enough: the
marketplace key is whatever the user typed when they added it, so a plugin
installed as `shim-guard@shim-guard` keeps loading and updating with no action.

**Codex needs one migration.** There the marketplace name in the manifest *is*
the identity, so renaming it to `shim-cli` orphans an install made under the old
name — `config.toml` still says the plugin is enabled while `codex plugin list`
reports nothing installed. A Codex user who installed the 0.2.0 plugin runs:

```
codex plugin remove shim-guard
codex plugin marketplace remove shim-guard
codex plugin marketplace add https://github.com/GetSHIM/shim-cli
codex plugin add shim-cli@shim-cli
```

This affects the plugin only. A Codex user who installed the PyPI package is
unaffected, and the zero-install plugin path could not reach the archive under
Codex before 0.3.0, so no working Codex plugin install is being broken.

Codex and Copilot install prompt hooks only. The repository contains no
Codex, Copilot, `PostToolUseFailure`, or `PostToolBatch` tool adapter. Tool
coverage is based on live protocol evidence rather than documentation and is
printed by `shim doctor <client>`.

The local integrations follow the client-native protocol references: the
[Codex hook documentation](https://developers.openai.com/codex/hooks/), Claude
Code [hooks](https://code.claude.com/docs/en/hooks) and
[settings](https://code.claude.com/docs/en/settings), and GitHub's
[Copilot hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference).
Codex leaves inline `config.toml` hooks untouched. Claude uses shell-free
arguments and native structured tool responses. Copilot uses
`userPromptTransformed` to replace the model-facing prompt; the original can
remain visible in its timeline.

**A Codex hook does not run until it is trusted.** From 0.151.0 Codex holds a
persisted trust record per hook and silently skips any hook it does not have
one for: no warning, no line in the transcript, and prompts reach the model
uninspected. Writing the fragment is therefore only half of `shim install
codex` — review and trust it in Codex, which is why `shim doctor codex` ends
on `Codex hook activation is client UI state; verify SHIM with /hooks`. shim
cannot read that record and does not write it; a diagnosis that claimed to
would be guessing. `codex exec --dangerously-bypass-hook-trust` runs enabled
hooks without it, which is useful to confirm an install and wrong as a habit.

## 0.3.0 release evidence

PENDING_RELEASE_EVIDENCE

## Dated development evidence

These facts guided implementation. They are not evidence for the 0.2.0 tag and
must not be copied into its release record without a fresh run.

| Evidence | Recorded result |
| --- | --- |
| Locally inspected prompt clients | Codex CLI 0.149.0, Claude Code 2.1.251, GitHub Copilot CLI 1.0.80 |
| Claude tool protocol probe | Claude Code 2.1.250 on 29 August 2026; 71 sanitised fixtures from seven events |
| Interactive prompt clients | Codex ChatGPT sign-in, Claude first-party sign-in, and GitHub Copilot OAuth exercised on macOS 26.5.2 arm64 |
| Hook activation and timeout behavior | Hooks reviewed and activated; safe and finding prompts exercised; forced timeout or error observed to fail open at the client boundary |
| `shim watch` | Claude verified end to end on 30 August 2026 against a live subscription sign-in; request forwarded unchanged, streaming preserved, and provider usage read from the wire |
| `shim watch` scan scope | Claude Code 2.1.263 on 8 September 2026. One minimal request measured 191,599 bytes, 921 text leaves and 169,134 characters; a working request measured 4,402 leaves, 256,517 characters and 35 levels of nesting, the depth coming from an MCP tool's recursive JSON schema. The hook's own limits (2,000 leaves, 200,000 characters, depth 24) would report almost every real request as unmeasured, so the proxy carries its own. |
| `shim watch` both directions | Claude Code 2.1.263 on 8 September 2026. A synthetic three-IBAN file read through the Read tool and echoed back reported `request 3 IBAN in messages`, `response 3 IBAN in model text`, `compare IBAN 3 in request, 3 in response`. |
| `Stop` model output | Claude Code 2.1.263 on 8 September 2026. `last_assistant_message` is present and carries **only the turn's last text block**: a turn that said `CHECKING`, called `Read`, then answered held just the answer. Text the model produced before a tool call in the same turn is not counted. Capture: `tests/fixtures/probe/claude/Stop-none-model-reply-1.json`. |
| Codex `shim watch` transport | Codex CLI 0.151.0 on 8 September 2026, ChatGPT sign-in, macOS 26.4.0 arm64. With the base URL passed as a config override every request reached the proxy; with `OPENAI_BASE_URL` alone **nothing did**. `chatgpt.com` returned 200 to a request re-sent by Python's `http.client` with a stock TLS context, `cf-ray` present, no challenge — so there is no fingerprint rejection. A WebSocket upgrade was attempted and fell back to HTTP 0.602 s after a 426. The usage shape is still uncaptured: the account's quota returned 429 before any turn completed. [Probe](probe-2026-09-codex-watch.md). |
| Claude auth header shape | Claude Code subscription sign-in on 8 September 2026 sends `authorization` and no `x-api-key`, with `anthropic-beta` and `anthropic-version`; upstream 200 through the same harness. |
| Codex live prompt hook | Codex CLI 0.151.0 on 8 September 2026, ChatGPT sign-in, macOS 26.4.0 arm64. With the hook trusted, a prompt carrying a synthetic address reported `hook: UserPromptSubmit Completed` under `observe` and `hook: UserPromptSubmit Blocked` under `enforce`, the blocked prompt never reaching the model. The same prompt with the hook untrusted produced no hook line at all and was sent unchanged. |
| `Stop` scan cost | 66 KB final assistant text, hook end to end: 41 ms median, 50 ms p95 on macOS 26.5.2 arm64, CPython 3.13.5. Text beyond the detector's 100,000-character limit is not scanned and the record says `truncated`. |

The native Claude capture and the decisions made from it are preserved in the
[August 2026 probe](probe-2026-08.md). Repository fixtures contract the prompt
shape for all three clients and the two installed Claude tool-event shapes.

## Detector migration evidence

The evaluation unit is exact output, not category presence. The old
`guard-v1` corpus asserted only category sets, so a finding at the wrong offset
could pass. It has been superseded by:

| Corpus | Cases | Contract |
| --- | ---: | --- |
| `guard-v2.json` | 53 | Exact redacted output for every case, plus source spans for normalization-sensitive cases. |
| `guard-tools-v1.json` | 24 | Exact output at 25 scanned paths in captured tool payloads, per event and policy direction. |
| `parity-v1.json` | 475 | Exact findings, spans, scores, and redacted output from the previous Presidio implementation. |

Of the 475 parity cases, 473 remain byte-identical. The two intentional
differences are `0.0.0.0` and `::1`, which identify no person or remote host and
whose masking erased a meaningful bind-address distinction. Both live in
`DELIBERATE_DIVERGENCES` with reasons and tightly pinned new output. The parity
corpus is generated migration evidence and must never be regenerated to make a
test pass.

The fixture-bound metrics report 100% synthetic precision, recall, and exact
output. That is deterministic contract evidence, not a real-world statistical
guarantee. Every implementation category has a positive and a targeted safe
negative, and the secret-assignment rule has prose negatives.

The detector is first-party and offline. `presidio-analyzer`, its spaCy
pipeline, and `tldextract` were removed; recognizers, checksums, and the public
suffix table are shipped in the package. `phonenumbers` is the one third-party
module on the hook path, enforced by the import contract.

## Historical performance evidence

Before the 0.2.0 release, 20 safe and 20 blocking fresh-process invocations of
the installed package were alternated without warm-up on Darwin 25.4.0 arm64,
macOS 26.4, and CPython 3.13.5:

| Fixture | p50 | p95 | Maximum |
| --- | ---: | ---: | ---: |
| Safe prompt | 63 ms | 66 ms | 67 ms |
| Email block | 64 ms | 67 ms | 69 ms |

Earlier hook-path measurements were:

| Hook path | Interpreter | p50 |
| --- | --- | ---: |
| Installed package | CPython 3.13 | 55–70 ms |
| Bundled `shim.pyz` | CPython 3.13 | about 110 ms |
| Bundled `shim.pyz` | macOS system CPython 3.9.6 | about 205–280 ms |
| Nothing runnable, allow and warn | — | about 6 ms |

The previous Presidio-based implementation measured p50 2,410 ms and p95
4,262 ms on the same development line. Host load dominates these figures, so
they are historical comparisons rather than a release guarantee. Detector
analysis has a 20-second deadline, the outer hook has a 25-second deadline, and
client settings use 30 seconds.

## 0.2.0 release evidence

Evidence was recorded from the `shim` 0.2.0 wheel on 2 September 2026. The
release owner accepted the remaining client-boundary risk and directed the
release without further local client testing.

| Required evidence | 0.2.0 value |
| --- | --- |
| Supported client versions and platform | Codex CLI 0.152.0, Claude Code 2.1.210, and GitHub Copilot CLI 1.0.82 inspected on macOS 26.6.2 arm64 with CPython 3.13.9 |
| Authentication routes | Codex ChatGPT sign-in confirmed; Claude and Copilot routes not revalidated |
| Trusted-hook activation | Exact Codex user hook reviewed and trusted; Claude and Copilot hook configurations installed and diagnosed but not activated in a live client |
| Safe, finding, timeout, and error behavior | Codex safe and synthetic-email finding paths passed; remaining live client and fault paths not rerun; protocol fixtures remain enforced in CI |
| Synthetic corpus and quality metrics | `guard-v2`, `guard-v2-metrics.json`, and `guard-tools-v1.json` |
| Fresh-process latency | `benchmark-hook.json`, generated from the tag |

Repository settings must protect `v*` tags and require reviewers for the
`release` and `pypi` environments; workflow code cannot enforce those
GitHub-side controls.

The tag workflow builds and tests the wheel and source distribution, then
builds the tagged source again on a separate runner and requires byte-identical
artifacts. It generates the plugin archive, locked runtime requirements,
benchmark, hashes, SBOM, and attestations. Release assets also include the
detector corpora and this compatibility record. These checks remain redundant
because they protect different trust boundaries.

The release record must agree with [the 0.2.0 release notes](releases/0.2.0.md),
package and plugin versions, the lock, the committed plugin archive, and the
tag name before the evidence marker is removed.
