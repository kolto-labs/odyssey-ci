# CI architecture and cache preservation map

Updated 2026-11-03. This document separates inspected implementation, observed Actions results, and owner requirements. It does not claim that all archives are cached or that full-guide installs pass.

## Owner requirements

- Odyssey CI remains private while development continues and owns the shared CI implementation.
- Associated project repositories invoke that implementation rather than duplicating the test logic.
- Public project execution is intended to avoid private hosted-runner minute consumption. Public launcher YAML remains visible; private implementation must be fetched with authorized credentials and must not leak through logs or artifacts.
- Mod-builds pushes and published releases should trigger archive acquisition/cache maintenance. Exact branch filters remain to be agreed.
- Preserve existing downloaded archives, indexes, hashes, and encryption keys before changing execution or storage location. Do not rebuild the archive collection unnecessarily.
- Installation verification must compare corresponding steps across installation lanes. Exact lane count, selections, comparison semantics, and mismatch behavior remain to be specified.
- Explain proposed changes before committing them.

## Repository map

| Repository | Verified current role | Target role or unresolved work |
| --- | --- | --- |
| kolto-labs/odyssey-ci (private, main) | Hosts the main CI workflow, game acquisition, archive-cache maintenance, installation lanes, and artifact-store tools. It checks out private ModSync implementations. | Retain private shared implementation; expose a deliberate entry point for associated project launchers. Preserve existing storage while wiring callers. |
| kolto-labs/mod-builds (public, default branch dev) | Inspected dev and main trees contain metadata and release workflows, without an Odyssey CI launcher. Supplies public guide content. | Launch the private archive-maintenance implementation on agreed pushes and published releases; execution and artifact ownership must be explicit. |
| bodencrouch/ModSync.rs (private, main) | Production CI builds and lints Rust on Linux, including Windows cross-checks; optional native builds are manually selected. Synthetic workflow files also exist. | Identify which checks should invoke Odyssey CI and which remain local production checks. Synthetic fixtures are not full real-guide installation evidence. |
| bodencrouch/ModSync.ts (private, default branch claude/modsync-nodejs-typescript-0ea3qs) | Default-branch CI installs dependencies, runs pnpm check, and prepares Electron packaging on Linux/Windows. Six simulation workflow files exist. Odyssey CI defaults its TypeScript source ref to feat/physical-install unless overridden. | Define shared CI entry points and explicitly select source revisions. Do not assume the default branch equals the tested physical-install branch. |
| kolto-labs/KPHarness (private, main) | Repository access and README verified. No .github/workflows directory was returned on main. Other branches and execution integration are not yet audited. | Confirm its role and applicable branch before specifying its Odyssey CI integration. |

## Existing storage and receipts

### Actions cache

Odyssey CI run [41446244828](https://github.com/kolto-labs/odyssey-ci/actions/runs/41446244828), started 2026-11-02, contains these log receipts in its mods job:

- Restored `mod-archives-fcff4c751aa8470e`, reporting 2,674,133,236 bytes (about 2.67 GB).
- Saved the updated cache as `mod-archives-c84fdc3101fa4489`.
- Its existing prune step then deleted the older key. This was observed historical workflow behavior, not an action performed during this review.

These logs prove a restore and save on November 2. They do not prove that the saved entry remains present today. Direct listing of current Actions cache entries was unavailable through the connector's permitted endpoints.

### Artifact storage

Run [39499301515](https://github.com/kolto-labs/odyssey-ci/actions/runs/39499301515) has these artifacts, returned as unexpired on 2026-11-03:

| Artifact | ID | Reported size | Expiration |
| --- | --- | --- | --- |
| artstore-pack-39499301515-1-0 | 12302900035 | 2,915,268,748 bytes | 2027-01-18T21:18:43Z |
| artstore-index-39499301515-1-0 | 12301995270 | 126,501 bytes | 2027-01-18T21:18:43Z |

An index and pack existing does not establish complete guide coverage or successful decryption of every object.

Run [40128022409](https://github.com/kolto-labs/odyssey-ci/actions/runs/40128022409) also has an unexpired `game-k2-sealed` backup, artifact ID 12574050387, reported size 3,158,273,346 bytes, expiring 2027-01-22T20:02:46Z. This is a game baseline backup, separate from mod archives.

This is a verified sample, not an exhaustive inventory or account-wide storage total.

### Current upload blocker

On November 2, run 41446244828 staged seven archive files totaling 4,812,377,083 bytes for publication. Each of its three pack upload attempts failed with:

> Artifact storage quota has been hit. Unable to upload any new artifacts.

The subsequent artifact-store round-trip check failed. The run returned no uploaded artifacts when inspected. The Actions cache save succeeded independently.

Therefore: new artifact publication was blocked, but the separate Actions cache was operational in that run. The precise account allowance, billed usage, and present quota state have not been retrieved. An overall failed workflow does not imply that all earlier cache steps failed or that stored archives disappeared.

## How archive reuse works

The implementation provides two different GitHub storage tiers:

1. A bounded Actions cache accelerates access to a subset of archives.
2. The artifact store retains encrypted archive parts and an index mapping archive SHA-256 identities to those parts across runs. Readers retrieve, decrypt, and verify objects rather than downloading the same archive again.

Existing documentation describes refreshing artifacts near expiration by copying them into newly uploaded packs and indexes. Artifacts have retention limits and quota; using prior runs is not permanent or unlimited storage.

The optional external bucket configuration targets Cloudflare R2 through an S3-compatible API. It is optional, and the owner does not want a paid storage dependency. No external storage purchase or configuration is authorized by this map.

Implementation references: [CI](ci.md), [cache](cache.md), [private setup](private-ci-setup.md), [artifact-store tooling](../tools/modsync-ci/artstore.py), and [private execution tooling](../tools/private-ci/README.md).

## Intended invocation model and the remaining gap

A public project needs a small public launcher that fetches the authorized private Odyssey CI implementation and executes its entry point. It cannot directly invoke a private reusable workflow through GitHub's public-to-private workflow-sharing mechanism.

Fetching a private repository does not automatically execute its workflow YAML. Shared behavior must have a runnable script/action entry point, with the necessary public job orchestration.

Starting a run in private Odyssey CI via dispatch would keep that run and its storage owned by Odyssey CI; it does not accomplish the stated goal of executing under a public project. Running under a different repository also does not automatically relocate existing caches or artifact indexes.

References:
- [Private workflow access restrictions](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)
- [Checking out another private repository](https://github.com/actions/checkout#checkout-multiple-repos-private)

## Work map, in dependency order

| Stage | Required work | Completion evidence |
| --- | --- | --- |
| 1. Inventory and preserve | Enumerate live cache keys, packs, indexes, game backups, expiration dates, and object coverage. Verify that the required encryption key remains available without exposing its value. | An inventory tied to repository/run/artifact IDs and successful restore/hash verification of retained objects. |
| 2. Resolve upload capacity | Determine why artifact quota blocks uploads and choose a capacity/retention arrangement consistent with the no-paid-storage requirement. | A successful bounded upload and download verification. Do not delete the only stored copy to make room. |
| 3. Design reuse or transfer | Decide whether new public jobs read the existing private artifact store with authorized access or copy verified objects into the new destination. Handle Actions cache separately. | New execution path restores existing bytes and verifies hashes without upstream archive re-download; source copies retained until verification. |
| 4. Wire public launcher | Add agreed mod-builds event filters, pinned private implementation fetch, constrained credentials, private output handling, and explicit artifact repository selection. | A trusted push/release runs the intended private implementation in the intended repository. |
| 5. Wire implementation projects | Inspect all relevant branches and define ModSync.rs, ModSync.ts, and KPHarness entry points. | Each intended caller records the tested source revision and executes the shared checks. |
| 6. Enforce installation comparison | Agree lane count, common baseline/archives/choices, step boundaries, resource comparison, and failure behavior. Connect recorded checkpoints to actual comparison gates. | A deliberate mismatch is attributed to the first differing step and prevents the agreed continuation. |

## Project tracking status

A GitHub Projects board has not been created. The available connector has no Projects creation operation, and the inspected browser session was signed out. This document records the work map without representing it as an existing board.

No cache deletion, migration, secret modification, workflow dispatch, or workflow change was performed as part of writing this document.
