"""Validate TTS requests and synthesize voice messages for Telegram delivery.

Used by the MCP ``send_voice_to_user`` tool intercept — the stream callback
validates each request via :func:`validate_voice_request` and performs TTS
synthesis via :func:`synthesize_voice`, collecting :class:`VoiceAttachment`
objects for later Telegram delivery.
"""

from dataclasses import dataclass
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

VALID_TTS_VOICES = {
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
}

MAX_VOICE_MESSAGES_PER_RESPONSE = 5
MAX_TEXT_LENGTH = 4096


@dataclass
class VoiceAttachment:
    """A synthesized voice message to send via Telegram."""

    audio_bytes: bytes
    text_preview: str  # First ~100 chars of source text, for logging
    voice: str


def validate_voice_request(
    text: str,
    voice: str = "nova",
    max_chars: int = MAX_TEXT_LENGTH,
) -> Optional[dict]:
    """Validate a TTS request from the MCP tool call.

    Returns a dict with validated parameters if valid, or None otherwise.
    """
    if not text or not text.strip():
        logger.debug("MCP voice request: empty text")
        return None

    if len(text) > max_chars:
        logger.debug(
            "MCP voice request: text too long", length=len(text), max=max_chars
        )
        return None

    voice_lower = voice.lower()
    if voice_lower not in VALID_TTS_VOICES:
        logger.debug("MCP voice request: invalid voice", voice=voice)
        return None

    return {"text": text.strip(), "voice": voice_lower}


async def synthesize_voice(
    text: str,
    voice: str,
    openai_client: Any,
    model: str = "gpt-4o-mini-tts",
    instructions: str = "",
) -> Optional[VoiceAttachment]:
    """Synthesize speech from text using the OpenAI TTS API.

    Returns a VoiceAttachment with the audio bytes, or None on failure.
    """
    try:
        kwargs: dict[str, Any] = {
            "model": model,
            "voice": voice,
            "input": text,
            "response_format": "opus",
        }
        if instructions:
            kwargs["instructions"] = instructions

        response = await openai_client.audio.speech.create(**kwargs)
        audio_bytes = response.content

        if not audio_bytes:
            logger.warning("TTS returned empty audio")
            return None

        return VoiceAttachment(
            audio_bytes=audio_bytes,
            text_preview=text[:100],
            voice=voice,
        )
    except Exception as exc:
        logger.warning(
            "TTS synthesis failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
