# Natural file requests: authority and repository boundary

Natural create/add/edit/modify/update requests use a bounded deterministic grammar.
The verb must be followed by a concrete target, optionally introduced by words such
as `a C++ file called`. A target contains an extension or a path separator. Ordinary
chat such as `update me on the build` does not gain mutation authority. Unsupported
phrasing remains model fallback; the model's proposal still requires acceptance.

`session/change_requests.py` owns this grammar and the request checks. `intents.py`
uses it for classification. `ConversationGateway` supplies the controller's declared
repository root and checks destinations before either a model proposal or task
admission. It also checks the original canonical user turn when accepting or resuming
a durable model proposal. Model reformulations cannot replace that request.

## Ordering

1. Explicit conversation mode grants no work authority.
2. Unsupported push, commit, Git and deletion clauses anywhere in a direct file-change
   request take precedence over source-change routing. A filename such as `git.cpp`
   is not a Git operation. Explicit `change:` retains its existing source-deletion
   meaning; `/commit <task-id>` retains its separate candidate authority.
3. Every path-shaped token in the direct request is checked, including a later
   `Save this file here:` destination. `resolve_in_repo` remains the containment owner:
   it resolves traversal and symlinks before comparing against the repository root.
   Foreign Windows drives/UNC names, drive-relative paths and expansion syntax are
   refused rather than reinterpreted as local filenames.
4. An external or unresolvable destination stops the whole request, with no model
   call or task. The answer names the active repository. The product never silently
   substitutes a repo-local destination for Downloads.
5. A valid natural request selects the existing isolated `implement-change` journey.
   Original user text is preserved. This grants no checkout, staging or Git-history
   authority; tool sandboxing and candidate verification still govern every effect.

This is deliberately not general natural-language path extraction. All path-shaped
tokens are conservatively checked, so an external path mentioned only as an example
can also cause refusal. Extensionless filenames can use a relative path such as
`./Makefile`; arbitrary prose is not promoted to a filename to improve recall.

Pending proposals rejected by the new boundary remain unaccepted and can be dismissed
with `chat`. A proposal already accepted by an older version is checked again before
recovery; if now refused, it cannot resume and the user is told to start a new session.
Its immutable resolution is not rewritten and no task is silently replayed.

## Regression evidence

`test_session_intents.py` covers anchored authority and zero/one/multiple pending
proposal selection. `test_session_file_change_boundary.py` exercises real gateway
dispatch with a model that fails if called: ordinary prose, compound unsupported
operations, traversal, Downloads, Windows/UNC paths, symlink escapes, contained
absolute paths, spaces and filenames that resemble operation keywords.
`test_session_gateway_route_acceptance.py` checks durable old-proposal acceptance
and recovery against the canonical user turn.
