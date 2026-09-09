<p align="center">
  <a href="https://getshim.tech">
    <img src="https://raw.githubusercontent.com/GetSHIM/shim-cli/main/docs/assets/shim-logo.svg" alt="shim" width="280">
  </a>
</p>

<h1 align="center">shim-cli</h1>

<p align="center">
  <strong>Local traffic visibility and privacy controls for coding agents.</strong><br>
  <a href="https://getshim.tech">getshim.tech</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/shim/"><img src="https://img.shields.io/pypi/v/shim.svg?logo=pypi&amp;label=PyPI" alt="PyPI version"></a>
  <a href="https://github.com/GetSHIM/shim-cli/actions/workflows/ci.yml"><img src="https://github.com/GetSHIM/shim-cli/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/GetSHIM/shim-cli/actions/workflows/codeql.yml"><img src="https://github.com/GetSHIM/shim-cli/actions/workflows/codeql.yml/badge.svg" alt="CodeQL"></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/GetSHIM/shim-cli"><img src="https://api.scorecard.dev/projects/github.com/GetSHIM/shim-cli/badge" alt="OpenSSF Scorecard"></a>
  <a href="https://pypi.org/project/shim/"><img src="https://img.shields.io/pypi/pyversions/shim.svg?logo=python&amp;logoColor=white" alt="Python versions"></a>
  <a href="https://github.com/GetSHIM/shim-cli/blob/main/LICENSE"><img src="https://img.shields.io/github/license/GetSHIM/shim-cli.svg" alt="License"></a>
  <a href="https://github.com/GetSHIM/shim-cli/stargazers"><img src="https://img.shields.io/github/stars/GetSHIM/shim-cli.svg?style=flat&amp;logo=github" alt="GitHub stars"></a>
</p>

shim-cli shows you what your coding agent actually sends to the model — how
many tokens went where, what the turn cost, and which secrets and personal data
were in it — and masks what it can before the model sees it. The hook and
detector add no network destination, account, API key, or telemetry. The opt-in
`shim watch` proxy forwards only to the provider the client already uses.

<p align="center">
  <img src="https://raw.githubusercontent.com/GetSHIM/shim-cli/main/docs/assets/shots/masked-tool-result.png" width="880"
       alt="A terminal: the agent runs Read on a .env file holding an AWS key, an IBAN and an email; the model is handed AWS_ACCESS_KEY_ID=&lt;SECRET_1&gt;, BILLING_IBAN=&lt;IBAN_1&gt; and &lt;EMAIL_1&gt; instead.">
</p>

Two commands, two different questions:

| Command | Answers |
| --- | --- |
| `shim watch -- claude` | What did this session actually send, and what did it cost? |
| `shim install claude` | Mask secrets and personal data in eligible tool results, every session, automatically. |

> [!WARNING]
> shim-cli is alpha software and a best-effort guard, not a data-loss
> prevention boundary. Read the [privacy limitations](https://github.com/GetSHIM/shim-cli/blob/main/docs/privacy.md) before
> using it with sensitive data.

## Measure a session

Nobody can tell you where their agent's context window actually goes. `shim
watch` puts a local proxy in front of the client for one command, forwards
every byte unchanged, and reports what went past:

```console
shim watch -- claude
shim watch -- claude -p "explain this repo"
```

<p align="center">
  <img src="https://raw.githubusercontent.com/GetSHIM/shim-cli/main/docs/assets/shots/shim-watch.png" width="880"
       alt="shim watch — 16s, 2 requests. Input 257,661 tokens, 46% cache read; output 281. Where the input went: tools ~226,781 (88%), system 3%, messages 9%. Request 3 IBAN and 2 EMAIL in messages; response 3 IBAN in model text. Spend ~$2.82. Nothing was modified, and no request body was written to disk.">
</p>

On the session above, **the tools array was 88% of the input tokens** — before
a single line of the user's own code. That is one session on one repository,
not a universal figure, which is the point: it is your number and you have no
other way to get it.

The three `IBAN`s came from a file the agent read with a tool: shim scans every
text field the model reads, including tool results, and says which part of the
request each finding was in.

If you sign in with a subscription, the `spend` line is what the same traffic
would cost on an API key, not a bill. shim reads the tokens the provider
reports and prices them; it cannot see what your plan charges.

The `response` line is the other direction — what the model wrote back, with
its `thinking` counted apart from its answer. It is a recall measurement, not a
leak report: on your own key there is no other tenant to leak from, and a coding
agent invents plausible values all day. `compare` puts the two sides next to
each other: three account numbers went in through a tool result and the same
three came back, which is the round trip made visible. A response that stopped
at the provider's output limit adds a `cut off` line.

Token counts come from the provider's own `usage` block and are exact. How
they divide between sections has no ground truth on the wire, so it is
inferred from byte share, marked `~`, and always sums to the exact total.
Exact and inferred figures never share a column.

It also covers what hooks structurally cannot see: files pulled in with `@` are
inlined by the client while it builds the prompt, so no hook fires for them,
and the system prompt, the tools array and the token counts are never handed to
a hook at all.

**It forwards and measures. It does not modify.** Not one byte of a request is
changed, no request body is ever written to disk, and nothing is transmitted
anywhere except to the provider the client was already talking to. The proxy
binds to loopback, exists for the length of the command, and nothing is left
behind — no shell profile is edited and no setting is changed.

Overhead measured against a live session: about 6 ms of proxy plumbing plus a
23 ms TLS handshake per request. Scanning the body costs more than that, but it
runs after the response has been relayed. Request inspection retains at most
8 MB per request, with two concurrent inspection slots and no pending queue.
Larger requests still pass through in full; skipped or unfinished measurements
are reported as incomplete. Usage can be known, partial, or unavailable.

`shim watch` refuses a non-empty `ANTHROPIC_BASE_URL` before startup. Custom
upstreams are not supported. Requests need an unambiguous non-negative
`Content-Length`; transfer coding (including chunked requests) is rejected.
Request-body reads have a 30-second deadline.

**`shim watch` supports Claude Code only.** Codex is refused, because it takes
its endpoint from its own configuration rather than the environment: a proxy
would be started and the whole session would run past it, reported as empty.
That was measured, not assumed — [the September 2026
probe](https://github.com/GetSHIM/shim-cli/blob/main/docs/probe-2026-09-codex-watch.md)
also shows the transport itself works, so this is a limitation with a fix
rather than a dead end. The Codex prompt hook is unaffected. Copilot is out of
scope: it accepts a custom endpoint only through bring-your-own-key, which
removes GitHub authentication altogether, so there is nothing to watch.

## Supported clients

| Client | Your typed prompt | Tool input and results |
| --- | --- | --- |
| [Claude Code](https://github.com/anthropics/claude-code) | Reports what it found and lets it through; blocks under `enforce` | Eligible structured arguments and inbound results are masked; commands and local writes are report-or-deny only |
| [Codex CLI](https://github.com/openai/codex) | Reports what it found and lets it through; blocks under `enforce` | Not installed — no verified native tool-event adapter |
| [GitHub Copilot CLI](https://github.com/github/copilot-cli) | Replaces the model-facing prompt with the redacted text | Not installed — no verified native tool-event adapter |

Tool coverage is verified against a running client, not derived from
documentation. `shim doctor <client>` prints exactly which events are installed
and what shim can and cannot change at each one.

shim-cli detects email addresses, phone numbers, credit cards, IBANs, IP and
MAC addresses, US SSNs, Turkish national and tax IDs, secrets, and database
URIs. Checksums are verified where they exist, so a mistyped IBAN or national
ID is not reported.

It deliberately stays quiet on values that name nobody: loopback and
unspecified addresses (`127.0.0.1`, `0.0.0.0`, `::1`) and connection strings to
them that carry no credentials (`redis://localhost:6379/0`). Private ranges,
real hosts, and anything with a `user:password@` are still detected. Once
`0.0.0.0` and `127.0.0.1` both read as `<IP_ADDRESS_1>`, the model can no
longer tell "listen on every interface" from "loopback only" — detection that
fires where there is nothing to find is how people learn to ignore it.

Detection runs locally without an account, API key, network request, daemon,
telemetry, or prompt history.

## Install

The `shim` package supports CPython 3.10 through 3.13 on macOS and Linux. The
plugin's bundled hook also runs on 3.9, which is what a stock macOS provides.
Choose one package manager:

```console
uv tool install --compile-bytecode shim
# or
pipx install shim
```

Preview and install the hook for your client:

```console
shim install codex --dry-run
shim install codex
shim doctor codex
```

Replace `codex` with `claude` or `copilot` as needed. Run `shim help` for all
commands.

One more step in Codex: **a hook does not run until you trust it.** Codex keeps
a trust record per hook and skips any hook without one — no warning, and your
prompts reach the model uninspected. Open `/hooks` in Codex, review the shim
entry, and enable it. `shim doctor codex` ends by reminding you, because that
record lives in Codex and shim cannot read it.

### Marketplace plugins

Codex and Claude Code users can install the repository's marketplace plugin:

```text
/plugin marketplace add GetSHIM/shim-cli
/plugin install shim-cli@shim-cli
```

```console
codex plugin marketplace add GetSHIM/shim-cli
codex plugin add shim-cli@shim-cli
```

The marketplace plugin and `shim install` are alternative hook-registration
methods. Do not use both for the same client; `shim doctor` fails when it finds
two. Release-tag plugins bundle the hook archive, which needs only Python 3.9 or
newer and nothing else installed, on both clients. A development checkout may not
contain the release archive.

### Upgrading from 0.2.0

Nothing breaks and nothing is required of you. The hook command your client
already runs keeps working, byte for byte, through a compatibility package.

When convenient, run `shim install <client>` once. That rewrites the hook line
to the new module name and, on Copilot, replaces the old hook file. Your
settings and ledger move to `shim/` on the next `shim` command that touches
them, and each move is reported once. The Claude Code plugin keeps loading and
updating: `shim-guard@shim-guard` still resolves through a marketplace alias,
which is removed in 0.5.0 along with the `shim-guard-hook` script, the
`shim_guard` package and the `SHIM_GUARD_CONFIG` variable.

Codex plugin users are the one exception and need four commands; see
[docs/compatibility.md](docs/compatibility.md).

## Use

Once the hook is installed, use your client normally. To inspect text directly,
pipe it through `scan` or `redact`:

```console
printf '%s' 'Contact me at alice@example.com' | shim scan
printf '%s' 'Contact me at alice@example.com' | shim redact
```

Both commands read standard input. Do not pass real prompts as command-line
arguments, where they may be recorded in shell history or process listings.

> [!IMPORTANT]
> **With the default configuration, shim-cli does not prevent a secret you
> type into a prompt from reaching the model. It tells you afterwards.**
> Codex and Claude Code offer no field for rewriting a submitted prompt, so the
> only way to stop one is to refuse the sentence you just typed — which is
> disruptive and rare enough that it is not the default. Set
> `user-prompt = "enforce"` in `[mode]` to block instead. Copilot's
> `userPromptTransformed` event does support a model-facing replacement. Claude
> tool results are masked at the verified installed events.

Under `enforce`, the prompt is withheld and you are handed a redacted copy to
resend:

<p align="center">
  <img src="https://raw.githubusercontent.com/GetSHIM/shim-cli/main/docs/assets/shots/blocked-prompt.png" width="880"
       alt="shim's verbatim answer to the client: decision block, reason &quot;shim blocked this prompt: EMAIL (1)&quot;, and a path to a redacted copy of the prompt to send instead, with suppressOriginalPrompt true.">
</p>

## See what it did

shim says nothing when it works, so it keeps a short record of its own
decisions. Claude's verified `Stop` hook shows the total at the end of a turn
where something changed:

```text
shim — this session
  masked    60 EMAIL  (Read customers.csv)
            60 IBAN  (Read customers.csv)
            60 PHONE  (Read customers.csv)
            60 TR_NATIONAL_ID  (Read customers.csv)
  flagged   1 INSTRUCTION_OVERRIDE  (Read runbook.md)
            1 HIDDEN_TEXT  (Read runbook.md)
  overhead  62 ms median, 119 ms p95
```

That is a real session against Claude Code, not an illustration: a 5.5 KB
customer file, every value in it replaced before the model saw it, and a
document in the repository caught trying to give the agent instructions. Run
under `shim watch` at the same time, the proxy — which reads the actual wire,
independently of the hook — reported two email addresses in the whole session,
both from the client's own system messages. None of the sixty reached the
model.

Two more lines appear when they have something to say. A session with your own
patterns names which one matched, and a scanned model reply is counted apart
from everything else, because the model wrote it:

```text
  masked    3 CUSTOM  (Read config/settings.py)
  custom    2 PROJECT_CODENAME, 1 INTERNAL_HOST
  model     1 EMAIL in its replies (model-generated content, not leaks)
```

`shim report` prints the same summary on demand, and `--json` makes it
scriptable. It reads the newest temporary spool first; if none remains, it
falls back to the retained ledger, if you turned that on.

## Shrink tool results

At Claude's verified `PostToolUse` event, shim compacts eligible tool results
before the model reads them, so the context window fills more slowly. Nothing
is ever truncated or summarised, and a result that cannot be shrunk safely is
left unchanged by the diet. Two transforms ship; only lossless JSON compaction
is on by default:

| Transform | What it does |
| --- | --- |
| `json` | Removes the whitespace *between* JSON tokens. Every number literal, duplicate key and string survives byte-for-byte, because this is a lexer rather than a parse and re-serialise. Lossless. |
| `whitespace` | Strips trailing spaces and tabs from the end of each line. Line count is never changed. Not byte-for-byte: a Markdown hard line break is two trailing spaces, and it does not survive this. |

It applies to tool *results* only — never to a tool's arguments, never to
anything written to your disk, and never under `mode = "observe"`.

**The diet also never touches a result that shows you a file.** `Edit` matches its
`old_string` against what is on disk, not against what the model was shown, so
compacting a pretty-printed file on the way in makes the next edit of that file
miss. Reads, notebooks and edit results are unchanged by the diet; sensitive
values can still be masked according to policy. Fetched pages, command output
and tool results are where the saving comes from.

Turn it off with `shim config --no-diet`, or name individual transforms in the
config file. Trailing-whitespace removal is opt in because it can remove a
Markdown hard line break:

```toml
diet = ["json", "whitespace"]   # or false to disable entirely
```

While reading results shim also flags text that is trying to give the model
orders — "ignore all previous instructions", impersonated system messages,
invisible characters. These are **reported and never acted on**: rewriting a
tool result because it reads as imperative would corrupt legitimate content.
They appear in the session summary as `flagged`, naming the file they came
from — which is the only part you can act on:

```
  flagged   1 INSTRUCTION_OVERRIDE  (Read release-notes.md)
            1 HIDDEN_TEXT  (Read release-notes.md)
```

The record holds entity names, counts and the file or URL involved — never the
value that was found, and never a shell command. It lives in a private OS
temporary file. Claude's installed `SessionEnd` hook deletes that session's
file; clients without a verified lifecycle hook leave it to operating-system
temporary cleanup. `shim config --ledger` opts in to monthly retained copies.
A month becomes eligible for pruning 30 days after its end and is removed on a
later ledger write; `shim ledger purge` deletes all ledger files immediately.
Nothing is ever transmitted. See
[Privacy](https://github.com/GetSHIM/shim-cli/blob/main/docs/privacy.md#what-is-recorded).

## Configure detection

All supported entity types are enabled by default. View or change the local
policy with:

```console
shim config
shim config --only EMAIL --only SECRET
shim config --disable IP_ADDRESS --disable MAC_ADDRESS
shim config --reset
shim config --ledger        # keep the session record past the session
shim config --no-diet       # stop shrinking tool results
```

`shim config` with no arguments prints the entity table plus the current
ledger and diet state, so what shim keeps and what it rewrites is answerable
without opening the file.

Detection can also be narrowed for one tool at a time, which the CLI has no
flag for — scan commands for secrets without scanning every file read for
phone numbers:

```toml
[entities]
Bash = ["SECRET", "DB_URI"]
Read = ["SECRET"]
```

Every key in the file is optional. A file holding only `[mode]` or only
`[entities]` is valid and everything else keeps its shipped default.

Changes are previewed before they are saved. The CLI, installed hook, `scan`,
and `redact` all use the same policy.

### Your own patterns

The eleven built-in types do not know your project's code name, your internal
host format, or your customer id shape. Name a pattern and shim masks it like
any other type, as `CUSTOM`:

```console
shim config --custom PROJECT_CODENAME='\bATLAS-[0-9]{4}\b'
shim config --custom-literal INTERNAL_HOST='build.corp.internal'
shim config --remove-custom PROJECT_CODENAME
```

```toml
[[custom]]
name = "PROJECT_CODENAME"
pattern = '\bATLAS-[0-9]{4}\b'

[[custom]]
name = "INTERNAL_HOST"
literal = "build.corp.internal"
ignore_case = true
```

A match is replaced by `<CUSTOM_1>` — the name stays out of the model's
context — and the session summary says which pattern matched:

```text
  masked    3 CUSTOM  (Read config/settings.py)
  custom    2 PROJECT_CODENAME, 1 INTERNAL_HOST
```

A literal matches whole words unless you set `whole_word = false`; a pattern is
a Python regular expression and writes its own boundaries. At most 32 patterns,
and a built-in type wins wherever the two overlap, so a custom pattern can add
detection but never take an email away from `EMAIL`.

**The pattern is checked when you write it, not when it runs.** The hook is
synchronous and Python's `re` has no per-match timeout, so a pattern that
backtracks would overrun the client's own timeout on a large tool result.
`shim config` refuses one before writing it, `shim doctor` re-checks the file,
and 32 patterns cost no measurable time on the hook path:

```console
$ shim config --custom BAD='(a+)+$'
FAIL pattern BAD backtracks on repeated input; simplify it
```

### Keep the last few digits

Every finding is replaced whole, so three masked accounts on three lines read
the same and neither you nor the model can tell which is which. Opt in per
entity and shim keeps the trailing digits banks and card issuers already print
for exactly that purpose:

```console
shim config --reveal IBAN=4
shim config --no-reveal IBAN
```

```diff
- move <IBAN_1> to <IBAN_2>
+ move <IBAN_1:1326> to <IBAN_2:6819>
```

Only `IBAN`, `CREDIT_CARD` and `PHONE` may reveal a tail, one to four digits;
anything else is refused. Separators are skipped, so a value printed as
`TR33 0006 1005 1978 6457 8413 26` still reveals `1326`. It is off by default
and changes nothing else: the same spans are found and the same counts are
reported.

## Privacy limitations

- The host client receives the raw prompt before its hook runs, and other hooks
  may receive it concurrently.
- Detection is best-effort and may miss sensitive values.
- A disabled, untrusted, crashed, or timed-out hook may fail open according to
  client behavior.
- Clients, providers, and other tools may retain data independently of shim.
- Redacted temporary files may still contain missed sensitive content. Review
  them before resubmission and delete them when finished.

See [Privacy](https://github.com/GetSHIM/shim-cli/blob/main/docs/privacy.md) for the full trust boundary and
[Compatibility](https://github.com/GetSHIM/shim-cli/blob/main/docs/compatibility.md) for tested versions and evidence.

## How this is verified

Every figure here was measured on the released build, not estimated.

| | |
| --- | --- |
| Tests | **1,800+**, one command: `python scripts/check.py` — lock, lint, format, types, suite, wheel, sdist |
| Hook cost | **67 ms** median end to end, interpreter start included; **41 ms** for a session summary |
| With 32 custom patterns | **+0.8 ms** median against the same prompt with none |
| Detector corpus | **570 cases**, graded on exact redacted output rather than category presence |

<p align="center">
  <img src="https://raw.githubusercontent.com/GetSHIM/shim-cli/main/docs/assets/shots/shim-doctor.png" width="880"
       alt="shim doctor claude: twelve checks, each PASS or WARN — the hook group is present, no 0.2.0 names are left, 12 of 12 entities are enabled, the runner protected a sensitive fixture, and coverage is 5 of 5 events.">
</p>

Hook output is asserted byte for byte, not by shape: a safe event must produce
exactly zero bytes on stdout and stderr. 312 contract tests hold that, plus the
import boundaries, the rule that no committed file carries the machine it was
written on, and a byte-identical rebuild of the shipped plugin archive.

The detector scores 1.0 precision and recall on that corpus, and the metrics
file states in the same breath why that is a weaker claim than it looks:
*synthetic fixture-bound evidence only; not a real-world statistical claim.* A
perfect score on a corpus you wrote is a regression guard, not a measurement of
the world.

Three things were learned by running the tool against a live client rather than
reasoning about it, and each one changed the code:

- One working request carried **4,402 text fields across 35 levels of nesting**,
  the depth coming from an MCP tool's recursive JSON schema. The hook's own
  traversal stops at 24, so `shim watch` carries its own budget.
- The **tools array was 88% of the input tokens** on a real session, before a
  single line of the user's own code.
- Claude Code's `Stop` event hands the hook **only the turn's last text block**,
  so anything the model said before a tool call in the same turn is not counted
  there. `shim watch` sees all of it.

Tested client versions, captured fixtures and the full evidence table are in
[Compatibility](https://github.com/GetSHIM/shim-cli/blob/main/docs/compatibility.md).

## Uninstall

In this order, because each step needs the one before it:

```console
shim revert claude          # once per client you installed
shim ledger purge           # only if you turned the ledger on (shim ledger show reads it)
uv tool uninstall shim      # or: /plugin uninstall shim-cli@shim-cli
rm -r ~/.config/shim        # your settings, if you want them gone too
```

`shim revert` removes only shim's own hook group and leaves every other hook in
the file untouched; for Copilot it also deletes the hook file, which is shim's
alone. `shim ledger purge` deletes the retained records — skip it and they age
out after 30 days on their own. Uninstalling the package leaves
`~/.config/shim/config.toml` in place, which is why the last line is separate:
reinstalling later finds your entity choices and custom patterns still there.

`shim watch` needs no uninstall: it edits nothing, so there is nothing to undo.

## Project documentation

- [Command reference](https://github.com/GetSHIM/shim-cli/blob/main/docs/commands.md) — every command, flag and exit code
- [Architecture](https://github.com/GetSHIM/shim-cli/blob/main/docs/architecture.md)
- [Compatibility](https://github.com/GetSHIM/shim-cli/blob/main/docs/compatibility.md)
- [Privacy](https://github.com/GetSHIM/shim-cli/blob/main/docs/privacy.md)
- [0.3.1 release notes](https://github.com/GetSHIM/shim-cli/blob/main/docs/releases/0.3.1.md)
- [0.3.0 release notes](https://github.com/GetSHIM/shim-cli/blob/main/docs/releases/0.3.0.md)
- [0.2.0 release notes](https://github.com/GetSHIM/shim-cli/blob/main/docs/releases/0.2.0.md)
- [Contributing](https://github.com/GetSHIM/shim-cli/blob/main/CONTRIBUTING.md)
- [Security policy](https://github.com/GetSHIM/shim-cli/blob/main/SECURITY.md)

## Development

Clone the `shim-cli` repository and run the complete local check from its root:

```console
git clone https://github.com/GetSHIM/shim-cli.git
cd shim-cli
python scripts/check.py
```

## License

Apache-2.0. See [LICENSE](https://github.com/GetSHIM/shim-cli/blob/main/LICENSE).
