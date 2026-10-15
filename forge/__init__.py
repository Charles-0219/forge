"""Forge local API.

The HTTP service, CLI client, and console call the kernel. Agent.chat is unchanged.
"""

from forge.kernel import Kernel

__all__ = ["Kernel"]
