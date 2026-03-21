---
name: speak
description: Send a voice message to the user. Use when the user asks you to speak, read aloud, narrate, or say something out loud. Requires the [TTS] section in your system prompt.
allowed-tools: Bash
---

Send a voice message to the user via the TTS HTTP endpoint.

## Prerequisites

Look for a `[TTS]` section in your system prompt. It contains a `curl` command template with a pre-authorized token. If there is no `[TTS]` section, tell the user that TTS is not enabled.

## How to use

Use the Bash tool to run the `curl` command from the `[TTS]` section, replacing:
- `<TEXT>` with the text to speak (escape double quotes and backslashes for JSON)
- `<VOICE>` with the desired voice (default: nova)
- `<INSTRUCTIONS>` with optional style instructions (e.g. "speak cheerfully", "whisper")

Available voices: alloy, ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer.

## Example

```bash
curl -s -X POST http://127.0.0.1:8080/tts \
  -H "Content-Type: application/json" \
  -d '{"token":"<from system prompt>","text":"Hello! How are you today?","voice":"nova","instructions":""}'
```

The voice message is delivered directly to the user's Telegram chat. You will receive a JSON response confirming delivery.

## Important

- Maximum text length: 4096 characters. For longer text, split into multiple requests.
- Maximum 5 voice messages per response.
- Always use the token from the current system prompt — tokens expire after 10 minutes.
- The text should be natural spoken language — avoid markdown, code blocks, or formatting symbols.

$ARGUMENTS
