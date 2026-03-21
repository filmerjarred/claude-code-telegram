"""FastAPI webhook server.

Runs in the same process as the bot, sharing the event loop.
Receives external webhooks and publishes them as events on the bus.
"""

import uuid
from typing import Any, Dict, Optional

import structlog
from fastapi import FastAPI, Header, HTTPException, Request

from ..config.settings import Settings
from ..events.bus import EventBus
from ..events.types import WebhookEvent
from ..security.rate_limiter import RateLimiter
from ..storage.database import DatabaseManager
from ..storage.facade import Storage
from .auth import verify_github_signature, verify_shared_secret
from .tts import TTSContextRegistry, add_tts_routes
from .web_auth import SupabaseAuthValidator

logger = structlog.get_logger()


def create_api_app(
    event_bus: EventBus,
    settings: Settings,
    db_manager: Optional[DatabaseManager] = None,
    tts_registry: Optional[TTSContextRegistry] = None,
    claude_integration: Optional[Any] = None,
    auth_validator: Optional[SupabaseAuthValidator] = None,
    storage: Optional[Storage] = None,
    rate_limiter: Optional[RateLimiter] = None,
) -> FastAPI:
    """Create the FastAPI application."""

    app = FastAPI(
        title="Claude Code Telegram - Webhook API",
        version="0.1.0",
        docs_url="/docs" if settings.development_mode else None,
        redoc_url=None,
    )

    @app.get("/health")
    async def health_check() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/{provider}")
    async def receive_webhook(
        provider: str,
        request: Request,
        x_hub_signature_256: Optional[str] = Header(None),
        x_github_event: Optional[str] = Header(None),
        x_github_delivery: Optional[str] = Header(None),
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, str]:
        """Receive and validate webhook from an external provider."""
        body = await request.body()

        # Verify signature based on provider
        if provider == "github":
            secret = settings.github_webhook_secret
            if not secret:
                raise HTTPException(
                    status_code=500,
                    detail="GitHub webhook secret not configured",
                )
            if not verify_github_signature(body, x_hub_signature_256, secret):
                logger.warning(
                    "GitHub webhook signature verification failed",
                    delivery_id=x_github_delivery,
                )
                raise HTTPException(status_code=401, detail="Invalid signature")

            event_type_name = x_github_event or "unknown"
            delivery_id = x_github_delivery or str(uuid.uuid4())
        else:
            # Generic provider — require auth (fail-closed)
            secret = settings.webhook_api_secret
            if not secret:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Webhook API secret not configured. "
                        "Set WEBHOOK_API_SECRET to accept "
                        "webhooks from this provider."
                    ),
                )
            if not verify_shared_secret(authorization, secret):
                raise HTTPException(status_code=401, detail="Invalid authorization")
            event_type_name = request.headers.get("X-Event-Type", "unknown")
            delivery_id = request.headers.get("X-Delivery-ID", str(uuid.uuid4()))

        # Parse JSON payload
        try:
            payload: Dict[str, Any] = await request.json()
        except Exception:
            payload = {"raw_body": body.decode("utf-8", errors="replace")[:5000]}

        # Atomic dedupe: attempt INSERT first, only publish if new
        if db_manager and delivery_id:
            is_new = await _try_record_webhook(
                db_manager,
                event_id=str(uuid.uuid4()),
                provider=provider,
                event_type=event_type_name,
                delivery_id=delivery_id,
                payload=payload,
            )
            if not is_new:
                logger.info(
                    "Duplicate webhook delivery ignored",
                    provider=provider,
                    delivery_id=delivery_id,
                )
                return {
                    "status": "duplicate",
                    "delivery_id": delivery_id,
                }

        # Publish event to the bus
        event = WebhookEvent(
            provider=provider,
            event_type_name=event_type_name,
            payload=payload,
            delivery_id=delivery_id,
        )

        await event_bus.publish(event)

        logger.info(
            "Webhook received and published",
            provider=provider,
            event_type=event_type_name,
            delivery_id=delivery_id,
            event_id=event.id,
        )

        return {"status": "accepted", "event_id": event.id}

    # TTS endpoint (optional)
    if tts_registry is not None and settings.enable_tts:
        add_tts_routes(app, tts_registry, settings)

    # Web interface (if Supabase auth is configured)
    if auth_validator is not None and claude_integration is not None:
        from starlette.middleware.cors import CORSMiddleware

        from .web_handler import WebHandler
        from .web_routes import add_web_routes

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )

        web_handler = WebHandler(
            claude_integration=claude_integration,
            settings=settings,
            storage=storage,
            auth_validator=auth_validator,
            rate_limiter=rate_limiter,
        )
        add_web_routes(app, web_handler, settings, auth_validator)
        logger.info("Web interface enabled")

    # Test endpoint — only in development mode
    if settings.development_mode and claude_integration is not None:
        _add_test_message_route(app, claude_integration, settings, tts_registry)

    return app


def _add_test_message_route(
    app: FastAPI,
    claude_integration: Any,
    settings: "Settings",
    tts_registry: Optional[TTSContextRegistry],
) -> None:
    """Add POST /test/message — simulate a user message without Telegram."""
    from pydantic import BaseModel as _BM

    from ..bot.utils.voice_synthesizer import (
        MAX_VOICE_MESSAGES_PER_RESPONSE,
        VoiceAttachment,
        synthesize_voice,
        validate_voice_request,
    )
    from ..claude.sdk_integration import StreamUpdate

    class _TestReq(_BM):
        text: str
        user_id: int = 0

    @app.post("/test/message")
    async def test_message(req: _TestReq) -> Dict[str, Any]:
        user_id = req.user_id or (
            settings.allowed_users[0] if settings.allowed_users else 1
        )
        voices: list[VoiceAttachment] = []
        tool_log: list[Dict[str, Any]] = []

        async def _on_stream(update_obj: StreamUpdate) -> None:
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    tool_log.append({"name": tc_name, "input": tc.get("input", {})})
                    if tc_name == "send_voice_to_user" or tc_name.endswith(
                        "__send_voice_to_user"
                    ):
                        logger.info(
                            "TEST: TTS tool call intercepted",
                            tool_name=tc_name,
                            text_length=len(tc.get("input", {}).get("text", "")),
                        )
                        if len(voices) < MAX_VOICE_MESSAGES_PER_RESPONSE:
                            tc_input = tc.get("input", {})
                            validated = validate_voice_request(
                                tc_input.get("text", ""),
                                tc_input.get("voice", settings.tts_voice),
                                max_chars=settings.tts_max_chars,
                            )
                            if validated:
                                try:
                                    from openai import AsyncOpenAI

                                    client = AsyncOpenAI(
                                        api_key=settings.openai_api_key_str
                                    )
                                    attachment = await synthesize_voice(
                                        text=validated["text"],
                                        voice=validated["voice"],
                                        openai_client=client,
                                        model=settings.tts_model,
                                        instructions=tc_input.get("instructions", ""),
                                    )
                                    if attachment:
                                        voices.append(attachment)
                                        logger.info(
                                            "TEST: TTS synthesized",
                                            voice=validated["voice"],
                                            bytes=len(attachment.audio_bytes),
                                        )
                                except Exception as e:
                                    logger.error("TEST: TTS failed", error=str(e))
            if update_obj.type == "assistant" and update_obj.content:
                logger.info(
                    "TEST: assistant text", text=update_obj.content[:200]
                )

        # Build extra system prompt with TTS curl template (matches real path)
        extra_system_prompt = None
        if tts_registry and settings.enable_tts:
            token = tts_registry.create_token(
                bot=None,  # no real bot in test mode
                chat_id=0,
                thread_id=None,
                reply_to_id=None,
            )
            port = settings.api_server_port
            extra_system_prompt = (
                "[TTS]\n"
                "To send a voice message to the user, run this curl command via Bash:\n"
                f"curl -s -X POST http://127.0.0.1:{port}/tts "
                f'-H "Content-Type: application/json" '
                f"-d '{{\"token\":\"{token}\",\"text\":\"<TEXT>\","
                f"\"voice\":\"<VOICE>\",\"instructions\":\"<INSTRUCTIONS>\"}}'\n"
                f"Available voices: alloy, ash, ballad, coral, echo, fable, "
                f"nova, onyx, sage, shimmer. Default: {settings.tts_voice}. "
                f"Max text length: {settings.tts_max_chars} chars. "
                f"Max {MAX_VOICE_MESSAGES_PER_RESPONSE} voice messages per response.\n"
                "Use the /speak skill when the user asks you to speak or read aloud."
            )

        logger.info("TEST: running command", text=req.text, user_id=user_id)

        response = await claude_integration.run_command(
            prompt=req.text,
            working_directory=settings.approved_directory,
            user_id=user_id,
            session_id=None,
            on_stream=_on_stream,
            force_new=True,
            extra_system_prompt=extra_system_prompt,
        )

        logger.info(
            "TEST: response received",
            content_length=len(response.content),
            session_id=response.session_id,
            tool_calls=len(tool_log),
            voices=len(voices),
        )

        return {
            "response": response.content,
            "session_id": response.session_id,
            "tool_calls": tool_log,
            "voices_synthesized": len(voices),
            "voice_details": [
                {"text_preview": v.text_preview, "voice": v.voice, "bytes": len(v.audio_bytes)}
                for v in voices
            ],
        }


async def _try_record_webhook(
    db_manager: DatabaseManager,
    event_id: str,
    provider: str,
    event_type: str,
    delivery_id: str,
    payload: Dict[str, Any],
) -> bool:
    """Atomically insert a webhook event, returning whether it was new.

    Uses INSERT OR IGNORE on the unique delivery_id column.
    If the row already exists the insert is a no-op and changes() == 0.
    Returns True if the event is new (inserted), False if duplicate.
    """
    import json

    async with db_manager.get_connection() as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO webhook_events
            (event_id, provider, event_type, delivery_id, payload,
             processed)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (
                event_id,
                provider,
                event_type,
                delivery_id,
                json.dumps(payload),
            ),
        )
        cursor = await conn.execute("SELECT changes()")
        row = await cursor.fetchone()
        inserted = row[0] > 0 if row else False
        await conn.commit()
        return inserted


async def run_api_server(
    event_bus: EventBus,
    settings: Settings,
    db_manager: Optional[DatabaseManager] = None,
    tts_registry: Optional[TTSContextRegistry] = None,
    claude_integration: Optional[Any] = None,
    auth_validator: Optional[SupabaseAuthValidator] = None,
    storage: Optional[Storage] = None,
    rate_limiter: Optional[RateLimiter] = None,
) -> None:
    """Run the FastAPI server using uvicorn."""
    import uvicorn

    app = create_api_app(
        event_bus,
        settings,
        db_manager,
        tts_registry=tts_registry,
        claude_integration=claude_integration,
        auth_validator=auth_validator,
        storage=storage,
        rate_limiter=rate_limiter,
    )

    host = settings.api_server_host
    config = uvicorn.Config(
        app=app,
        host=host,
        port=settings.api_server_port,
        log_level="info" if not settings.debug else "debug",
    )
    server = uvicorn.Server(config)
    await server.serve()
