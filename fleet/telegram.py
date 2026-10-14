"""Minimal async Telegram Bot API client.

Only Tony long-polls. The other agents' tokens are send-only, so each agent still
appears in the group as its own member with its own name and avatar, without four
pollers racing to answer the same message.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("fleet.telegram")

API = "https://api.telegram.org"
TG_LIMIT = 4096


class TelegramError(RuntimeError):
    pass


class Bot:
    def __init__(self, token: str, name: str = "bot", client: httpx.AsyncClient | None = None):
        if not token:
            raise TelegramError(f"{name}: empty token")
        self.token = token
        self.name = name
        self._client = client
        self._owns_client = client is None
        self.username: str | None = None

    async def __aenter__(self) -> "Bot":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=15.0))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=15.0))
        return self._client

    async def call(self, method: str, **params: Any) -> Any:
        url = f"{API}/bot{self.token}/{method}"
        for attempt in range(4):
            try:
                r = await self.client.post(url, json=params)
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise TelegramError(f"{self.name}.{method}: {exc}") from exc
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                retry = 1
                try:
                    retry = int(r.json().get("parameters", {}).get("retry_after", 1))
                except Exception:
                    pass
                await asyncio.sleep(min(retry, 30))
                continue
            data = r.json()
            if not data.get("ok"):
                raise TelegramError(
                    f"{self.name}.{method}: {data.get('error_code')} {data.get('description')}")
            return data["result"]
        raise TelegramError(f"{self.name}.{method}: gave up after retries")

    async def get_me(self) -> dict:
        me = await self.call("getMe")
        self.username = me.get("username")
        return me

    async def send(self, chat_id: str | int, text: str,
                   reply_to: int | None = None) -> list[dict]:
        """Send text, splitting on paragraph boundaries when over Telegram's limit."""
        sent = []
        for chunk in _split(text):
            params: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
                "link_preview_options": {"is_disabled": True},
            }
            if reply_to and not sent:
                params["reply_parameters"] = {"message_id": reply_to,
                                              "allow_sending_without_reply": True}
            try:
                sent.append(await self.call("sendMessage", **params))
            except TelegramError as exc:
                if "parse" not in str(exc).lower():
                    raise
                params.pop("parse_mode", None)
                sent.append(await self.call("sendMessage", **params))
        return sent

    async def poll(self, offset: int | None, timeout: int = 45) -> list[dict]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "edited_message"],
        }
        if offset is not None:
            params["offset"] = offset
        return await self.call("getUpdates", **params)


def _split(text: str, limit: int = TG_LIMIT) -> list[str]:
    text = text.strip() or "(empty)"
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            chunks.append(para[:cut])
            para = para[cut:].lstrip("\n")
        current = para
    if current:
        chunks.append(current)
    return chunks
