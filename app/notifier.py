"""Telegram notifier."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Sequence

import httpx

logger = logging.getLogger(__name__)


class Telegram:
    def __init__(self, bot_token: str, chat_ids: Sequence[str]) -> None:
        if not chat_ids:
            raise ValueError("chat_ids must not be empty")
        self.bot_token = bot_token
        self.chat_ids = tuple(str(cid).strip() for cid in chat_ids)
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "Telegram":
        self._client = httpx.AsyncClient(timeout=20.0)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _send_to_chat(
        self, chat_id: str, text: str, *, retries: int
    ) -> bool:
        assert self._client is not None
        url = f"{self._base}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        delay = 1.0
        for attempt in range(1, retries + 1):
            try:
                r = await self._client.post(url, data=payload)
                if r.status_code == 200:
                    return True
                logger.warning(
                    "Telegram sendMessage failed chat_id=%s (attempt %s): HTTP %s %s",
                    chat_id,
                    attempt,
                    r.status_code,
                    r.text[:200],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Telegram error chat_id=%s (attempt %s): %s",
                    chat_id,
                    attempt,
                    exc,
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        return False

    async def send(self, text: str, *, retries: int = 3) -> bool:
        """Отправляет одно и то же сообщение во все чаты из ``chat_ids``.

        Возвращает ``True``, только если **всем** адресатам отправка прошла.
        """
        assert self._client is not None
        ok = True
        for chat_id in self.chat_ids:
            if not await self._send_to_chat(chat_id, text, retries=retries):
                ok = False
        return ok
