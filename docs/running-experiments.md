# Run card: the 30B three-condition smoke

Package `08d5e0fe6f2be291`, commit `6b74c36e`, prompt `37f984092fed31ea`,
270 tests.

**Do not start until ChatGPT says GO.** The instrument is frozen. Anything that
changes it invalidates his sweep.

---

## Windows, and what runs where

**Window A: Windows PowerShell.** The model server. Leave it running for all
three conditions. Do not restart it between them: restarting empties the prefix
cache and the first condition after a restart pays a cost the others do not,
which shows up as a timing difference that is not a condition difference.

**Window B: WSL2 Ubuntu.** The agent, the fixture, the evaluator. Everything
below is typed here.

**Window C: nothing.** There is no Window C. If a document says there is, it is
out of date.

---

## Before the first run, once

Copy the two files across from Windows Downloads into your Linux home. Adjust
`<you>` to your Windows username.

```bash
cp /mnt/c/Users/<you>/Downloads/local-code-agent.zip ~/
cp /mnt/c/Users/<you>/Downloads/run_experiment.sh ~/
```

The launcher must be the one from this package. It checks itself against the
copy inside the zip and stops if they differ, because a drifted launcher would
report this package's hash while running a different experiment. If it stops
with STALE LAUNCHER, do exactly what it says.

Check the server is up and answering, from Window B:

```bash
curl -s http://127.0.0.1:8080/v1/models
```

If that returns nothing, the run will fail at gate 4 and tell you so. Nothing is
lost, fix the server and start again.

---

## The three runs

One sitting. This order. Do not restart the server between them.

```bash
CONDITION=control bash ~/run_experiment.sh ~/local-code-agent.zip nuc-llama-30b
CONDITION=narrow  bash ~/run_experiment.sh ~/local-code-agent.zip nuc-llama-30b
CONDITION=skill   bash ~/run_experiment.sh ~/local-code-agent.zip nuc-llama-30b
```

Roughly an hour for control, less for the other two. Control has no procedure,
so it takes more turns, which is the thing being measured. Call it two and a
half to three hours total.

Each run re-unpacks the package, rebuilds the venv and runs all 270 tests before
it goes near the model. That is the gate paying for itself three times. Leave it
alone.

The order is fixed and declared so that if there is thermal or server drift it is
readable in the data rather than mixed into the contrast. Each run does its own
`qualify` pass first, which warms the server identically every time, so cache
state is fair across the three without you doing anything.

---

## If a gate stops a run

The launcher stops at the first failing gate and prints which one. It also
prints this, and it means it:

> No run happened. Any probe or suite JSON already in ~/experiment-runs is from an
> EARLIER run. Do not send it as this run.

Send the console output. Send nothing from `~/experiment-runs`. A stale file sent as
a fresh result is worse than no result, because it looks like data.

---

## What to send back

Per condition:

```
~/experiment-runs/nuc-llama-30b-control.json
~/experiment-runs/nuc-llama-30b-control-transcripts/
~/experiment-runs/nuc-llama-30b-narrow.json
~/experiment-runs/nuc-llama-30b-narrow-transcripts/
~/experiment-runs/nuc-llama-30b-skill.json
~/experiment-runs/nuc-llama-30b-skill-transcripts/
~/experiment-runs/qualify-nuc-llama-30b.json
```

To get them somewhere you can attach them:

```bash
cp -r ~/experiment-runs /mnt/c/Users/<you>/Downloads/
```

Plus the console output of all three runs if you still have it.

---

## What happens then

I build the per-case paired table across the three conditions, the anomaly
ledger, and the one machinery number that matters most: **verification
disagreements**, which should be exactly zero now there is a single classifier.

Then the smoke gate decision, which is one of:

```
FIX INSTRUMENT
REDESIGN PILOT TASKS
BUILD SIX-CELL PILOT
```

**No thesis verdict.** The 8B is not in this data, so the small-model
interaction cannot be spoken about at all. Anyone who tries is wrong,
including me.

---

## What we are not doing

Not touching the evaluator. Not adding a check. Not tightening a rule. Not
fixing either of the two recorded limitations.

If something in the data looks wrong, it gets written down and argued with the
data in front of us. Patching the ruler after seeing the result is how you get
a number that means nothing.

---
