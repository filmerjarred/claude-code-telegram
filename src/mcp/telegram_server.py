"""MCP server exposing Telegram-specific tools to Claude.

Runs as a stdio transport server. The ``send_image_to_user`` tool validates
file existence and extension, then returns a success string. Actual Telegram
delivery is handled by the bot's stream callback which intercepts the tool
call.
"""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

mcp = FastMCP("telegram")


@mcp.tool()
async def send_image_to_user(file_path: str, caption: str = "") -> str:
    """Send an image file to the Telegram user.

    Args:
        file_path: Absolute path to the image file.
        caption: Optional caption to display with the image.

    Returns:
        Confirmation string when the image is queued for delivery.
    """
    path = Path(file_path)

    if not path.is_absolute():
        return f"Error: path must be absolute, got '{file_path}'"

    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return (
            f"Error: unsupported image extension '{path.suffix}'. "
            f"Supported: {', '.join(sorted(IMAGE_EXTENSIONS))}"
        )

    if not path.is_file():
        return f"Error: file not found: {file_path}"

    return f"Image queued for delivery: {path.name}"


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


@mcp.tool()
async def send_voice_to_user(
    text: str,
    voice: str = "nova",
    instructions: str = "",
) -> str:
    """Generate speech from text and send as a voice message to the Telegram user.

    Args:
        text: The text to convert to speech. Must be non-empty, at most 4096 chars.
        voice: TTS voice name (alloy, ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer).
        instructions: Optional instructions for speech style/tone.

    Returns:
        Confirmation string when the voice message is queued for delivery.
    """
    if not text or not text.strip():
        return "Error: text must be non-empty"

    if len(text) > 4096:
        return f"Error: text too long ({len(text)} chars). Maximum is 4096 characters."

    voice_lower = voice.lower()
    if voice_lower not in VALID_TTS_VOICES:
        return (
            f"Error: unsupported voice '{voice}'. "
            f"Supported: {', '.join(sorted(VALID_TTS_VOICES))}"
        )

    return f"Voice message queued for delivery ({len(text)} chars, voice={voice_lower})"


if __name__ == "__main__":
    mcp.run(transport="stdio")
