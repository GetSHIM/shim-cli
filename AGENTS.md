# SHIM Guard agent guidance

Read `README.md`, `docs/architecture.md`, `docs/privacy.md`, and `docs/compatibility.md` before changing the detector, hook protocol, installer, or `shim watch`.

Apply the `ponytail:ponytail` and `code-writing-guidelines` skills to every implementation and review. Prefer the smallest complete change, standard-library boundaries, direct control flow, and no speculative abstractions.

## Safety boundaries

- The hook is synchronous, local, and network-free, with no daemon.
  `shim watch` is the one component that touches the network, and only as a
  forwarding proxy: it runs for the length of one command, binds to loopback
  only, forwards bytes unchanged, and originates no request of its own. Nothing
  under `src/shim_cli/watch/` may be imported from the hook path — the hook is
  a cold-start subprocess on every tool call and `tests/contracts` enforces it.
- Never log or persist raw prompts, findings, or replacement values. The only
  prompt-derived file is one `0600` typed redaction in OS temporary storage per
  supported block; remove it if the block response cannot be serialized. The
  session spool and the opt-in ledger hold entity *names and counts* and a
  scrubbed target path — never the value that produced them, and never a shell
  command. `shim watch` keeps sizes and counts only; no request or response body
  reaches disk.
- Safe hook input must produce exactly empty stdout and stderr.
- Handled prompt errors must block with a generic native response. A verified
  tool event that cannot be inspected must pass through unchanged and report
  the failure without denying already-created work.
- Entity settings default to all public types; malformed or unsafe settings fail
  closed, and no settings field may contain prompt-derived data.
- Never modify real user configuration in development or tests; use temporary paths.
- This repository is public and much of its documentation and corpus comes from
  running the tool for real. Nothing committed may carry the machine it was
  written on — a home directory, an editor path, a scratch directory, a
  provider request or organisation id. Test credentials must be *obviously*
  synthetic (`0123456789abcdef`, a vendor's published example key), never a
  plausible-looking random string: a fake that looks real trips secret scanners
  and costs a reviewer time. `tests/contracts/test_no_local_leakage.py` checks
  every tracked file, with the markers read from the environment so it protects
  whoever runs it.
- Install may create a missing client settings file or append SHIM's exact hook
  groups last while preserving unrelated settings and hooks; revert removes
  only those groups and retains the document even when empty.
- Keep install and revert idempotent, leave Codex inline `config.toml` hooks
  untouched, preview only SHIM's fragment, and require manual setup for malformed,
  ambiguous, unsafe, or concurrently changed files.
- Do not add clients, auth, telemetry, daemons, plugin frameworks, or
  compatibility shims without an approved requirement. `shim watch` is the only
  approved proxy and it is opt-in per command: nothing may route a client
  through it automatically, and no shell profile may be edited to do so.
- Detection may be relaxed only in the direction that drops noise, never in the
  direction that drops a credential. `parity-v1.json` is generated evidence and
  must not be regenerated to make a test pass; a deliberate difference from it
  goes in `DELIBERATE_DIVERGENCES` with its reason and its new expected result
  pinned as tightly as the old one.
- A cap that turns "a lot of sensitive data" into "no protection at all" is a
  bug, not a safeguard. Bound work by input size, which is already bounded, and
  keep masking whatever was found.

## Checks

Use CPython 3.13 and a compatible uv 0.12.x. CI also covers the supported
CPython 3.10 floor. Run the complete local check from the repository root:

```bash
python scripts/check.py
```
