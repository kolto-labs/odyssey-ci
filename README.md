# Odyssey CI

Private continuous integration for the KOTOR Community Mod Builds guides. The guide
content (`content/`, `scripts/`, `meta.json`) is a copy of the public guides and is
the input the tools in `tools/` verify; it is not published from here.

If you are looking to install mods, <ins>**PLEASE FOLLOW THE INSTRUCTIONS ON THE
[WEBSITE](https://kotor.neocities.org/modding/mod_builds/)**</ins>.

## Where things are

- `.github/workflows/ci.yml` — game acquisition, the mod archive cache and every
  install lane. See [docs/ci.md](docs/ci.md).
- `.github/workflows/manual-probe.yml` — patcher probes for the reviewed manual plans
  in `ci/`.
- `.github/workflows/full-build-*.yml` — the private full-build lanes; see
  [tools/private-ci/README.md](tools/private-ci/README.md).
- `.github/workflows/artifact-cache-*.yml`, `private-ci-toolkit.yml` — the encrypted
  artifact cache and credential-free checks of the private tooling.

## Branches

`main` is the default branch. Pushes to `main`, the schedule and manual dispatch write
the Actions caches; pull requests only read them.

## Setup

Secrets, variables, enabling Actions, minute limits and self-hosted runners are
described in [docs/private-ci-setup.md](docs/private-ci-setup.md).
