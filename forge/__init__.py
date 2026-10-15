"""Forge event kernel and phase-4 execution path.

Tool execution, child tasks, and preconfigured MCP calls go through Coordinator.
This package is not wired into Agent.chat.
"""

from forge.kernel import Kernel

__all__ = ["Kernel"]
