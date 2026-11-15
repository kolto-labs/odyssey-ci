# Deadly Stream preservation runners

Run `Preserve Deadly Stream to cloud` in `kolto-labs/odyssey-ci` using `workflow_dispatch` on `main`:

```sh
gh workflow run deadlystream-preservation.yml -R kolto-labs/odyssey-ci \
  -r main -f mode=both -f minutes=240
```

Modes are `both`, `capture`, `deliver`, and `probe`. A completed run means its bounded batch finished, not that the entire site has been preserved. Inspect the encrypted checkpoint counts and independent Drive verification before asserting coverage.

Source requests use the runner's verified Cloudflare WARP SOCKS proxy, retain TLS verification, identify the collector, honor robots and backoff, and never fall back to a direct connection. The collector streams originals into encrypted R2 chunks; native game and diagnostics credentials are not used. Downloaded content is never executed.

The existing organization settings are sufficient; no additional manually configured secret was needed:

- `DEADLYSTREAM_CACHE_WRITE_ACCESS_KEY_ID`
- `DEADLYSTREAM_CACHE_WRITE_SECRET_ACCESS_KEY`
- `DEADLYSTREAM_CACHE_READ_ACCESS_KEY_ID`
- `DEADLYSTREAM_CACHE_READ_SECRET_ACCESS_KEY`
- `DEADLYSTREAM_CACHE_DECRYPTION_IDENTITY`

Nonsecret variables are `DEADLYSTREAM_CACHE_BUCKET`, `DEADLYSTREAM_CACHE_ENDPOINT`, and `DEADLYSTREAM_CACHE_ENCRYPTION_RECIPIENT`. The scripts retain existing defaults. GitHub resolves same-name secrets normally: a repository setting overrides an organization setting. Either scope works; defining both is not an error.

The metadata-only owner-side broker issues one-file Google resumable upload capabilities and places them in an encrypted R2 job manifest. The runner streams verified originals straight from R2 to those upload destinations. Google account credentials stay outside Actions. The broker then independently checks each Drive object's SHA256, size and parent folder before recording delivery. This broker must remain available to issue further Drive capabilities; source capture and encrypted recovery continue independently on the runner.

Only manually dispatched runs on the owner repository's main branch receive catalogue credentials. The built-in token has `contents: read`; no token is persisted in the checkout. Runs serialize through one concurrency group. No source payloads, cookies, private identities, upload capabilities or plaintext checkpoints are published as Actions artifacts.

`whole-site/actions/latest.json` points to immutable, independently read-back encrypted checkpoints. Checkpoints include a consistent discovery database and completed source pages; captured native payloads retain their immutable encrypted manifests and original bytes. Checkpoints are saved every five minutes and at normal completion. A canceled runner can leave in-flight rows, which the next collector safely retries. The original native catalogue pointer is retained separately. Source gaps and inaccessible historical files remain explicit; newer releases never substitute for missing originals.

The public workflow uses anonymous site access. `Preserve member-accessible Deadly Stream downloads` is a separate manually dispatched workflow that receives only the explicitly provisioned `DEADLYSTREAM_SESSION_COOKIES` secret. That secret may be defined at repository scope or at organization scope with access granted to this repository; a repository override takes priority. It contains only the site's login cookies, and does not include Google cookies or account passwords. The runner validates the signed-in user navigation before retrieving publicly linked gated resources and exact missing historical releases. It does not crawl private forums. Authenticated results use a separate encrypted report pointer so they cannot overwrite the public collector's live checkpoint.

Fresh official sitemaps are merged at the start of each subsequent public batch. Removed URLs remain in the preserved frontier. Native last-modified changes are compared with the retained original catalogue, and new or changed native entries retain all available versions and variants. A discovery refresh failure is recorded while the previously verified frontier remains available for capture.
