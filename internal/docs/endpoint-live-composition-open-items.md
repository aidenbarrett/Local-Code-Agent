# Endpoint composition open items

This slice intentionally leaves two things open. First, quarantine reconciliation is not yet connected to the Session Hub Stop action, because cancellation/cleanup is the later Stop slice and must not be faked here. Second, endpoint ownership remains process-local; an OS-backed cross-process lease is not claimed or introduced.

Those are explicit limits, not hidden TODOs. The merge claim for this slice is only that live Session Hub conversation and admitted worker calls no longer bypass the existing in-process queue/lease authority.
