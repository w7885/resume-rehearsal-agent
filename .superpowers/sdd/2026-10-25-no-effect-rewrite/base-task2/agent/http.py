"""Compatibility import for the integrated, provenance-checked Host routes.

The old text/messageId endpoint was never mounted by main. The integrated
router takes a saved userMessageId and is composed in app.main's lifespan.
"""
from app.agent_host import RunInput as RunRequest, create_agent_router

__all__ = ['RunRequest', 'create_agent_router']
