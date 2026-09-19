# Process containment and timeout evidence

Local Code Agent distinguishes **attempted cleanup** from **proven containment**.

The generic command runner starts a new POSIX session so ordinary descendants share a killable process group. On Windows it reconciles the visible `psutil` descendant tree. Both mechanisms are useful cleanup, but neither proves ownership of every descendant:

- a POSIX descendant can call `setsid()` or otherwise move out of the inherited process group;
- Windows descendant enumeration can race process creation or escape before enumeration completes.

Therefore a timeout must not report `process_cleanup_confirmed=true` merely because `killpg()` succeeded or a visible `psutil` tree was terminated. `True` is reserved for an implementation that owns the process tree with an operating-system containment primitive, such as a Linux cgroup or Windows Job Object. Until then timeout cleanup is fail-closed as unconfirmed.

Timeout output has a separate evidence rule. Child stdout/stderr is directed to private operating-system temporary files rather than pipes or files inside the public run directory. After the direct child exits or bounded timeout cleanup completes, the runner measures each capture's current byte length and copies exactly that snapshot into the public `stdout.log`, `stderr.log`, and `combined.log` artifacts. An escaped descendant may retain its inherited descriptor and continue writing to the private temporary object, but it cannot keep the controller waiting for pipe EOF, create a mutable capture file inside the run artifacts, or alter the public evidence after `run_command()` returns.

This rule is intentionally independent from eventual Session Hub cancellation semantics. A UI may request cancellation, but durable state must not claim cancellation or cleanup success unless the execution layer can prove it under the containment contract above.
