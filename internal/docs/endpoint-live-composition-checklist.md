# Endpoint composition acceptance checklist

- [x] Public conversation calls enter the canonical endpoint lease adapter.
- [x] Worker calls enter it only under admitted task UUID/execution-epoch authority.
- [x] Same physical base URL reuses one in-process authority.
- [x] Explicit split base URLs use distinct authorities.
- [x] Client exceptions quarantine rather than silently release.
- [x] Cross-process arbitration and inference cancellation remain explicitly unclaimed.
- [ ] Exact PR-head CI green on all required jobs.
- [ ] Physical Panther Lake run, deliberately deferred to the later hardware acceptance slice.
