"""
Simple OpenHands agent wrapper.

This module provides a thin client around the self-hosted OpenHands REST API
so the webhook service can start and poll conversations without importing the
full openhands library.

If you install `openhands-ai` and want a fully-custom agent class, see the
commented-out section at the bottom.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class ConversationResult:
    conversation_id: str
    status: str          # running | stopped | error
    last_message: str


# ── OpenHands API client ──────────────────────────────────────────────────────

class SimpleAgent:
    """Lightweight async client for the self-hosted OpenHands server."""

    def __init__(self, base_url: str, poll_interval: float = 5.0, timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.timeout = timeout

    # ── public ───────────────────────────────────────────────────────────────

    async def run(self, task: str) -> ConversationResult:
        """Start a conversation for *task* and wait until it finishes."""
        conversation_id = await self._start_conversation(task)
        logger.info("Started conversation %s", conversation_id)
        return await self._wait_for_completion(conversation_id)

    # ── private ──────────────────────────────────────────────────────────────

    async def _start_conversation(self, task: str) -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/api/conversations",
                json={"initial_message": task},
            )
            resp.raise_for_status()
            return resp.json()["conversation_id"]

    async def _wait_for_completion(self, conversation_id: str) -> ConversationResult:
        deadline = asyncio.get_event_loop().time() + self.timeout
        async with httpx.AsyncClient(timeout=30) as client:
            while asyncio.get_event_loop().time() < deadline:
                resp = await client.get(
                    f"{self.base_url}/api/conversations/{conversation_id}"
                )
                resp.raise_for_status()
                data = resp.json()
                status = data.get("status", "running")
                last_msg = self._extract_last_message(data)

                if status in ("stopped", "error"):
                    return ConversationResult(
                        conversation_id=conversation_id,
                        status=status,
                        last_message=last_msg,
                    )
                await asyncio.sleep(self.poll_interval)

        return ConversationResult(
            conversation_id=conversation_id,
            status="timeout",
            last_message="Task timed out.",
        )

    @staticmethod
    def _extract_last_message(data: dict) -> str:
        """Pull the last assistant message from a conversation response."""
        events: list = data.get("events", [])
        for event in reversed(events):
            if event.get("source") == "agent" and event.get("action") == "message":
                return event.get("args", {}).get("content", "")
        return ""


# ── Optional: fully-custom agent class (requires `pip install openhands-ai`) ─
#
# from openhands.controller.agent import Agent
# from openhands.core.config import AgentConfig
# from openhands.events.action import Action, MessageAction
#
# class MyCustomAgent(Agent):
#     VERSION = "1.0"
#
#     def step(self, state) -> Action:
#         last_user_msg = ""
#         for event in reversed(state.history):
#             if hasattr(event, "source") and event.source == "user":
#                 last_user_msg = getattr(event, "content", "")
#                 break
#         return MessageAction(content=f"Echo: {last_user_msg}")