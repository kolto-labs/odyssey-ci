# Manual reference lane

The `manual` job in `.github/workflows/ci.yml` installs the full K1 or K2 guide
literally, with each mod's original patcher, to produce a reference install that
the port lanes are compared against.

| Build | Guide |
| --- | --- |
| K1 | `content/k1/full.md` |
| K2 | `content/k2/full.md` |

## In CI

For each game:

1. Looks for `ci/<game>-manual-plan.json`. If it is absent the job writes a step
   summary ("manual lane failed: no reviewed plan, nothing installed"), uploads the
   `draft_plan.py` skeleton as `manual-drafts-<game>` and then fails with an
   `::error::`. No manual evidence exists until a human-reviewed plan is committed, so
   the lane stays red until then. A plan that exists but
   fails validation, or a missing game or archive, still fails the job.
2. Installs Wine, Xvfb and archive tools.
3. Restores `game-<game>-v1` and runs `game_cache.py unpack`, which verifies every
   file, then prints the game inventory. Nothing is renamed or lowercased: the game
   tree keeps Steam's original spelling (for example K2's `Modules/`), and
   `manual.py` resolves the plan's game paths case-insensitively instead (see
   [The plan](#the-plan)).
4. Restores archives with `cache.py restore` using the read-only cache credentials.
   Restore is fail-closed: if any archive of the guide is not in the cache (or fails
   verification), the step exits non-zero and the lane stops before any install
   step. This job never acquires new links; only the `mods` job fills the cache.
   See [cache.md](cache.md).
5. Installs PyKotor at the exact version `manual.PYKOTOR_REQUIREMENT` names, then
   runs `xvfb-run -a python tools/modsync-ci/manual.py --plan <plan> --guide
   content/<game>/full.md --game-dir ... --archives ... --work-dir ... --report ...`.

Linux runners only: `manual.py` is POSIX only, and Windows patchers run under Wine.

## The plan

`manual.py` executes a human-reviewed JSON plan. It never generates, infers or
approves operations itself. The full contract is in the docstring of
`tools/modsync-ci/manual.py`. In short:

- `guide_sha256` pins the guide; `archive_sha256` binds every archive input file.
- `steps` follow every `###` heading in `## Mod List`, once each, in order. Each
  step lists `operations`: `extract`, `copy`, `rename`, `delete`, `patcher` or
  `gui_patcher`, or is a reviewed [omission](#omitting-a-step-partial-plans).
- `supplemental_sections` gives a written review for every heading outside the
  Mod List and links any steps it affects.
- A `patcher` operation names the original patcher and its SHA-256, exact `argv`
  (optionally after an absolute `wine` path), working directory, success log,
  exact success markers, failure regexes, and expected output files or a required
  manifest change.

- Game paths are matched case-insensitively, as on Windows, where the guide's
  "move the files to Override" or "delete X" ignores case. `copy`, `rename` and
  `delete` targets, `if_any_exists` paths and every `expected_files` path are
  resolved with PyKotor's `CaseAwarePath` (`pykotor.tools.path`, pinned by
  `manual.PYKOTOR_REQUIREMENT`; `manual.py` fails at startup without it). A path
  component that matches an existing entry case-insensitively is that entry, under
  its existing name; the first component without a match and everything after it
  are created as the plan spells them. Nothing is ever renamed or lowercased.
- A `copy` operation overlays a file or directory onto its destination the way
  Windows does: every destination component (the plan's own and each copied
  entry's) that matches an existing entry case-insensitively is that entry, kept
  under its existing name and overwritten; anything else is created under the name
  as given (a copied entry keeps its source name). Two existing entries, or two
  source entries, that differ only in case stop the step.
- A `copy` operation may set `skip_existing: true` (default `false`) for the
  guide's "do not overwrite" instructions. A destination file that already exists
  (matched case-insensitively) is left untouched and that source file is not
  copied. Directories are still merged.
- A `rename` operation, `{"type": "rename", "source": "{game}/...", "destination":
  "{game}/...", "replace": false}`, moves an installed file or directory inside the
  game tree, for guide instructions such as renaming `w_ionrfl_04.mdl` to
  `w_ionrfl_004.mdl` in Override. Both paths resolve case-insensitively; an
  existing destination keeps its existing name, and a new one is created as the
  plan spells it. The source must exist. The destination must not
  exist unless `replace` is `true`, and then only an existing file can be replaced
  by a file; replacing a directory is an error. Missing parent directories of the
  destination are created.

Use the mod's own original patcher. Do not substitute ModSync's `vendor/bin`
patchers, OdyPatcher or another HoloPatcher build. A HoloPatcher build shipped by
a mod has its own command line, so it runs as a `patcher` with that CLI.

### GUI-only patchers (`gui_patcher`)

TSLPatcher.exe has no command line: it is a GUI-only program, so it cannot be run as
a `patcher`. A `gui_patcher` operation runs the original TSLPatcher.exe under Wine
on the Xvfb display (the CI step runs `manual.py` under `xvfb-run -a`) and replays
an explicit, reviewed list of UI actions with `xdotool`:

```json
{"type": "gui_patcher",
 "patcher": "{extract}/example/TSLPatcher.exe",
 "sha256": "<64 hex>",
 "argv": ["/usr/bin/wine", "{extract}/example/TSLPatcher.exe"],
 "cwd": "{extract}/example",
 "success_log": "{extract}/example/installlog.rtf",
 "success_markers": ["Done. Changes have been applied"],
 "failure_regex": ["(?i)error"],
 "expected_changes": "changed_manifest",
 "wine_registry": [{"key": "HKEY_LOCAL_MACHINE\\Software\\BioWare\\SW\\KOTOR",
                    "name": "Path", "data": "{game_win}"}],
 "actions": [
   {"wait_window": "^Game Data Installer", "timeout": 60},
   {"click": [420, 380], "window": "^Game Data Installer"},
   {"wait_window": "^Browse", "timeout": 30},
   {"type": "{game_win}", "window": "^Browse"},
   {"key": ["Return"], "window": "^Browse"},
   {"wait_log": 600}]}
```

(The titles, coordinates and markers above are placeholders; take the real ones
from a probe run of that patcher.)

- It keeps every `patcher` guarantee: SHA-256 identity of the original patcher,
  `argv` naming it directly or after an absolute `wine`/`wine64`, a fresh success
  log with every exact marker and no failure regex match, expected files or a
  required manifest change, and the game manifest must change.
- `actions` run in order, each exactly one of `wait_window` (with `timeout`),
  `key`, `type` or `click` (each with a `window` regex), `sleep` (at most 60
  seconds) or `wait_log` (seconds to wait until every success marker is present
  or a failure regex matches). Nothing is inferred: no default action, title,
  keystroke or wait exists. Every wait is also bounded by the step deadline.
- `{game_win}` in `type` text and `wine_registry` data expands to the game root as
  a Wine path (`Z:` followed by the absolute path with `\` separators).
  `wine_registry` entries run `wine reg add` before launch, for patchers that read
  the install path from the registry; keys must start with
  `HKEY_LOCAL_MACHINE\` or `HKEY_CURRENT_USER\`. Each value is read first with
  `wine reg query` and put back (or deleted, if it did not exist) after the
  patcher stopped, on success and on failure, so no setting leaks into later
  steps. Only `REG_SZ`, `REG_EXPAND_SZ` and `REG_DWORD` values can be read back;
  any other existing value blocks the step before anything is written. A failed
  restore fails a run that otherwise succeeded.
- Generated TSLPatcher operations (`expand_patchers.py`) do not walk the folder
  tree of TSLPatcher's "Browse for Folder" dialog. They set the Wine user's
  Desktop folder (`HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders`,
  value `Desktop`) to `{game_win}`, click the dialog's root item "Desktop" and
  then OK. Wine's dialog returns the Desktop item as the Desktop folder's path,
  which is now the game folder, so TSLPatcher gets the game folder without any
  navigation and without changing the mod's `changes.ini`. The value is restored
  after the step.
- TSLPatcher stays open after installing, so after the last action `manual.py`
  kills the process and runs `wineserver -k`. Its exit code is not evidence; only
  the log, markers and manifest are.
- TSLPatcher writes `installlog.rtf` as plain ANSI RTF. Markers match the raw RTF
  text. The log is decoded as UTF-8 and, only for `gui_patcher`, as cp1252 if that
  fails. Bytes are never replaced or dropped.
- While `wait_log` polls, the step log lists the visible window names every 30
  seconds, so a stuck or error dialog is named. On failure the step log lists the
  visible window names, and if ImageMagick's `import` is available a screenshot of
  the root window is stored next to it in the private checkpoint directory.
- When a `patcher` or `gui_patcher` fails, its own success log (if it exists; at
  most the first 1 MiB) is copied next to the step log as `installer-log-N.<ext>`
  before rollback wipes `{extract}` and restores the game.
- In CI the manual job uploads `manual-diagnostics-<game>` (always, when a plan is
  present): per checkpoint `step-NNNN/` the small top-level files, i.e.
  `operations.log`, `gui-failure-N.png` and `installer-log-N.*`, never the game
  snapshot `before/` or the `before.json`/`after.json` manifests.
- Generated TSLPatcher plans (`expand_patchers.py`) wait 600 seconds in
  `wait_log`; one mod installs in well under two minutes.
- `xdotool`, `wine` and `wineserver` must be on `PATH` and `DISPLAY` must be set,
  otherwise the step fails with a clear error.

## Omitting a step (partial plans)

When a step cannot be authored honestly yet (for example its original archive or a
verified patcher run is not available), the reviewer may omit it explicitly instead
of leaving the whole lane without a plan:

```json
{"id": "step-042", "source_heading": "### Example Mod", "omit": {"reason": "original patcher not yet verified under Wine", "reviewed_by": "Reviewer Name"}}
```

Rules, all enforced by `manual.py`:

- The step still names its exact `source_heading` and keeps its place in guide order.
  Every `###` step is either executed or omitted; a missing step is still an error.
- `omit` takes exactly `reason` and `reviewed_by`, both nonempty printable strings
  (no line breaks or control characters; they are echoed into the CI log).
- An omitted step has no `operations` (not even `[]`) and no `timeout_seconds`. Giving
  both `omit` and `operations` is an error, so a step can never be half-executed.
- At least one step must still execute; an all-omitted plan is rejected.
- Omission is never inferred or defaulted. `draft_plan.py` does not write `omit`
  entries, and a draft step with empty `operations` is still rejected.

Omissions are visible, not silent:

- Each omitted step prints a `::warning title=manual step omitted::` line naming its
  id, heading, reviewer and reason, followed by a
  `::warning title=partial manual install::` summary, at the start and again at the
  end of the run.
- The report records every step. Omitted ones have status `omitted` with `reason` and
  `reviewed_by`, and the report carries `partial` (`true` or `false`) and an
  `omitted_steps` list of `{id, heading, reason, reviewed_by}`.
- A run with omissions ends with report status `partial` instead of `complete` and
  exits 0, because every executed step was verified. A run without omissions is
  unchanged: status `complete`, `partial` false.

A partial result is not a full manual reference install. The executed steps ran
without the omitted ones, and later rows in shared `.2da`/`.tlk` files may differ
from a full run. Do not use a partial run to claim parity for any resource the omitted
steps touch, and remove each omission as soon as its step can be authored. The
`cache.py restore` step still restores every archive of the guide; a link that is
uncached by policy must also be passed with `--allow-uncached-by-policy`, which is
independent of `omit`.

## Drafting a plan

`tools/modsync-ci/draft_plan.py` gives the reviewer a starting point. It reads
`content/<game>/full.md` and writes two files under `ci/drafts/`:

- `<game>-manual-plan.draft.json`: one step per `###` heading in `## Mod List`, in
  guide order, plus an entry for every supplemental heading. Every `operations`
  list and every supplemental `review` is empty, and `archive_sha256` is `{}`.
- `<game>-manual-plan.review.json`: every step with status `needs-human-review` and
  the evidence found in the guide: heading and line, the links on the `**Name:**`
  line (with an archive name only when the URL path itself ends in `.zip`, `.7z` or
  `.rar`), the verbatim `**Installation Method:**` text and the lines of each
  `:::` block.

```
python3 tools/modsync-ci/draft_plan.py [--game k1|k2|all] [--force]
```

A draft is not a plan. The tool never invents an operation, `argv`, patcher, hash
or success marker, and it refuses to write `ci/<game>-manual-plan.json` or anything
else in `ci/` outside `ci/drafts/`. `manual.py` rejects a draft three ways: the
top-level `draft` key is an unknown field, empty `operations` are an error, and
empty supplemental reviews are an error. Output is deterministic; an existing draft
that differs is not overwritten without `--force`.

A human authors the real plan from the guide and the original patchers, reviews
every step and supplemental section, drops the `draft` key and commits the result as
`ci/<game>-manual-plan.json`. Do not commit generated or inferred operations as
reviewed.

## Execution rules

- Start from a clean game tree; the work directory must not exist yet.
- Snapshot before each step. Run steps in guide order. Every executed step has a
  complete pre-step snapshot in its checkpoint, verified against the SHA-256
  manifest of the live tree before the step runs. A failed step is restored from
  it into a staging tree, which must match the pre-step manifest before it is
  swapped in, and the live tree must match it again afterwards.
- With the default `--keep-snapshots 1` only the first snapshot is a full copy.
  After a step succeeds, its snapshot is moved to the next step's checkpoint and
  only the entries whose manifest entry changed are rewritten there (changed
  files are unlinked and copied as new files, never edited in place; the game
  tree never shares files with a snapshot). The earlier checkpoint keeps its
  `before.json`/`after.json`, and its report entry names the checkpoint the
  snapshot was advanced to (`snapshot_advanced_to`). With `--keep-snapshots N`
  (N > 1) every snapshot is a full copy and the last N successful ones are kept.
- Manifests reuse a file's SHA-256 while its device, inode, size, mtime and
  ctime are all unchanged. The kernel updates ctime on every write, truncate,
  rename, link, chmod and timestamp change, and programs cannot set it, so a
  patcher that rewrites a file in place and restores its size and mtime is still
  re-hashed; a replaced file has a new inode. Files changed less than two
  seconds before a hash are never cached (timestamp ticks are coarse), and the
  cache is dropped if the clock goes backwards. Like a fresh hash, it does not
  cover a process still writing the tree while it is hashed; manifests are taken
  only while no plan process runs.
- Cost per step on a 4 GB tree: a few walks that `lstat` every file, plus hashing
  and copying of only the files the step changed. Archive inputs are re-checked
  before every operation the same way.
  The first snapshot, a failed step's restore and `--keep-snapshots` above 1
  still copy and hash the whole tree.
- If a step fails, restore its pre-step snapshot and stop. Never skip a step,
  continue past it, or install a fix at the end: loose files are last-writer-wins
  and patchers build on earlier rows in shared `.2da` and `.tlk` files.
- Work, logs, snapshots and reports are private (0700/0600).
- Exit 0 means every executed step completed and verified (report status `complete`,
  or `partial` when steps were reviewed and omitted); 1 means failed or blocked.
  There is no resume.

## Patcher probe

`.github/workflows/manual-probe.yml` checks the patcher mechanics of the reviewed
plans without running a whole guide. `tools/modsync-ci/probe_plan.py build` picks one
step per distinct patcher build and configuration (TSLPatcher namespace options,
`LookupGameFolder`, `ConfirmMessage`, `PlaintextLog`, each HoloPatcher build, the
TSLRCM installer), copies it verbatim from `ci/<game>-manual-plan.json` and turns
every other step into a reviewed omission. `probe_plan.py run` then runs `manual.py`
once per selected step on a fresh copy of the vanilla game and writes a step summary
table; the artifact `manual-probe-<game>` holds each run's report, `manual.py`
output, operation log and failure screenshots. A probe run proves only that a
patcher runs and verifies under Wine; it is never manual-lane evidence. It runs on
branch pushes that touch the plans, `manual.py`, `probe_plan.py` or the workflow, and
on dispatch with `game` and `steps` inputs.

## Comparing with the port lanes

Compare only runs with the same guide revision, selections and baseline. Compare
loose files by content hash and containers by their logical resources. Packing-only
differences are acceptable only after the contents are shown to be equal. Each
finding should name the resource, which lanes have it, the difference and the guide
step responsible.

A reviewed plan or a green infrastructure step is not a successful manual install.
Only an actual end-to-end run with every step verified counts. A `partial` run
(omitted steps) is evidence for the executed steps only.
