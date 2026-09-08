# Study: images, repeat requests and cache awareness

**Recorded:** 8 September 2026 · **Client:** Claude Code with `claude-sonnet-5` ·
**Proxy:** `shim watch` 0.3.0 · **Harness:** `scripts/probe/study.py`,
`scripts/probe/images.py`, `scripts/probe/summarize_study.py`

Three ideas from the feature glossary were held together because each one, done
alone, can break another or a rule the product already keeps. This is the
evidence for each, and a decision. No product feature was built for it; the one
change to shipped code was to report per-request usage in `shim watch --json`,
because the question is which requests hit the cache, not how many tokens a
session spent.

## What was measured

One scripted task, ten tool calls in a single turn, so that the requests inside
a session share a growing prefix; separate `claude -p` runs would each start
from a cold cache and measure nothing. Two properties of the context diet decide
the shape of that task, and getting either wrong measures nothing at all:

- it skips file-viewing tools, so a task made of `Read` calls is byte-identical
  with the diet on and off;
- its only default transform is JSON compaction, so a tool result has to be
  JSON for there to be anything to remove.

So the workspace holds pretty-printed JSON files read through `Bash`, which the
diet is allowed to rewrite. Fourteen sessions were run for about $9.47 of
quota, seven of them discarded for the reason in the next section.

### The first seven sessions measured nothing, and why

The first run used 46,725-byte JSON files. Every session showed an intact cache
prefix with the diet on and with it off, which looked like a clean result and
was worthless: the ledger showed the diet had never run. With the setting on and
off, every tool result was byte-identical — 34,388 in, 33,609 out,
`transforms: []` both times.

Claude Code delivered about 34,000 bytes of each 46,725-byte file. `compact_json`
verifies its own work: it parses the original and the compacted form and keeps
the result only if the value is unchanged. A truncated document does not parse,
so the diet correctly declined to touch it. Complete, that file compacts to
28,952 bytes; truncated, not at all.

The run was repeated with 19,495-byte files, which arrive whole and compact to
12,082 bytes on their own. **A study of a transform has to prove the transform
ran.** The ledger pass exists for that and nothing else.

## Cache safety (R1)

**Verdict: cache-safe.**

Seven sessions, two per configuration plus one that changed the setting
mid-session and two more with the ledger on. In every one, once the cache
existed, each request read what the previous request had already paid for and
created only the tokens the new tool result added:

| Session | Requests | Cache reads never fall | Prefix preserved | Created after the first request |
| --- | --- | --- | --- | --- |
| diet-on-1 | 10 | yes | yes | 36,742 |
| diet-on-2 | 11 | yes | yes | 44,313 |
| diet-off-1 | 6 | yes | yes | 33,622 |
| diet-off-2 | 12 | yes | yes | 43,015 |
| flip-mid-session | 9 | yes | yes | 39,432 |
| ledger-on | 14 | yes | yes | 27,432 |
| ledger-off | 11 | yes | yes | 32,817 |

The diet did run, and the ledger says so directly. The same 22,199-byte tool
result, in the two configurations:

| Setting | In | Out | Saved | Transforms |
| --- | --- | --- | --- | --- |
| diet on | 22,199 | 13,374 | 39.8% | `['json']` |
| diet off | 22,199 | 21,690 | 2.3% | none |

The 2.3% with the diet off is masking, which runs either way.

**The mid-session change produced no spike.** The configuration was rewritten
25 seconds into the session, while the tool loop was running; the hook re-reads
it on every invocation, so later results were transformed differently from
earlier ones. The cache-creation series stayed flat afterwards, median 6,421
tokens, and the prefix held. This is the expected consequence of where the diet
runs rather than a surprise: a result is transformed once, as it enters the
transcript, and every later request replays what is already there. Changing the
setting changes only results that have not been produced yet, and those are
appended after the cached prefix rather than inside it.

**What this does not show.** The second half of the hypothesis — that the
creation totals differ only by the bytes the diet removed — is not resolvable
here. The spread within one configuration (33,622 and 43,015 with the diet off)
is larger than the difference between configurations, because the agent chose a
different number of tool calls each time: 6 to 14 requests for the same
instruction. Answering that would need the session count to grow until the
variance is averaged out, and it is not worth the quota, because the question
that mattered — does the transform break the prefix — is answered by the shape,
which was identical in all seven.

**One measurement artifact, named because it changes the numbers.** Claude Code
issues a final request that is *smaller* than the one before it — 87,000 bytes
shorter in one session, 133,000 in another — and reads a static prefix of about
117,000 tokens instead of the conversation's. It shares the system prompt and
tool definitions and nothing else. Counted as part of the turn, it reports a
broken prefix in a session where nothing broke, so the analysis cuts each run at
the first request that does not grow and reports how many it dropped.

## Image visibility (R2)

**Verdict: prompt attachment `proxy-visible`; `Read` of a PNG or a PDF
`hook-visible`.**

Three cases, one session each, with the recording hook installed and `shim watch`
in front:

| Case | Hook events | Image bytes at the hook | Requests |
| --- | --- | --- | --- |
| `@shot.png` in the prompt | `UserPromptSubmit`, `Stop`, `SessionEnd` | **0** of 11,823 | 1 |
| `Read` an 11,823-byte PNG | + `PreToolUse`, `PostToolUse`, `PostToolBatch` | **15,764** base64 in `tool_response` | 2 |
| `Read` a 599-byte PDF | + `PreToolUse`, `PostToolUse`, `PostToolBatch` | **800** base64 in `tool_response` | 2 |

An image attached to a prompt fires no tool event at all, and the
`UserPromptSubmit` payload is 620 bytes with no encoded content in it: the hook
never sees the image, and the model answers without a tool call. An image or PDF
read through `Read` arrives at `PostToolUse` as base64 under a `{"file", "type"}`
response, which is where a transform could reach it.

**Q18.2 is answered by the same measurement.** Downscaling is available on one
surface out of two, and it is lossy, so it needs the opt-in class and the
"did the model still answer" corpus that old PRD-07 R4 requires. Counting is
available on both, because the proxy sees every image whichever way it arrived,
and it changes nothing. The useful feature is the count.

## Repeat detection (R3)

**Verdict: drop, on the evidence below.**

The scripted task repeats three targets deliberately, so the *rate* measured
here is a property of the script and not of real sessions. What the run does
establish is which repeats a detector could see. In the ledger session, of
thirteen tool results:

- `Read` recorded a target and one repeat was visible: `dotenv-sample.txt`,
  seen twice.
- ten `Bash` results recorded `target: ""`, because the command is never kept —
  the probe corpus contains one carrying a live credential, and that rule is
  older than this study. Two of the three scripted repeats were `Bash` and a
  target-based counter is blind to both.

So a repeat detector built on what shim already stores would have found one of
three repeats in a task written to contain them. The one it found was a
300-byte file. R3's own rule is to drop the item if repeats cost under 5% of the
session's input tokens before the privacy question is reached, and one small
file against 27,432 created tokens is far below it.

Seeing the other two would mean keeping something about `Bash` results —
a digest of the command or of the result — which is the case R4 rules out.

## The privacy question (R4)

Nothing new has to be stored, so the answer for the counting version is **yes,
`docs/privacy.md` already covers it**.

A repeat is two tool results in one session naming the same target with the same
tool. Both halves are already in the record shim writes for every decision:
`tool_name`, and `target`, the scrubbed file path or URL — run through the
detector first, so a secret inside a path is masked there too. Counting how many
records in a session share a `(tool_name, target)` pair reads what is already on
disk and adds no field, no digest and no new retention. The spool it lives in is
deleted at `SessionEnd` on Claude Code, the one client that provides the event;
the opt-in ledger keeps the same records for a month and already says so.

A sentence would be needed for the version R3 shows would be necessary to make
the feature worth having: a detector keyed on the *content* of a result rather
than its target. That means a digest of the tool result, and a digest of
prompt-derived text is prompt-derived data. The rule in `AGENTS.md` forbids it,
and this study found no benefit that would justify reopening it.

## Decisions

| Item | Verdict | Reason |
| --- | --- | --- |
| Cache awareness | **drop the concern** | The diet does not break the prefix, including when the setting changes mid-session. There is nothing to build; the worry is closed. |
| Image downscaling | **shelve** | Reachable only for `Read`, lossy, and needs its own opt-in class and correctness corpus. Reopen if a measured session shows images are a material share of input tokens. |
| Image cost reporting | **build** | Proxy-visible on both surfaces, no transform, no new storage. Becomes PRD-19. |
| Repeat detection | **drop** | Sees one repeat in three in a task built to contain them, because commands are deliberately not stored. Making it useful requires content digests, which the privacy rule forbids. |

## Follow-up

PRD-19 covers image cost reporting only: `shim watch` already measures the
`messages` section, and an image is bytes in it. The work is attribution and a
line in the report, not a transform.

The captured numbers are in `tests/fixtures/probe/study-2026-09-08.json` and
`tests/probe/test_study_fixtures.py` asserts them, recomputing the compaction
figures from the shipped diet rather than trusting the copy above.
