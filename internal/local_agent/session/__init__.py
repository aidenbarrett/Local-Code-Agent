"""Product session package.

Submodules own their dependencies. Importing the package itself must stay cheap:
direct chat imports ``conversation_store`` through this package and must not
implicitly import the gateway/event-contract stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .conversation_gateway import ConversationGateway

__all__ = ["ConversationGateway"]


def __getattr__(name: str) -> Any:
    """Preserve the public gateway import without eagerly loading its stack."""
    if name == "ConversationGateway":
        from .conversation_gateway import ConversationGateway

        return ConversationGateway
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
