# Cookbook

Recipes for getting more out of shim once it is installed. The README is the
tour and [`commands.md`](commands.md) is the map; this is the part where you
have a specific problem and want the two lines that solve it.

Everything here runs locally. Nothing in this document sends anything anywhere.

- [Before you install: what did my agent already send?](#before-you-install-what-did-my-agent-already-send)
- [Start here: make it visible](#start-here-make-it-visible)
- [Stop a secret before it leaves](#stop-a-secret-before-it-leaves)
- [Teach it your project's own secrets](#teach-it-your-projects-own-secrets)
- [Quieten a noisy tool](#quieten-a-noisy-tool)
- [Keep the last four digits](#keep-the-last-four-digits)
- [Keep a connection string's host](#keep-a-connection-strings-host)
- [Be strict about shell commands](#be-strict-about-shell-commands)
- [When a command fails](#when-a-command-fails)
- [See what a whole session sent](#see-what-a-whole-session-sent)
- [Use it in CI and in scripts](#use-it-in-ci-and-in-scripts)
- [Know what your client can actually do](#know-what-your-client-can-actually-do)
- [When something looks wrong](#when-something-looks-wrong)
- [Leaving](#leaving)

## Before you install: what did my agent already send?

shim only sees what happens after you install it. Your Claude Code history
holds what happened before:

```console
shim audit
shim audit --since 2026-09-01 --project ~/work/kasa-mutabakat
```

Each line under "reached the model" names a kind of value, how many there
were, in how many sessions, and how they got there: `your prompt`, a tool such
as `Read`, `failed Bash` for a command that failed, or `@.env` for a file you
attached. The last of these is the one people expect least, and it is why
[Stop a secret before it leaves](#stop-a-secret-before-it-leaves) says to ask
the agent to read a file rather than attach it.

If you would rather those sessions were not kept on this computer:

```console
shim audit --purge
```

It asks you to type `delete N` before it deletes anything, and it cannot take
back what the model provider already received. It deletes the transcripts and
their lines in `history.jsonl`; Claude Code's backups of edited files in
`file-history/` stay.

## Start here: make it visible

The first thing worth doing is watching shim catch something, on purpose, with
a value that is not yours:

```console
shim demo claude
```

Then use your agent normally for an hour and ask what it saw:

```console
shim report
```

The session report is the habit to build. It names what was found, where, and
what happened to it — masked, blocked, or only reported — and it ends with the
overhead shim added. If the report is empty, nothing sensitive crossed the line
that hour, which is also worth knowing.

Records live for the session only. To keep them for 30 days:

```console
shim config --ledger --yes
shim ledger show
```

The ledger holds entity *names and counts* and a scrubbed path. It never holds
the value that produced them, so it is safe to keep and useless to steal.

## Stop a secret before it leaves

shim ships deliberately asymmetric defaults. Your prompt is reported and sent,
because rewriting what you typed under you is worse than telling you. Tool
results are masked, because that is content you never read.

To turn your own prompts into a hard stop instead, edit the settings file —
the flags cover entity types, not per-direction rules. `shim config` prints its
path. Add:

```toml
[mode]
user-prompt = "enforce"
```

Now a prompt with a finding is withheld, and shim writes the redacted version
to a `0600` file in your temporary directory and tells you the path. Paste the
line it gives you and carry on — your prompt reaches the model with
`<SECRET_1>` where the key was.

A file you attach with `@` goes with the prompt as it is: Claude Code inlines it
after the prompt hook has run, so nothing can mask it. shim reads it first and
tells you what it holds before it is sent, and under `enforce` it stops the
prompt. To have the file masked instead, ask the agent to read it ("read .env
and explain the variables"): a tool result is masked before the model sees it.

The three modes are `observe` (count it, say nothing), `warn` (say it, change
nothing) and `enforce` (mask or refuse). They apply per direction, per event or
per tool, most specific first.

## Teach it your project's own secrets

The detector knows emails, cards, IBANs, keys and the rest. It does not know
your internal hostnames or your ticket codenames. Tell it:

```console
shim config --custom 'PROJECT_CODENAME=\bATLAS-[0-9]{4}\b' --yes
shim config --custom-literal 'INTERNAL_HOST=db-core-01' --yes
```

Use `--custom-literal` whenever the value contains regex characters — a dot in
a hostname matches any character otherwise, and a pattern that is wider than
you meant is a pattern that masks half your file.

Check it before you trust it:

```console
printf 'deploy ATLAS-1874 to db-core-01' | shim redact
```

A pattern that backtracks badly is refused when you add it, not when it hangs
your hook.

## Quieten a noisy tool

If one tool produces findings you do not care about, narrow that tool instead
of turning a whole type off everywhere:

```toml
[entities]
Read = ["SECRET", "DB_URI", "CREDIT_CARD"]
```

Now a `Read` only reports those three, while every other tool still checks
everything. This is almost always better than `shim config --disable PHONE`,
which switches phone numbers off for your prompts too.

Bare numbers are the usual complaint — an order id that looks like a phone
number. shim counts those separately and says so in the report rather than
masking them; if a specific tool is still noisy, narrow it here.

## Keep the last four digits

Support work often needs to know *which* card, without knowing the card:

```console
shim config --reveal IBAN=4 --yes
```

`<IBAN_1:1326>` instead of `<IBAN_1>`. Only `IBAN`, `CREDIT_CARD` and `PHONE`
can do this, at most four digits. `SECRET` cannot, and that refusal is
deliberate: a partial key is worth guessing at.

## Keep a connection string's host

A connection string keeps its scheme, host, port and database and loses its
user and password, so the agent can still tell production from staging:

```console
$ printf 'postgresql://app:synthetic-password@db-prod.internal:5432/kasa' | shim redact
postgresql://<DB_URI_1>@db-prod.internal:5432/kasa
```

A connection string with no user and password in it is not a finding at all. If
your host names are sensitive in themselves, teach shim their shape:

```console
$ shim config --custom 'INTERNAL_HOST=\bdb-[a-z-]+\.internal\b' --yes
$ printf 'postgresql://app:synthetic-password@db-prod.internal:5432/kasa' | shim redact
postgresql://<DB_URI_1>@<CUSTOM_1>:5432/kasa
```

## Be strict about shell commands

A command is not a document: text going into a shell can act. Tighten that one
direction without touching the rest:

```toml
[mode]
Bash = "enforce"
```

For a command or a local write, `enforce` means the call is refused rather than
rewritten, because silently editing a command the model is about to run would
change what it does. shim tells you what it found and stops there.

## When a command fails

The output of a command that failed reaches the model as it is: Claude Code
gives it to the hook afterwards and does not let it be replaced. When
`cat .env && cat missing-file` exits 1, the model has read `.env`. shim says
what was in it, tells the model not to repeat it, and counts it on its own line:

```text
  unmasked  5 SECRET  (failed Bash)
```

That needs the hook's `PostToolUseFailure` entry. The plugin has it from 1.0.3;
a hook installed with `shim install claude` before 1.0.3 does not, so run the
install once more and let doctor confirm it:

```console
shim install claude
shim doctor claude
```

`Coverage: 6 of 6 events installed` means it is there. To have a file masked,
ask the agent to read it ("read .env"): a tool result that succeeds is masked
before the model sees it.

## See what a whole session sent

The hook sees events. To see the whole conversation — bytes, tokens, and what
it would have cost — run the client through the measuring proxy:

```console
shim watch -- claude -p "Read calc.py and explain it in one sentence."
```

It binds to loopback, forwards bytes unchanged, invents no request of its own,
and writes no request or response body to disk. It reports both directions
separately and reads usage off the wire rather than guessing.

**Claude Code only.** Codex is refused, with the reason measured rather than
assumed: it takes its endpoint from its own configuration, so the proxy would
be bypassed and the session reported as empty. Copilot is out of scope, because
a custom endpoint there removes GitHub authentication altogether.

## Use it in CI and in scripts

`scan` exits `1` when it found something, so a pipeline step fails on a finding:

```console
shim scan < notes.md && echo "clean"
```

```yaml
- name: No secrets in the changelog
  run: git diff --name-only origin/main | xargs cat | shim scan
```

`redact` exits `0` and writes the rewritten text, so it composes; when stdin
cannot be read or scanned in full it writes nothing and exits `1`:

```console
kubectl logs api-7f4 | shim redact | pbcopy
```

That one is worth keeping in your shell history. Pasting logs into a chat is
how most values escape, and this makes the safe version the easy version.

Add `--json` to most commands when a script is reading the output. The text
layout is not a stable interface; the JSON is.

## Know what your client can actually do

shim can only do what the client grants its hooks, and the clients differ more
than their documentation suggests. `shim doctor <client>` prints exactly which
events are installed and what shim can change at each one.

| Client | Your prompt | Tool input and results |
| --- | --- | --- |
| Claude Code | Reported; withheld under `enforce` | Masked before the model reads them |
| Codex CLI | Reported; blocked under `enforce` | Not installed |
| GitHub Copilot CLI | Replaced with the redacted text | Not installed |
| VS Code | Reported; stopped before sending under `enforce` | A call is reported and denied under `enforce`; a **result is only reported** |

**VS Code deserves a sentence of its own.** Nothing is masked there, and after
a tool has run nothing can be withheld either: by then the model has the
result, and a hook that answers `block` is read straight through. That is
measured, not assumed. So in VS Code the protection that matters is the one
that happens *before* the data moves — the prompt that is stopped, the call
that is denied. A `read_file` result never reaches the hook at all, so a file
read is covered by its path, before the read. Terminal output is inspected in
full.

Because refusing is the only enforcement available there, tool events report by
default. Ask for the refusal when you want it:

```toml
[mode]
outbound = "enforce"
```

## When something looks wrong

```console
shim doctor claude
```

Doctor is the first command to run and usually the last one you need. It checks
the hook line, the events installed, the launcher in use, and the version the
archive and the package disagree about, if they do. The common answers:

**Nothing happens at all.** In Codex, a hook does not run until you trust it:
open `/hooks`, review the shim entry, enable it. shim cannot read that record
and does not pretend to.

**Everything is inspected twice.** Both the marketplace plugin and `shim
install` are registered for the same client. Pick one; doctor names the command
that removes the other.

**`No module named shim_guard`.** A hook written by 0.2.0. `shim install
<client>` rewrites the line.

**Your settings file is refused.** shim will not read settings that anything
else can rewrite — a symlink, another user's file, a group-writable location.
Anything that can edit your settings can turn detection off. `shim config
--reset --yes` starts over.

**A prompt was withheld and you do not know why.** The reason names the file
holding the redacted copy. Read it; it shows what was found and where.

## Leaving

```console
shim revert claude
shim ledger purge
```

Revert removes only shim's own hook groups and keeps everything else in the
file byte for byte, including hooks you added. What stays behind is your
settings file and, if you turned it on, the ledger — both listed above so you
can delete them yourself.
