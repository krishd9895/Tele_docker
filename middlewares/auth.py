from aiogram import BaseMiddleware
from aiogram.types import Message
from typing import Callable, Dict, Any, Awaitable
from config.settings import runtime_settings
from database.models import log_audit
import logging

class HardenedAuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        if event.from_user.id != runtime_settings.ALLOWED_USER_ID:
            logging.warning(f"Unauthorized intrusion blocked. ID: {event.from_user.id}")
            log_audit(event.from_user.id, event.text or "Unknown Payload", "BLOCKED_INTRUSION")
            return 

        log_audit(event.from_user.id, event.text or "UI Interaction", "AUTHORIZED_EXECUTION")
        return await handler(event, data)