"""TTS HTTP endpoint — synthesize speech and deliver via Telegram.

Claude calls ``POST /tts`` with a short-lived token.  The endpoint validates
the token, synthesizes audio via OpenAI TTS, and sends the voice message
directly to the originating Telegram chat.
"""

import io
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..bot.utils.voice_synthesizer import (
    MAX_VOICE_MESSAGES_PER_RESPONSE,
    synthesize_voice,
    validate_voice_request,
)
from ..config.settings import Settings

logger = structlog.get_logger()

_TOKEN_TTL_SECONDS = 600  # 10 minutes


@dataclass
class TTSContext:
    """Telegram delivery context stashed when the token is created."""

    bot: Any  # telegram.Bot
    chat_id: int
    thread_id: Optional[int]
    reply_to_id: Optional[int]
    created_at: float = field(default_factory=time.time)
    use_count: int = 0


class TTSContextRegistry:
    """In-memory store mapping opaque tokens to Telegram delivery context."""

    def __init__(self) -> None:
        self._store: Dict[str, TTSContext] = {}

    def create_token(
        self,
        bot: Any,
        chat_id: int,
        thread_id: Optional[int] = None,
        reply_to_id: Optional[int] = None,
    ) -> str:
        """Generate a token and stash the Telegram context."""
        self._purge_expired()
        token = secrets.token_urlsafe(24)
        self._store[token] = TTSContext(
            bot=bot,
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to_id=reply_to_id,
        )
        return token

    def consume(self, token: str) -> Optional[TTSContext]:
        """Validate and consume one use of *token*.

        Returns the context if valid, or ``None`` if the token is unknown,
        expired, or has exceeded its use limit.
        """
        ctx = self._store.get(token)
        if ctx is None:
            return None
        if time.time() - ctx.created_at > _TOKEN_TTL_SECONDS:
            del self._store[token]
            return None
        if ctx.use_count >= MAX_VOICE_MESSAGES_PER_RESPONSE:
            return None
        ctx.use_count += 1
        return ctx

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [
            t
            for t, ctx in self._store.items()
            if now - ctx.created_at > _TOKEN_TTL_SECONDS
        ]
        for t in expired:
            del self._store[t]


class TTSRequest(BaseModel):
    """POST /tts request body."""

    token: str
    text: str
    voice: str = Field(default="nova")
    instructions: str = Field(default="")


def add_tts_routes(
    app: FastAPI,
    registry: TTSContextRegistry,
    settings: Settings,
) -> None:
    """Register the ``POST /tts`` endpoint on *app*."""

    @app.post("/tts")
    async def tts_endpoint(req: TTSRequest) -> Dict[str, Any]:
        ctx = registry.consume(req.token)
        if ctx is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        validated = validate_voice_request(
            req.text, req.voice, max_chars=settings.tts_max_chars
        )
        if validated is None:
            raise HTTPException(
                status_code=400,
                detail="Invalid TTS request (empty text, text too long, or bad voice)",
            )

        # Build OpenAI client (lazy import — optional dependency)
        try:
            from openai import AsyncOpenAI
        except ModuleNotFoundError:
            raise HTTPException(
                status_code=501,
                detail="TTS unavailable: 'openai' package not installed. "
                "Install with: pip install 'claude-code-telegram[voice]'",
            )

        api_key = settings.openai_api_key_str
        if not api_key:
            raise HTTPException(
                status_code=501,
                detail="TTS unavailable: OPENAI_API_KEY not set",
            )

        openai_client = AsyncOpenAI(api_key=api_key)

        attachment = await synthesize_voice(
            text=validated["text"],
            voice=validated["voice"],
            openai_client=openai_client,
            model=settings.tts_model,
            instructions=req.instructions,
        )
        if attachment is None:
            raise HTTPException(status_code=502, detail="TTS synthesis failed")

        # Deliver to Telegram (skip if bot is None — test mode)
        if ctx.bot is not None:
            audio_file = io.BytesIO(attachment.audio_bytes)
            audio_file.name = "voice.opus"

            await ctx.bot.send_voice(
                chat_id=ctx.chat_id,
                voice=audio_file,
                message_thread_id=ctx.thread_id,
                reply_to_message_id=ctx.reply_to_id,
            )

        logger.info(
            "TTS delivered via HTTP endpoint",
            chat_id=ctx.chat_id,
            text_length=len(validated["text"]),
            voice=validated["voice"],
            test_mode=ctx.bot is None,
        )

        return {
            "status": "sent",
            "text_length": len(validated["text"]),
            "voice": validated["voice"],
        }
