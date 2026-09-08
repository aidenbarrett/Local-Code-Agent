# cpp-sandbox

A deliberately breakable C++ repository for exercising the agent. Nothing here
is real work and nothing here is anybody's intellectual property.

    python scripts/apply_scenario.py --list
    python scripts/apply_scenario.py clean
    python scripts/apply_scenario.py test_failure

Each scenario overwrites a small number of files with a broken variant. `clean`
restores the baseline.
