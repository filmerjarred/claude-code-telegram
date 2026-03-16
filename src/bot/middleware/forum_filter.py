"""Forum topic filtering middleware.

Drops messages from non-listened forum topics before auth/security
middleware runs, preventing unwanted session creation.
"""

from typing import Any, Callable, Dict

import structlog

logger = structlog.get_logger()

# Commands that must work in any topic (so users can /listen in the first place)
_BYPASS_COMMANDS = frozenset({
    "/listen",
    "/ignore",
    "/topics",
    "/set_notification_channel",
    "/clear_notification_channel",
})


async def forum_filter_middleware(
    handler: Callable, event: Any, data: Dict[str, Any]
) -> Any:
    """Silently drop messages from non-listened forum topics.

    Runs before auth middleware so that messages in ignored topics
    never trigger session creation or welcome messages.
    """
    chat = event.effective_chat
    if not chat:
        return  # No chat context — drop

    # Private chats — always allow
    if getattr(chat, "type", "") == "private":
        return await handler(event, data)

    # Non-forum groups — always allow
    if not getattr(chat, "is_forum", False):
        return await handler(event, data)

    # Check if this is a bypass command
    message = event.effective_message
    if message and message.text:
        first_token = message.text.split()[0].split("@")[0] if message.text.strip() else ""
        if first_token in _BYPASS_COMMANDS:
            return await handler(event, data)

    # Forum group — check listen_topics config
    settings = data.get("settings")
    bot_config = (getattr(settings, "bot_config", None) or {}) if settings else {}
    listen_topics = bot_config.get("listen_topics", {})
    chat_topics = listen_topics.get(str(chat.id), {})

    if not chat_topics:
        return  # No topics configured for this chat — drop silently

    # Extract topic ID (mirrors MessageOrchestrator._extract_message_thread_id)
    message_thread_id = None
    if message:
        raw = getattr(message, "message_thread_id", None)
        if isinstance(raw, int) and raw > 0:
            message_thread_id = raw
        else:
            dm_topic = getattr(message, "direct_messages_topic", None)
            topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
            if isinstance(topic_id, int) and topic_id > 0:
                message_thread_id = topic_id
            else:
                # General topic in forum supergroups has canonical ID 1
                message_thread_id = 1

    if message_thread_id is None or str(message_thread_id) not in chat_topics:
        return  # Topic not in listen list — drop silently

    return await handler(event, data)
