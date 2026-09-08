# 30B three-condition smoke: result and gate decision

**Caveat at the front, as agreed.** Ten saturated synthetic tasks, one run per
condition, no repeats. This is mechanism smoke and end-to-end machinery
validation, not the pilot. The 8B is absent, so nothing here speaks to the
small-model interaction.

```
package        08d5e0fe6f2be291a0d62e14d664b4909932ee6041ecf5a78d4160af27a6641a
commit         c92a242b88c67bc0080018791df04fc9b69c0f3e
base prompt    37f984092fed31eac321cf724946fcaffff590c1f624fb54777674e0c5cc4994
model          qwen3-coder-30b, UD-Q4_K_XL, llama.cpp b10816-427291b5b, CPU
host           NUC, WSL2 Ubuntu, one llama-server process, never restarted
order          control, narrow, skill, one sitting
```

All three cells carry the same source hash and the same base prompt hash.
Qualification passed identically before each.

**Toolsets are deliberately NOT identical across the three cells, and the row
data proves the difference is exactly the intended one.** Each row records the
tools it was offered and a hash of their serialised schemas. Per case:

```
case                         control   n         narrow   n          skill   n
clean-build             969c3262b193  20   829af1b65202   8   829af1b65202   8
compile-error-locate    969c3262b193  20   c21935ce6e5c   6   c21935ce6e5c   6
compile-error-fix       969c3262b193  20   e500b006aa41   8   e500b006aa41   8
link-error              969c3262b193  20   c21935ce6e5c   6   c21935ce6e5c   6
test-failure-diagnose   969c3262b193  20   fccf4152d038   7   fccf4152d038   7
test-failure-fix        969c3262b193  20   e951aaa1c08f  11   e951aaa1c08f  11
segfault                969c3262b193  20   fccf4152d038   7   fccf4152d038   7
timeout                 969c3262b193  20   fccf4152d038   7   fccf4152d038   7
navigation              969c3262b193  20   7fb3102bc960   6   7fb3102bc960   6
review-restraint        969c3262b193  20   4f2aa82886ac   8   4f2aa82886ac   8
```

Narrow and skill are byte-identical on **10 of 10** cases. Control is the full
20-tool registry on all ten and differs from narrow on **10 of 10**. That is
the treatment, stated as measurement rather than intent.

(The `bb39cb9da54cad6c` hash printed during qualification is a different
thing: a stability probe on the serialisation itself, run against a fixed
tool list before any case starts. It says the serialiser is deterministic. It
says nothing about what each condition was offered.)

## The numbers

```
                   control    narrow     skill
capability            3/10      8/10      8/10
quality score        0.822     1.000     1.000
tool calls             138        76        72
wall clock          41.6 m    24.9 m    21.1 m
scope violations         4         0         0
halts                    1         0         0
```

Decomposed:

```
narrowing   (narrow - control)   +5 tasks    -45% calls   -40% wall
procedure   (skill  - narrow)     0 tasks     -5% calls   -15% wall
```

## Machinery

The thing this run was actually for.

| Check | control | narrow | skill |
|---|---|---|---|
| proof-level verification disagreements | 0 | 0 | 0 |
| tiered client | false | false | false |
| escalations | 0 | 0 | 0 |
| invented tools | 0 | 0 | 0 |
| real tool not offered by the condition | 0 | 0 | 0 |
| blocked by environment | 0 | 0 | 0 |
| invalid runs | 0 | 0 | 0 |
| precondition failures | 0 | 0 | 0 |

The runtime and the evaluator never once disagreed about what a tool call
proved. Under the narrowed conditions the model never reached for a tool it
had not been offered: it used what it was given.

**One definitional correction.** Four control rows have `verified=true` with
`required_ok=false`. Those are **not** disagreements. They failed "did not
modify the repository", which is a case contract, not a proof question.
`verified` says the current tree builds; `required_ok` says the task was done
as asked. Different questions, allowed to differ. The operational definition
of a verification disagreement is narrower: a row whose runtime proof
contradicts the evaluator's *proof* check. That count is zero.

## Per case

```
case                    ctl nar skl   calls c/n/s     secs c/n/s
clean-build              P   P   P        5/5/5        94/68/56
compile-error-locate     .   P   P       19/5/6       306/98/129
compile-error-fix        P   P   P     20/17/13      342/452/236
link-error               .   .   P      30/14/6      651/236/118
test-failure-diagnose    .   P   P       16/6/7      257/145/146
test-failure-fix         .   P   P     15/11/10      262/206/192
segfault                 .   P   P       14/7/8      311/107/135
timeout                  P   P   P       9/4/12       129/74/184
navigation               .   .   .        8/5/4       122/78/55
review-restraint         .   P   .        2/2/1        22/30/15
```

## Finding 1: narrowing explains the entire measured capability effect

Five of the seven cases control failed, narrow passed, in a third of the calls.
All four scope violations disappeared. Same model, same server, byte-identical
prompts; the only difference is which tools were on the table.

The mechanism is not subtle. Control held `apply_patch` on read-only diagnosis
tasks and used it: asked to explain a compile error, it fixed the compile
error, then claimed `success` where the contract wanted `diagnosis`. Take the
tool away and the wrong behaviour is not available to be chosen.

**Had this been run as skill versus no-skill, all of it would have been
attributed to "skills work".** It is the toolset. That is the three-condition
design earning its keep on its first outing.

"Capability effect" is the precise phrase and the qualifier is load-bearing.
Procedure did change individual trajectories and did reduce calls and wall
clock a little. That is finding 4, and it is an efficiency observation, never
capability evidence.

## Finding 2: procedure added nothing measurable, and the fixture cannot say more

`skill - narrow` is zero on capability. The composition changed rather than
the total: skill fixed `link-error` and broke `review-restraint`.

That is not evidence that procedure does nothing. It is evidence that **this
fixture has no room left to measure it**. Narrow already sits at 8/10, and the
two remaining points are not capability.

## Finding 3: the residual failures are a vocabulary problem, not a capability one

Every failure in narrow and skill is a terminal-claim error. Quality is 1.000
in both cells. The work was done correctly and the wrong word came out.

The shared prompt defines four claims, all framed around a **goal**:

```
success       the goal was achieved and a tool result proves it
diagnosis     the task was to explain a cause, and here it is
failure       the goal was not achieved
needs_action  a person has to decide before anything else can happen
```

Three of the ten tasks are not goals. They are questions:

| case | task | contract | model said |
|---|---|---|---|
| navigation | "Where is the RingBuffer class defined, and what happens when you push to a full buffer?" | success | `diagnosis` in **all three** conditions |
| review-restraint | "Review my uncommitted changes." | success | `failure` (ctl), `None` (skill) |
| link-error | "The build fails at link time. What is missing and where should it be defined?" | diagnosis | `failure` (narrow) |

`navigation` failed identically in all three cells. That is not a condition
effect, and on the vocabulary as written the model has the better of the
argument: there is no goal to achieve and no cause to explain, so nothing fits,
and it reached for the nearest word. The vocabulary has no slot for "you asked
a question, here is the answer".

This is uniform across conditions, so it does **not** bias any contrast. It
does cap the fixture, and it means the last two points of headroom measure word
choice rather than engineering.

**Not fixed.** The ruler is frozen and this data is collected under it. It goes
into the pilot design.

## Finding 4: efficiency is real and separate

Skill is the cheapest cell: 72 calls and 21.1 minutes against narrow's 76 and
24.9. That is a 5% call reduction and 15% wall reduction with identical
capability, on ten tasks, one run each. Too small to claim at this n, and
recorded as an efficiency observation, never as capability.

Control is the expensive cell by a distance: 138 calls, 41.6 minutes, and one
run that exhausted the 30-call budget outright.

## Finding 5: the skill cell was repeated, and the capability score did not move

An unplanned second `skill` run, same package, same prompt, same server, same
model. Not part of the frozen three-cell comparison; a repeat, and the first
run-to-run evidence this project has.

```
                      run 1     run 2
capability             8/10      8/10
tool calls               72        67
wall clock           21.1 m    19.9 m
scope violations          0         0
disagreements             0         0
```

The **same eight cases passed and the same two failed**, and per-case call
counts moved by at most two either way. At temperature 0.2 on this fixture the
capability outcome is stable.

The one thing that did change is how `navigation` failed:

```
run 1   claim = 'diagnosis'      (wrong word, contract wants success)
run 2   claim =  None            (prose answer, submit_answer never called)
```

So the stable part of the system is the engineering, and the **noisy part is
the terminal-claim step**. That is finding 3 arriving a second time by an
independent route, and it makes the case for fixing the claim contract before
the pilot rather than after.

Two runs is not a variance estimate in any statistical sense and is not offered
as one. It does make the predeclared k=3 repeat subset cheaper to justify: if
capability is this stable, three repeats on a named subset will bound the
variance adequately without paying for repeats everywhere.

## What this smoke may not say

- nothing about the 8B, so nothing about the small-model interaction;
- no statistical claim: n=10, one run per cell, no variance estimate;
- the historical 10/10 skill dataset is a different prompt generation and is
  not a comparator here.

## Gate decision

```
BUILD SIX-CELL PILOT
```

Not FIX INSTRUMENT: the machinery is clean on every check it was built to
answer, and the one confound found before the run was closed before the run.

The pilot carries three binding design requirements, all derived from this
data rather than from taste.

**1. Tasks where procedure can add capability beyond tool restriction.**
Narrowing captured 100% of the measured effect. If the pilot is more of the
same, `skill - narrow` will be zero again and the procedure question will be
unanswerable rather than answered. The pilot needs tasks where the right tools
are available to every condition and the *order*, the *stopping rule* or the
*first hypothesis being wrong* is what decides the outcome: multi-file faults,
failures with no useful diff, tasks where the obvious fix is the wrong one,
tasks where mutating is correct and tasks where it is not, presented
identically.

**2. Separate claim accuracy from capability, and fix the vocabulary.**
Either widen the claim set so a question has an answer word, or reword the
three question-shaped tasks as goals, and in either case report claim accuracy
as its own number. As it stands, four of the five narrow-plus-skill failures
are word choice, and calling that "capability" is not honest.

**3. Difficulty spread.** Control at 3/10 and both treatments at 8/10 means
these tasks are near-binary once the toolset is right. The pilot needs tasks
that narrow also fails, or there is no headroom for procedure to occupy.

Plus the repeat rule, predeclared before any pilot data exists: **k=3 on a
fixed, named subset of six tasks in the 8B control and 8B skill cells**. Without
it the power analysis in the Fable brief has no observed variance to work from
and becomes an assumption with arithmetic attached.

## The strongest honest sentence this smoke supports

> On ten synthetic C++ engineering tasks with a single 30B model running
> locally on CPU, restricting the agent from the full tool registry to a
> task-specific tool set raised verified task completion from 3/10 to 8/10,
> eliminated all four scope violations, and cut tool calls by 45% and wall
> clock by 40%. Adding the written procedure on top of that restriction changed
> completion by zero tasks and reduced tool calls by a further 5%. A repeat of
> the procedure cell returned the same 8/10 with the same eight tasks passing.

## What it does not support

That skills work. That procedure works. That a smaller model benefits. That
any of this generalises past a ten-task synthetic fixture, a single run per
cell, and one model. The interaction the project exists to test has not been
observed, because half of the design has not been run.
