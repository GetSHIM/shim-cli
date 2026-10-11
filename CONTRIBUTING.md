# Contributing

Keep changes small, local, and evidenced. Do not add client adapters, network
paths, telemetry, persistent state, or a self-updater without an approved
design.

Before a pull request:

```console
python scripts/check.py
```

Use synthetic values in tests and examples. Never commit real prompts, secrets,
or personal data. Changes to hook output, supported entities, installation, or
compatibility claims need contract and documentation updates.

## Issues

Open an issue at <https://github.com/GetSHIM/shim-cli/issues/new/choose>: a
bug report, or a detection report for a value shim missed or flagged by
mistake. Paste synthetic values only; the issue is public. A missed value is
the most useful report you can send. Security vulnerabilities go through a
private advisory, as [SECURITY.md](SECURITY.md) describes, never an issue.

## The plugin archive

`plugins/shim-cli/bin/shim.pyz` is rebuilt only in a release commit, with
`scripts/build_zipapp.py`. The Claude Code marketplace installs the plugin from
`main`, so an archive rebuilt between releases would ship unreleased code to
plugin users while PyPI still serves the last release. A pull request that is
not a release leaves the archive alone.
