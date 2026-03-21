"""Web chat handler with SSE streaming.

Bridges HTTP requests to ClaudeIntegration.run_command(),
streaming tool usage and response text back via Server-Sent Events.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional

import structlog
from pydantic import BaseModel

from ..claude.facade import ClaudeIntegration
from ..claude.sdk_integration import ClaudeResponse, StreamUpdate
from ..config.settings import Settings
from ..security.rate_limiter import RateLimiter
from ..storage.facade import Storage
from .web_auth import SupabaseAuthValidator, SupabaseUser

logger = structlog.get_logger()

# Sentinel to signal end of stream
_DONE = object()


class ChatRequest(BaseModel):
    """Incoming chat message from the web client."""

    message: str
    session_id: Optional[str] = None
    working_directory: Optional[str] = None
    force_new: bool = False


@dataclass
class WebUserState:
    """Per-user state for web sessions (mirrors context.user_data in Telegram)."""

    claude_session_id: Optional[str] = None
    current_directory: Optional[Path] = None
    force_new_session: bool = False


class WebHandler:
    """Handles web chat requests, producing SSE event streams."""

    def __init__(
        self,
        claude_integration: ClaudeIntegration,
        settings: Settings,
        storage: Optional[Storage] = None,
        auth_validator: Optional[SupabaseAuthValidator] = None,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        self.claude_integration = claude_integration
        self.settings = settings
        self.storage = storage
        self.auth_validator = auth_validator
        self.rate_limiter = rate_limiter
        # Per-user state, keyed by derived integer user_id
        self._user_state: Dict[int, WebUserState] = {}
        # Per-user lock to serialize concurrent requests
        self._user_locks: Dict[int, asyncio.Lock] = {}

    def _get_user_state(self, user_id: int) -> WebUserState:
        if user_id not in self._user_state:
            self._user_state[user_id] = WebUserState()
        return self._user_state[user_id]

    def _get_user_lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    async def handle_chat(
        self,
        request: ChatRequest,
        user: SupabaseUser,
    ) -> AsyncGenerator[str, None]:
        """Process a chat message and yield SSE events.

        Mirrors the flow from orchestrator.agentic_text():
        1. Rate limit check
        2. Resolve working directory and session
        3. Call claude_integration.run_command() with stream callback
        4. Store interaction
        5. Yield final response
        """
        user_id = SupabaseAuthValidator.supabase_uid_to_user_id(user.id)
        lock = self._get_user_lock(user_id)

        if lock.locked():
            yield _sse_event("error", {
                "content": "A request is already in progress. Please wait."
            })
            return

        async with lock:
            async for event in self._run_chat(request, user, user_id):
                yield event

    async def _run_chat(
        self,
        request: ChatRequest,
        user: SupabaseUser,
        user_id: int,
    ) -> AsyncGenerator[str, None]:
        """Inner chat handler, runs under the per-user lock."""
        # Rate limit
        if self.rate_limiter:
            allowed, limit_message = await self.rate_limiter.check_rate_limit(
                user_id, 0.001
            )
            if not allowed:
                yield _sse_event("error", {"content": limit_message})
                return

        state = self._get_user_state(user_id)

        # Resolve working directory
        if request.working_directory:
            working_dir = Path(request.working_directory)
        elif state.current_directory:
            working_dir = state.current_directory
        else:
            working_dir = self.settings.approved_directory

        # Resolve session
        session_id = request.session_id or state.claude_session_id
        force_new = request.force_new or state.force_new_session

        # Queue for bridging stream callback → SSE generator
        queue: asyncio.Queue[Any] = asyncio.Queue()

        async def on_stream(update: StreamUpdate) -> None:
            """Stream callback — pushes events to the SSE queue."""
            if update.tool_calls:
                for tc in update.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = _summarize_tool_input(name, tc.get("input", {}))
                    await queue.put(("tool", {"name": name, "detail": detail}))

            if update.type == "assistant" and update.content:
                text = update.content.strip()
                if text:
                    first_line = text.split("\n", 1)[0][:120]
                    await queue.put(("reasoning", {"content": first_line}))

            if update.type == "stream_delta" and update.content:
                await queue.put(("delta", {"content": update.content}))

        # Run Claude in a background task
        result_holder: Dict[str, Any] = {}

        async def run_claude() -> None:
            try:
                response = await self.claude_integration.run_command(
                    prompt=request.message,
                    working_directory=working_dir,
                    user_id=user_id,
                    session_id=session_id,
                    on_stream=on_stream,
                    force_new=force_new,
                )
                result_holder["response"] = response
            except Exception as e:
                result_holder["error"] = str(e)
                logger.error("Web chat Claude error", error=str(e), user_id=user_id)
            finally:
                await queue.put(_DONE)

        task = asyncio.create_task(run_claude())

        # Yield SSE events from the queue
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                event_type, data = item
                yield _sse_event(event_type, data)
        except asyncio.CancelledError:
            task.cancel()
            raise

        # Wait for task to finish (should already be done)
        await task

        # Process result
        if "error" in result_holder:
            yield _sse_event("error", {"content": result_holder["error"]})
            return

        response: ClaudeResponse = result_holder["response"]

        # Update user state
        if force_new:
            state.force_new_session = False
        state.claude_session_id = response.session_id

        # Store interaction
        if self.storage:
            try:
                await self.storage.save_claude_interaction(
                    user_id=user_id,
                    session_id=response.session_id,
                    prompt=request.message,
                    response=response,
                )
            except Exception as e:
                logger.warning("Failed to store web interaction", error=str(e))

        # Final response event
        yield _sse_event("done", {
            "content": response.content,
            "session_id": response.session_id,
            "cost": response.cost,
            "duration_ms": response.duration_ms,
            "num_turns": response.num_turns,
            "tools_used": [t.get("name", "") for t in response.tools_used],
        })

    async def new_session(self, user: SupabaseUser) -> Dict[str, str]:
        """Clear the session for a user (equivalent to /new)."""
        user_id = SupabaseAuthValidator.supabase_uid_to_user_id(user.id)
        state = self._get_user_state(user_id)
        state.claude_session_id = None
        state.force_new_session = True
        return {"status": "ok"}

    async def get_sessions(self, user: SupabaseUser) -> list:
        """List sessions for a user."""
        user_id = SupabaseAuthValidator.supabase_uid_to_user_id(user.id)
        return await self.claude_integration.get_user_sessions(user_id)

    async def get_status(self, user: SupabaseUser) -> Dict[str, Any]:
        """Get current status for a user."""
        user_id = SupabaseAuthValidator.supabase_uid_to_user_id(user.id)
        state = self._get_user_state(user_id)
        return {
            "user": {
                "email": user.email,
                "name": user.name,
            },
            "session_id": state.claude_session_id,
            "working_directory": str(
                state.current_directory or self.settings.approved_directory
            ),
        }


def _sse_event(event_type: str, data: Dict[str, Any]) -> str:
    """Format a single SSE event."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """Produce a short summary of a tool call's input."""
    if tool_name in ("Read", "read_file"):
        return tool_input.get("file_path", "")
    if tool_name in ("Write", "Edit", "create_file", "edit_file"):
        return tool_input.get("file_path", "")
    if tool_name in ("Bash", "bash", "shell"):
        cmd = tool_input.get("command", "")
        return cmd[:120] if cmd else ""
    if tool_name in ("Glob",):
        return tool_input.get("pattern", "")
    if tool_name in ("Grep",):
        return tool_input.get("pattern", "")
    # Generic: show first key's value
    for v in tool_input.values():
        if isinstance(v, str):
            return v[:80]
    return ""
