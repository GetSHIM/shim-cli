# Compatibility and evidence

## Supported surface

| Area | Status |
| --- | --- |
| Python | CPython 3.10 through 3.13 for the package; the plugin archive runs on 3.9 through 3.13 |
| Operating systems | macOS and Linux target |
| Prompt hooks | Codex CLI, Claude Code, and GitHub Copilot CLI |
| Tool hooks | Claude Code `PreToolUse` and `PostToolUse` only |
| `shim watch` | Claude Code only. Codex is refused: it reads its endpoint from its own configuration, so the proxy is bypassed and the session measured as empty ([probe](probe-2026-09-codex-watch.md)). Copilot out of scope because a custom endpoint removes GitHub authentication |

## Install

The package runs on CPython 3.10 through 3.13. On a machine whose only Python
is 3.9, the plugin route needs no Python beyond 3.9 and the package route needs
`--python`: `uv tool install --python 3.12 --compile-bytecode shim`.

## The 0.2.0 names

The package was renamed from `shim_guard` to `shim_cli` in 0.3.0. From 0.3.0
through 0.3.2 a re-exporting compatibility package, a second console script,
the old settings variable and a second `shim-guard` marketplace entry kept an
install written by 0.2.0 working. 0.3.0 scheduled their removal for 0.5.0; 1.0
removed them, announced one release ahead in the 0.3.2 notes and in
`shim doctor`.

In 1.0 a client settings file that still carries `-m shim_guard.hook` makes the
client report `No module named shim_guard` on every prompt, and nothing is
inspected, until `shim install <client>` rewrites the line. `shim doctor
<client>` reports that shape, and a Copilot hook file under its 0.2.0 name, as
`FAIL` with that command, and counts no event from it as installed. Settings,
the ledger and the Copilot hook file still move on first contact.

A Claude Code plugin installed as `shim-guard@shim-guard` stopped updating at
0.3.2 and moves with:

```
/plugin uninstall shim-guard@shim-guard
/plugin marketplace add GetSHIM/shim-cli
/plugin install shim-cli@shim-cli
```

**Codex needed one migration in 0.3.0.** There the marketplace name in the manifest *is*
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
Codex before 0.3.0, so no working Codex plugin install was broken.

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

**The plugin ships two hook files, and Claude Code reads both.** `plugin.json`
declares `hooks/claude.json`, but Claude Code 2.1.263 also loads
`hooks/hooks.json` by convention — the file Codex finds the same way, because
`.codex-plugin/plugin.json` has no field that names a hooks path. Claude Code
does not expand Codex's `${PLUGIN_ROOT}`, so every prompt ran `/hooks/run-shim`
and logged exit 127 beside the real hook's output. The Codex command therefore
opens with `[ -z "${PLUGIN_ROOT}" ] && exit 0`: Claude Code leaves `PLUGIN_ROOT`
unset and the command stands down silently, while Codex sets it. 0.3.1 and
0.3.2 keyed the guard on `CLAUDE_PLUGIN_ROOT` instead, which Codex 0.151.0
sets too, so the Codex plugin hook exited before inspecting anything and
reported the prompt as `Completed`; the package route was unaffected. The
guard now fails the safe way: a Claude Code that set `PLUGIN_ROOT` would
inspect a prompt twice, not zero times. If a future Codex manifest accepts a
hooks path, the file becomes `hooks/codex.json` and the guard is dropped.

**A Codex hook does not run until it is trusted.** From 0.151.0 Codex holds a
persisted trust record per hook and silently skips any hook it does not have
one for: no warning, no line in the transcript, and prompts reach the model
uninspected. Writing the fragment is therefore only half of `shim install
codex` — review and trust it in Codex, which is why `shim doctor codex` ends
on `Codex hook activation is client UI state; verify SHIM with /hooks`. shim
cannot read that record and does not write it; a diagnosis that claimed to
would be guessing. `codex exec --dangerously-bypass-hook-trust` runs enabled
hooks without it, which is useful to confirm an install and wrong as a habit.

## 1.0.0 release evidence

Recorded PENDING_RELEASE_EVIDENCE (date, macOS version and architecture, CPython and uv versions).
Every row is pasted from the terminal of the day, on the 1.0.0 candidate.

| Evidence | Recorded result |
| --- | --- |
| Local gate | `python scripts/check.py` green on the candidate: PENDING_RELEASE_EVIDENCE (test count). |
| Tag-time re-verification | `release.yml` re-runs the same gate on the tagged tree, rebuilds from a clean snapshot and requires the fresh build to match the tested artifacts byte for byte. It refuses the tag while this record carries a pending marker. |
| Claude Code | tested: 2.1.263. PENDING_RELEASE_EVIDENCE: plugin from the marketplace with a `PATH` whose only `python3` is 3.9; the Turkish prompt (J1); a `Read` of a JSON file with a 2026 epoch, left intact; a `Read` of a `.env`, the model saying values were masked; `shim doctor claude` with no version `WARN`. |
| Claude Code `shim watch` | PENDING_RELEASE_EVIDENCE: a live session on a subscription sign-in (J6). |
| Codex CLI | tested: 0.151.0. PENDING_RELEASE_EVIDENCE: prompt hook trusted, `observe` and `enforce` (J7); `shim watch -- codex` refused with the sentence. |
| GitHub Copilot CLI | tested: 1.0.80. PENDING_RELEASE_EVIDENCE: `shim install copilot`, hook reviewed and enabled; a safe prompt printed nothing; a synthetic email reached the model redacted (its verbatim repeat quoted); a forced hook error failed open. |
| Package on a 3.9-first `PATH` | PENDING_RELEASE_EVIDENCE: `uv` with `--python` installed 1.0.0 (J2). |
| Upgrade from 0.2.0 | PENDING_RELEASE_EVIDENCE: a 0.2.0 machine upgraded to 1.0 sees `No module named shim_guard` on a prompt, `shim doctor` `FAIL` with the command, runs it, and is clean (J3). |
| When something is wrong | PENDING_RELEASE_EVIDENCE: the numbers table (J8). |
| Leaving | PENDING_RELEASE_EVIDENCE: revert, uninstall, and what is left on disk (J9). |
| Python floor | PENDING_RELEASE_EVIDENCE: `check.py` green in CI on 3.10 and 3.13; the committed archive answers identically on 3.9 and 3.13 (`archive-on-3-9`). |
| SBOM and attestation | PENDING_RELEASE_EVIDENCE: SBOM component count for the tag and the `gh attestation verify` command. |
| Supply-chain workflows | PENDING_RELEASE_EVIDENCE: the first green run of CodeQL, Scorecard, Dependabot and the prose check. |

## 0.3.0 release evidence

Recorded 8 September 2026 on macOS 26.4 arm64, CPython 3.13.5, uv 0.12.5.

| Evidence | Recorded result |
| --- | --- |
| Local gate | `python scripts/check.py` green: lock, `ruff check`, `ruff format --check`, `ty`, **1,856 tests**, wheel and source distribution built. |
| Tag-time re-verification | `release.yml` re-runs the same gate on the tagged tree, rebuilds from a clean snapshot and requires the fresh build to match the tested artifacts byte for byte. The tag is cut only from a commit whose `Verify` run is green. |
| Claude Code | tested: 2.1.263. Prompt and tool hooks exercised live; `shim watch` measured request and response, both directions reported apart, `stop_reason` read from the wire. The `Stop` last-block limitation is captured as a fixture. |
| Codex CLI | tested: 0.151.0. Prompt hook installed into a real `~/.codex` and exercised live: `observe` passed the prompt through, `enforce` blocked it before the model call. Hook trust is a client-side record shim cannot read; an untrusted hook runs silently not at all. |
| Codex `shim watch` | Refused, with the reason measured rather than assumed. See [the September 2026 probe](probe-2026-09-codex-watch.md). The transport works; the shipped implementation set an environment variable Codex ignores. |
| GitHub Copilot CLI | **Not run live for 0.3.0.** 1.0.83 is installed locally; tested: 1.0.80. Install and diagnosis paths are covered by the suite; no live client run was made for 0.3.0. |
| Context diet under the proxy | Seven scripted Claude Code sessions, 8 September 2026. The cache prefix held in every one, including a session whose configuration changed mid-run. A 22,199-byte tool result became 13,374 with the diet on and 21,690 with it off. [Study](study-2026-09-08-image-repeat-cache.md). |
| Python floor | 3.10 is exercised by CI only; no local 3.10 run was made. The bundled archive targets 3.9 and is rebuilt and compared by a contract test. |
| Supply-chain workflows | CodeQL, Scorecard, Dependabot and the prose check are configured and their pinning is asserted by `tests/contracts/test_workflows.py`. They run on pull requests into `main` and on pushes to `main`; a first green run of each is a condition of the release, not a claim of this document. |
| SBOM and attestations, from 0.3.2 | `shim-cli.sbom.cdx.json` is scanned from the tagged wheel installed with `requirements.lock`, and the release fails unless it lists `shim` at the tag's version and every unconditional pin in the lock: **11 library components** (`shim` and ten dependencies; `tomli` and `colorama` are conditional and absent on CPython 3.13), measured with syft 1.42.3 on the tagged tree. The wheel, sdist and `shim.pyz` are attested, the release job verifies all three before publishing, and the bundles are release assets. Against a downloaded wheel: `gh attestation verify shim-0.3.2-py3-none-any.whl --bundle shim-0.3.2.intoto.jsonl --repo GetSHIM/shim-cli` for provenance, and the same with `--bundle shim-0.3.2-py3-none-any.whl.sigstore.json --predicate-type https://cyclonedx.org/bom` for the SBOM. |

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
