"""Notification service for delivering proactive agent responses to Telegram.

Subscribes to AgentResponseEvent on the event bus and delivers messages
through the Telegram bot API with rate limiting (1 msg/sec per chat).
"""

import asyncio
from typing import List, Optional, Tuple

import structlog
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from ..events.bus import Event, EventBus
from ..events.types import AgentResponseEvent

logger = structlog.get_logger()

# Telegram rate limit: ~30 msgs/sec globally, ~1 msg/sec per chat
SEND_INTERVAL_SECONDS = 1.1

# A notification target: (chat_id, optional message_thread_id)
NotificationTarget = Tuple[int, Optional[int]]


class NotificationService:
    """Delivers agent responses to Telegram chats with rate limiting."""

    def __init__(
        self,
        event_bus: EventBus,
        bot: Bot,
        default_chat_ids: Optional[List[int]] = None,
    ) -> None:
        self.event_bus = event_bus
        self.bot = bot
        # Convert bare chat IDs to (chat_id, None) targets
        self._targets: List[NotificationTarget] = [
            (cid, None) for cid in (default_chat_ids or [])
        ]
        self._send_queue: asyncio.Queue[AgentResponseEvent] = asyncio.Queue()
        self._last_send_per_chat: dict[int, float] = {}
        self._running = False
        self._sender_task: Optional[asyncio.Task[None]] = None

    def register(self) -> None:
        """Subscribe to agent response events."""
        self.event_bus.subscribe(AgentResponseEvent, self.handle_response)

    async def start(self) -> None:
        """Start the send queue processor."""
        if self._running:
            return
        self._running = True
        self._sender_task = asyncio.create_task(self._process_send_queue())
        logger.info("Notification service started")

    async def stop(self) -> None:
        """Stop the send queue processor."""
        if not self._running:
            return
        self._running = False
        if self._sender_task:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass
        logger.info("Notification service stopped")

    async def handle_response(self, event: Event) -> None:
        """Queue an agent response for delivery."""
        if not isinstance(event, AgentResponseEvent):
            return
        targets = self._resolve_targets(event)
        if not targets:
            logger.warning(
                "Agent response has no notification targets — message will be dropped. "
                "Use /set_notification_channel in a Telegram chat to configure delivery.",
                event_id=event.id,
                text_preview=event.text[:200] if event.text else None,
            )
            return
        await self._send_queue.put(event)

    def set_notification_channel(
        self, chat_id: int, thread_id: Optional[int] = None
    ) -> None:
        """Set a chat (+ optional thread) as a notification target."""
        target: NotificationTarget = (chat_id, thread_id)
        if target not in self._targets:
            self._targets.append(target)
            logger.info(
                "Notification channel added",
                chat_id=chat_id,
                thread_id=thread_id,
            )

    def clear_notification_channel(
        self, chat_id: int, thread_id: Optional[int] = None
    ) -> None:
        """Remove a notification target."""
        target: NotificationTarget = (chat_id, thread_id)
        if target in self._targets:
            self._targets.remove(target)
            logger.info(
                "Notification channel removed",
                chat_id=chat_id,
                thread_id=thread_id,
            )

    @property
    def targets(self) -> List[NotificationTarget]:
        """Current notification targets."""
        return list(self._targets)

    async def _process_send_queue(self) -> None:
        """Process queued messages with rate limiting."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._send_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            targets = self._resolve_targets(event)
            for target in targets:
                await self._rate_limited_send(target, event)

    def _resolve_targets(self, event: AgentResponseEvent) -> List[NotificationTarget]:
        """Determine which chats to send to."""
        if event.chat_id and event.chat_id != 0:
            return [(event.chat_id, None)]
        return list(self._targets)

    async def _rate_limited_send(
        self, target: NotificationTarget, event: AgentResponseEvent
    ) -> None:
        """Send message with per-chat rate limiting."""
        chat_id, thread_id = target
        loop = asyncio.get_event_loop()
        now = loop.time()
        last_send = self._last_send_per_chat.get(chat_id, 0.0)
        wait_time = SEND_INTERVAL_SECONDS - (now - last_send)

        if wait_time > 0:
            await asyncio.sleep(wait_time)

        try:
            # Split long messages (Telegram limit: 4096 chars)
            text = event.text
            chunks = self._split_message(text)

            send_kwargs: dict = {
                "chat_id": chat_id,
                "parse_mode": (
                    ParseMode.HTML if event.parse_mode == "HTML" else None
                ),
            }
            if thread_id is not None:
                send_kwargs["message_thread_id"] = thread_id

            for chunk in chunks:
                await self.bot.send_message(text=chunk, **send_kwargs)
                self._last_send_per_chat[chat_id] = asyncio.get_event_loop().time()

                # Rate limit between chunks too
                if len(chunks) > 1:
                    await asyncio.sleep(SEND_INTERVAL_SECONDS)

            logger.info(
                "Notification sent",
                chat_id=chat_id,
                thread_id=thread_id,
                text_length=len(text),
                chunks=len(chunks),
                originating_event=event.originating_event_id,
            )
        except TelegramError as e:
            logger.error(
                "Failed to send notification",
                chat_id=chat_id,
                thread_id=thread_id,
                error=str(e),
                event_id=event.id,
            )

    def _split_message(self, text: str, max_length: int = 4096) -> List[str]:
        """Split long messages at paragraph boundaries."""
        if len(text) <= max_length:
            return [text]

        chunks: List[str] = []
        while text:
            if len(text) <= max_length:
                chunks.append(text)
                break

            # Try to split at a paragraph boundary
            split_pos = text.rfind("\n\n", 0, max_length)
            if split_pos == -1:
                # Try single newline
                split_pos = text.rfind("\n", 0, max_length)
            if split_pos == -1:
                # Try space
                split_pos = text.rfind(" ", 0, max_length)
            if split_pos == -1:
                # Hard split
                split_pos = max_length

            chunks.append(text[:split_pos])
            text = text[split_pos:].lstrip()

        return chunks
