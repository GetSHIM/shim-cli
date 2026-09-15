# Security policy

## Supported versions

Only the latest released `shim` version receives security fixes.

## Reporting a vulnerability

Please report suspected vulnerabilities through a private GitHub Security
Advisory for this repository. Do not include exploit details in a public issue.

Include the affected version, environment, reproduction steps, impact, and any
relevant redacted logs. Please avoid sending real secrets or personal data. We
will acknowledge the report, assess it, and coordinate disclosure through the
advisory.

To verify a downloaded release against its attestation bundles, run the
`gh attestation verify` command in
[the compatibility record](docs/compatibility.md#030-release-evidence).

## Scope notes

The hook is a local best-effort guard, not an enforcement boundary. A report
involving prompt leakage, unsafe hook output, persistence beyond the documented
temporary suggestion, session spool, or opt-in ledger boundaries, an
undocumented network destination or transmission, installer ownership
failures, or an unsafe fail-open path is in scope.
