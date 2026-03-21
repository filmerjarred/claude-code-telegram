"""Web interface routes.

Adds chat, session management, and static file serving routes
to the FastAPI application.
"""

from pathlib import Path
from typing import Any, Dict, Optional

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from ..config.settings import Settings
from .web_auth import SupabaseAuthValidator, SupabaseUser
from .web_handler import ChatRequest, WebHandler

logger = structlog.get_logger()

_STATIC_DIR = Path(__file__).parent / "static"


def _make_get_user(auth_validator: SupabaseAuthValidator):
    """Create a FastAPI dependency that extracts and validates the Supabase user."""

    async def get_user(authorization: Optional[str] = Header(None)) -> SupabaseUser:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        token = authorization[7:]
        user = await auth_validator.validate_token(token)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return user

    return get_user


def add_web_routes(
    app: FastAPI,
    web_handler: WebHandler,
    settings: Settings,
    auth_validator: SupabaseAuthValidator,
) -> None:
    """Register web interface routes on the FastAPI app."""

    get_user = _make_get_user(auth_validator)

    @app.get("/web")
    async def serve_web_app() -> FileResponse:
        """Serve the web frontend."""
        index_path = _STATIC_DIR / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="Web interface not found")
        return FileResponse(index_path, media_type="text/html")

    @app.post("/web/chat")
    async def web_chat(
        request: ChatRequest,
        user: SupabaseUser = Depends(get_user),
    ) -> StreamingResponse:
        """SSE streaming chat endpoint."""
        return StreamingResponse(
            web_handler.handle_chat(request, user),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/web/new")
    async def web_new_session(
        user: SupabaseUser = Depends(get_user),
    ) -> Dict[str, str]:
        """Clear session (equivalent to /new command)."""
        return await web_handler.new_session(user)

    @app.get("/web/sessions")
    async def web_sessions(
        user: SupabaseUser = Depends(get_user),
    ) -> list:
        """List user's Claude sessions."""
        return await web_handler.get_sessions(user)

    @app.get("/web/status")
    async def web_status(
        user: SupabaseUser = Depends(get_user),
    ) -> Dict[str, Any]:
        """Get current user status and session info."""
        return await web_handler.get_status(user)

    logger.info("Web interface routes registered")
