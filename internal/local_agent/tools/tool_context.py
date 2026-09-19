"""Shared state handed to every tool implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import RepoConfig


@dataclass
class ToolContext:
    repo: RepoConfig

    @property
    def root(self) -> Path:
        return self.repo.root

    @property
    def run_root(self) -> Path:
        path = self.repo.run_path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def timeout(self) -> int:
        return self.repo.policy.command_timeout_seconds
