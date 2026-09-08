# Architecture migration record for 0.2.0

- Status: implementation record
- Released baseline: `v0.1.3`
- Normative architecture: [architecture.md](architecture.md)

This file records why the 0.2.0 ownership changes were made. It is not a second
architecture specification or an active checklist.

## Decision

Keep shim-cli as one domain-sliced modular monolith and one Python
distribution. The product has no demonstrated need for services, Polylith,
dependency injection, a provider-neutral plugin system, or an
architecture-specific library.

The refactor stopped after each policy, native protocol, persisted record, and
external effect had one discoverable owner and the valuable dependency rules
were executable. Ordinary Python modules, frozen data values, direct functions,
and explicit composition remain sufficient.

## Why the branch changed

The branch added tool-event policy, context diet, session persistence, and an
opt-in forwarding proxy. Its import graph was already acyclic and its tests
already mirrored packages well, but several responsibilities had converged on
the process runner and shared event modules:

- detector modules imported user configuration for the entity vocabulary;
- policy modes and actions had two owners;
- the session summary depended on an event-owned persisted schema;
- guarded settings-file behavior was hidden below a feature-named
  `installation/` package even though configuration used it too;
- client-native tool responses lived in a cross-client adapter bucket that also
  contained unverified future scaffolding;
- `hook.py` owned session storage details and native tool response documents;
- stored targets could bypass the full detector when a tool narrowed its entity
  policy, and untrusted display labels were not normalized at ingress;
- development had many correct commands but no single verification entry point;
- published network, dependency, and client-coverage claims had drifted from
  executable behavior.

## Migration

| Before | 0.2.0 owner | Reason |
| --- | --- | --- |
| Entity vocabulary in `config.py` | `guard/entities.py` | Detection owns its public vocabulary and no longer depends upward on configuration. |
| Policy split between config and events | top-level `policy.py` | Directions, modes, defaults, decisions, and actions have one owner. |
| Persisted schema in events | `session/record.py` and `session.remember()` | The state owner validates records and contains best-effort spool and ledger effects. |
| `installation/` | `settings_files/` | The shared guarded filesystem boundary is named for what it owns. |
| Cross-client tool registry | `clients/claude/tool_events.py` | Only live-verified Claude tool events ship; Codex and Copilot remain prompt-only. |
| Native tool JSON and storage details in `hook.py` | client codecs and `session/` | The hook is now the visible process boundary and composition point. |
| Reconstructed check sequence | `python scripts/check.py` | One repository-local command runs the complete local verification path. |

Internal module paths were never a promised public API, so the ownership moves
were direct. No compatibility packages, aliases, dual parsers, or adapter flags
were added. Existing valid `config.toml` files, native hook envelopes, detector
output, settings merge/revert semantics, and watch forwarding behavior remain
valid.

## Non-obvious decisions retained

- Stored targets are always scrubbed with the full detector entity set. A user
  may narrow payload detection but cannot weaken storage sanitization.
- Untrusted tool, event, target, and model labels are bounded and made printable
  before persistence or display.
- Prompt inspection failures fail closed. Tool-event inspection failures pass
  the existing work through unchanged and report that it was not inspected;
  denying an already-created result protects nothing and loses work.
- Local writes and command strings are never rewritten. A placeholder in a real
  file is data loss, and a modified command is a different command.
- `Stop` renders a Claude session summary because `SessionEnd` output is not
  shown. `SessionEnd` is used for cleanup.
- The hook and detector are network-free. `shim watch` is the sole network path
  and only forwards the client's request to its already configured provider.
- Exact provider usage and inferred section attribution remain visibly
  distinct.
- The independently built, hashed, benchmarked, attested, and tested release
  paths remain redundant because they protect different trust boundaries.

## Evidence preserved

The dated [Claude hook probe](probe-2026-08.md) remains the source for native
payload shapes and lifecycle behavior. `guard-v2.json`, `guard-tools-v1.json`,
and `parity-v1.json` remain executable detector contracts. In particular,
`parity-v1.json` is migration evidence, not a fixture to regenerate when a test
fails; deliberate differences need a reason and an equally tight expected
result.

## Non-goals and stop condition

Do not:

- split the package into services, Polylith components, or multiple
  distributions;
- add dynamic client discovery, a generic adapter registry, or ports for
  hypothetical storage, proxy, detector, or configuration replacements;
- create `common`, `misc`, `helpers`, or `utils` ownership buckets;
- rewrite the guarded publisher, detector, recognizer catalog, proxy, corpus, or
  release-integrity system without a reproduced defect;
- split modules to meet line-count targets or optimize without a measured
  regression;
- add compatibility aliases for internal moves;
- continue rearranging code once the import contract and `python
  scripts/check.py` pass.

The 0.2.0 release remains blocked until the package, lock, plugin archive,
version metadata, release notes, and fresh client evidence agree. See
[Compatibility](compatibility.md) for that evidence gate and
[0.2.0 release notes](releases/0.2.0.md) for public and persisted-surface
changes.
