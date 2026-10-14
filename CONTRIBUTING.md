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
not a release leaves the archive alone; CI fails one that changes the archive
without changing the version in `pyproject.toml`.

## After a release

Once PyPI serves the new version, the getshim.tech site has to follow, or its
shim-cli page shows the last release. In the site repository: bump
`SHIM_CLI.version` in `lib/constants/products.ts`, re-read the install, figures
and VS Code lines from this README at the new tag, and add a frozen entry to
`lib/constants/agent-instructions.ts` (older entries never change: their
addresses and hashes are public). The site repository opens an issue within a
day when its version and PyPI disagree.
