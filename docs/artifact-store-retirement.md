# Artifact-store retirement

The artifact-store cache is being retired. Splitting storage across workflow runs does not multiply the owner's artifact allowance.

Phase one disables every pack/index publisher in cache-archives.yml, disables its renewal job and caller, and disables the separate encrypted artifact-cache maintenance job (including removing its schedule). Existing code is retained for recovery. No artifact, cache, key, index or pack is deleted or migrated.

Ordinary Actions cache restore/save behavior, keys, paths and encryption are unchanged. Existing artifact-store reads remain available to archive refresh and installation fallback. The read-only round-trip check remains active and can still report a genuine recovery failure. Skipped publication does not itself fail the job. Game backup artifacts and diagnostic artifacts are separate and remain unchanged.

Stopping writes does not free existing storage or resolve quota failures for other artifact uploads. The last complete repository inventory contained 19,412,778,146 artifact bytes. Six referenced mod packs account for 13,225,963,174 bytes; the inspected index references 361 unique blobs, with no demonstrated large redundant pack. The actual owner allowance/billing budget could not be verified with available access.

Stopping renewal leaves current expiration dates in place. The earliest known referenced pack expires January 18, 2027. Before then, required contents need verified durable alternatives: inventory every referenced blob, verify hashes and recoverability of alternatives, and retain originals until that proof exists. No destination, including AllDebrid, is approved. Removing recovery readers is a later phase, conditional on that proof. No deletion or migration is authorized by this change.

Already-running workflows retain their original definitions; this change takes effect for subsequent runs. Existing ordinary-cache rotation behavior remains unchanged, so this is not a guarantee against platform eviction or prior workflow behavior.
