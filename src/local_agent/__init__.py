"""Local C++ engineering agent.

Design rule for this package: the orchestrator owns the workflow, the policy
engine owns what may execute, the build system and tests own verification.
The language model supplies judgement only where judgement is useful.
"""

__version__ = "0.1.0"
