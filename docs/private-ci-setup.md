# Private CI setup

The workflows in `.github/workflows/` run in the private repository
`kolto-labs/odyssey-ci` on GitHub-hosted runners. Logs, artifacts and caches are
private by default. Nothing here pushes to another repository or to the website.

## Enable Actions

Actions starts disabled. Enable it with:

```sh
gh api -X PUT repos/kolto-labs/odyssey-ci/actions/permissions -F enabled=true
```

Scheduled workflows only run from the default branch, `main`.

## Minutes, storage and the spending limit

Private repositories use the account's included Actions quota. On the Free plan that
is 2,000 minutes per month and 500 MB of artifact storage. With a $0 spending limit,
GitHub stops jobs (and refuses artifact uploads over quota) instead of billing.

- Linux minutes count 1x, Windows 2x, macOS 10x. `private-ci-toolkit.yml` runs on
  all three, and `full-build-*.yml` with `runner-mode: hosted` can select Windows
  or macOS (hosted full builds are currently refused by the lane contract).
- A full `ci.yml` run (game, mods, 8 install lanes, 2 manual lanes, the build jobs)
  takes several hours of runner time; two scheduled runs a week
  (`23 4 * * 1,4`) alone can use most of the Free quota.
- The `game-<game>-sealed` backup artifacts (~3 GB each) and the artifact-store
  packs (up to 3 GiB each) do not fit the Free plan's 500 MB artifact storage. The
  Actions cache (10 GB per repository) is not billed and still works.

The schedules are kept:

| Workflow | Schedule | Runner |
| --- | --- | --- |
| `ci.yml` | Monday and Thursday, 04:23 UTC | hosted (`ubuntu-latest`) |
| `artifact-cache-refresh.yml` | 1st and 15th, 06:00 UTC | self-hosted, labels `self-hosted`, `modsync-session` |

To pause a schedule without editing files: `gh workflow disable ci.yml -R kolto-labs/odyssey-ci`
(`gh workflow enable` turns it back on). `artifact-cache-refresh.yml` waits in the
queue (and fails after 24 hours) when no runner with its labels is online.

## Concurrency

- `ci.yml`: push, schedule, release and dispatch runs share the group `ci-full`, so
  two full runs never overlap; a newer queued run replaces an older queued one, a
  running one is never cancelled. Pull requests use one group per pull request and a
  new commit cancels the previous run. Jobs that write the mod cache also share the
  job-level group `mod-cache-writer`.
- `full-build-{cli,gui,manual}-{k1,k2}.yml`: one run per workflow at a time.
- `manual-probe.yml`: one run per ref; a newer push cancels the older run.
- `artifact-cache-refresh.yml`: one run at a time (`encrypted-artifact-cache`).
- `artifact-cache-toolkit.yml`, `private-ci-toolkit.yml`: one run per ref; a newer
  one cancels the older.

## Secrets

Names referenced as `secrets.*` under `.github/`. Optional ones may be left unset.

| Name | Used by | Purpose |
| --- | --- | --- |
| `CACHE_ENCRYPTION_KEY` | ci, manual-probe | Seals the game cache, the `game-<game>-sealed` backup and artifact-store blobs |
| `DIAGNOSTICS_ENCRYPTION_KEY` | ci | Seals install lane reports and convert logs before upload |
| `STEAM_USERNAME` | ci | Steam account that owns both games |
| `STEAM_PASSWORD` | ci | Password of that account |
| `STEAM_CONFIG_VDF` | ci (optional) | Base64 SteamCMD `config.vdf`, a saved login that needs no Steam Guard approval |
| `STEAM_RECOVERY_CODE` | ci (optional) | Steam Guard recovery code(s), enabled only by manual `use_steam_guard_codes=true`, tried before `STEAM_GUARD_CODES` |
| `STEAM_GUARD_CODES` | ci (optional) | Whitespace-separated single-use Steam Guard backup codes, enabled only by manual `use_steam_guard_codes=true` |
| `GAME_SEED_URL_K1`, `GAME_SEED_URL_K2` | ci (optional) | URL of the encrypted game seed tried before SteamCMD |
| `GAME_SEED_KEY_K1`, `GAME_SEED_KEY_K2` | ci (optional) | Key that decrypts that seed |
| `GAME_SEED_SHA256_K1`, `GAME_SEED_SHA256_K2` | ci (optional) | Expected SHA-256 of the seed |
| `MOD_CACHE_S3_ACCESS_KEY_ID` | ci (optional) | R2 read/write key id (`mods` and `seed` jobs) |
| `MOD_CACHE_S3_SECRET_ACCESS_KEY` | ci (optional) | R2 read/write secret |
| `MOD_CACHE_S3_READ_ACCESS_KEY_ID` | ci, manual-probe (optional) | R2 read-only key id (install, manual and probe lanes) |
| `MOD_CACHE_S3_READ_SECRET_ACCESS_KEY` | ci, manual-probe (optional) | R2 read-only secret |
| `MOD_CACHE_S3_ENDPOINT` | ci, manual-probe (optional) | S3-compatible (R2) endpoint URL |
| `SEED_STAGING_RCLONE_CONF_B64` | ci `seed` job (optional) | Base64 rclone config of the staging remote; set it on the `mod-cache-seed` environment |
| `MODSYNC_SOURCE_TOKEN` | ci | Read access to the ModSync source repositories and their private Cargo git dependencies |
| `FULL_BUILD_CONFIG` | full-build-*, artifact-cache-refresh | Private JSON configuration of the full-build lanes and the artifact-cache payload command |
| `PRIVATE_CI_TOKEN` | full-build-* (required by the reusable job) | Read-only token for the private installer sources |
| `PRIVATE_CI_RECIPIENT` | full-build-* (required by the reusable job) | Public `age1…` recipient that encrypts full-build receipts |
| `CACHE_ACCESS_KEY_ID` | full-build-* (optional) | Cache access key id passed to the reusable full-build job (not read by its steps yet) |
| `CACHE_SECRET_ACCESS_KEY` | full-build-* (optional) | Cache secret passed to the reusable full-build job (not read by its steps yet) |

`GITHUB_TOKEN` is provided by Actions and needs no setup.

Set each secret from stdin (paste the value, then Ctrl-D) or from a file:

```sh
R=kolto-labs/odyssey-ci
for name in CACHE_ENCRYPTION_KEY DIAGNOSTICS_ENCRYPTION_KEY \
    STEAM_USERNAME STEAM_PASSWORD STEAM_RECOVERY_CODE STEAM_GUARD_CODES \
    GAME_SEED_URL_K1 GAME_SEED_KEY_K1 GAME_SEED_SHA256_K1 \
    GAME_SEED_URL_K2 GAME_SEED_KEY_K2 GAME_SEED_SHA256_K2 \
    MOD_CACHE_S3_ACCESS_KEY_ID MOD_CACHE_S3_SECRET_ACCESS_KEY \
    MOD_CACHE_S3_READ_ACCESS_KEY_ID MOD_CACHE_S3_READ_SECRET_ACCESS_KEY MOD_CACHE_S3_ENDPOINT \
    MODSYNC_SOURCE_TOKEN FULL_BUILD_CONFIG PRIVATE_CI_TOKEN PRIVATE_CI_RECIPIENT \
    CACHE_ACCESS_KEY_ID CACHE_SECRET_ACCESS_KEY; do
  echo "== $name"; gh secret set "$name" -R "$R"
done

base64 -w0 ~/Steam/config/config.vdf | gh secret set STEAM_CONFIG_VDF -R kolto-labs/odyssey-ci
gh secret set FULL_BUILD_CONFIG -R kolto-labs/odyssey-ci < /path/to/full-build-config.json

# Environment secret for the seed job (create the environment first in Settings → Environments)
base64 -w0 rclone.conf | gh secret set SEED_STAGING_RCLONE_CONF_B64 -R kolto-labs/odyssey-ci --env mod-cache-seed

gh secret list -R kolto-labs/odyssey-ci
```

Skip the names you do not use; unset optional secrets disable only their source.

## Variables

Names referenced as `vars.*` under `.github/`.

| Name | Used by | Purpose |
| --- | --- | --- |
| `MOD_CACHE_BUCKET` | ci, manual-probe (optional) | R2 bucket name of the mod archive cache |
| `MODSYNC_RS_REPOSITORY` | ci | `owner/name` of the Rust ModSync repository the `rust` lanes build |
| `MODSYNC_TS_REPOSITORY` | ci | `owner/name` of the TypeScript ModSync repository the `typescript` lanes build |
| `MODSYNC_TS_REF` | ci (optional) | ModSync.ts ref to build; default `feat/physical-install` |

```sh
gh variable set MOD_CACHE_BUCKET -R kolto-labs/odyssey-ci --body BUCKET
gh variable set MODSYNC_RS_REPOSITORY -R kolto-labs/odyssey-ci --body OWNER/NAME
gh variable set MODSYNC_TS_REPOSITORY -R kolto-labs/odyssey-ci --body OWNER/NAME
gh variable set MODSYNC_TS_REF -R kolto-labs/odyssey-ci --body feat/physical-install
gh variable list -R kolto-labs/odyssey-ci
```

## Self-hosted runner (no hosted minutes)

Jobs on a self-hosted runner use no hosted minutes. A runner in a private repository
only runs this repository's jobs, but it still executes whatever a workflow on a
pushed branch tells it to, so use a dedicated machine or VM.

1. Get a registration token:

   ```sh
   gh api -X POST repos/kolto-labs/odyssey-ci/actions/runners/registration-token --jq .token
   ```

2. Download the runner from **Settings → Actions → Runners → New self-hosted runner**
   (the page lists the current version and checksum), then configure it with the
   labels the workflows ask for:

   ```sh
   ./config.sh --url https://github.com/kolto-labs/odyssey-ci --token TOKEN \
     --name odyssey-linux-1 --labels modsync-session --unattended
   sudo ./svc.sh install && sudo ./svc.sh start
   ```

   Every runner gets `self-hosted` and its OS/architecture labels (`Linux`, `X64`)
   automatically; `--labels` adds the custom ones.

3. Labels the workflows already use:

   - `artifact-cache-refresh.yml`: `runs-on: [self-hosted, modsync-session]`.
   - `full-build-*.yml` with `runner-mode: self-hosted` (and the manual lanes, which
     always use it): `runs-on: [self-hosted]`. A Windows or macOS lane needs a runner
     on that OS; add the OS label in `full-build-job.yml` (for example
     `fromJSON('["self-hosted","Windows"]')`) if more than one OS registers.

4. To move a hosted job such as `ci.yml` or `manual-probe.yml` onto the runner,
   change its `runs-on: ubuntu-latest` to `runs-on: [self-hosted, Linux, X64]` (or a
   custom label such as `[self-hosted, odyssey]`) and list any new custom label in
   `.github/actionlint.yaml` under `self-hosted-runner.labels`. The jobs assume an
   Ubuntu host with passwordless `sudo`, `apt-get`, Python 3 and at least ~40 GB of
   free disk; the `Free disk space` steps delete hosted-image directories and are
   harmless where those do not exist.
