"""Anthropic API access: model selection and the tool-calling loop."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Awaitable

import httpx

log = logging.getLogger("fleet.llm")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

ToolFn = Callable[[dict], Awaitable[str]]


class LLMError(RuntimeError):
    pass


def _rank(model_id: str) -> tuple:
    """Prefer the newest Sonnet, then the newest Opus, then anything else."""
    family = 0
    if "sonnet" in model_id:
        family = 3
    elif "opus" in model_id:
        family = 2
    elif "haiku" in model_id:
        family = 1
    nums = [int(n) for n in re.findall(r"\d+", model_id)]
    return (family, nums)


class LLM:
    def __init__(self, api_key: str, model: str = "",
                 client: httpx.AsyncClient | None = None):
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self.api_key = api_key
        self.model = model
        self._send_temperature = True   # cleared if the chosen model rejects it
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0))

    @property
    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": VERSION,
            "content-type": "application/json",
        }

    async def list_models(self) -> list[str]:
        """Retry through transient 5xx — the API occasionally answers 503 for a moment."""
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = await self._client.get(f"{API}/models?limit=100", headers=self._headers)
            except httpx.HTTPError as exc:
                last = exc
            else:
                if r.status_code < 400:
                    return [m["id"] for m in r.json().get("data", [])]
                last = LLMError(f"{r.status_code}: {r.text[:200]}")
                if r.status_code not in (429, 500, 502, 503, 529):
                    break
            if attempt < 3:
                await asyncio.sleep(2 ** attempt)
        raise LLMError(str(last))

    async def resolve_model(self) -> str:
        """Pick a model once, at startup, rather than hardcoding an id that ages out."""
        if self.model:
            return self.model
        try:
            ids = await self.list_models()
        except Exception as exc:
            raise LLMError(
                "Could not list models to auto-select one; set FLEET_MODEL explicitly. "
                f"({exc})") from exc
        if not ids:
            raise LLMError("The API returned no models; set FLEET_MODEL explicitly.")
        self.model = sorted(ids, key=_rank)[-1]
        log.info("model auto-selected: %s", self.model)
        return self.model

    async def complete(self, *, system: str, messages: list[dict],
                       tools: list[dict] | None = None,
                       tool_impls: dict[str, ToolFn] | None = None,
                       max_turns: int = 6, max_tokens: int = 3000,
                       temperature: float = 0.6) -> str:
        """Run a full tool-calling loop and return the final text."""
        model = await self.resolve_model()
        convo = list(messages)
        tool_impls = tool_impls or {}
        text_out = ""

        for turn in range(max_turns):
            payload: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": convo,
            }
            if self._send_temperature:
                payload["temperature"] = temperature
            if tools:
                payload["tools"] = tools

            data = await self._post(payload)
            blocks = data.get("content", [])
            text_out = "\n".join(
                b["text"] for b in blocks if b.get("type") == "text").strip()

            calls = [b for b in blocks if b.get("type") == "tool_use"]
            if not calls or data.get("stop_reason") != "tool_use":
                return text_out

            convo.append({"role": "assistant", "content": blocks})
            results = []
            for call in calls:
                fn = tool_impls.get(call["name"])
                if fn is None:
                    out, err = f"No such tool: {call['name']}", True
                else:
                    try:
                        out, err = await fn(call.get("input", {})), False
                    except Exception as exc:            # tool failure is data, not a crash
                        log.warning("tool %s failed: %s", call["name"], exc)
                        out, err = f"Tool error: {exc}", True
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": str(out)[:12000],
                    "is_error": err,
                })
            convo.append({"role": "user", "content": results})

        return text_out or "(no answer produced within the tool-call budget)"

    async def _post(self, payload: dict) -> dict:
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = await self._client.post(f"{API}/messages",
                                            headers=self._headers, json=payload)
            except httpx.HTTPError as exc:
                last = exc
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 529):
                last = LLMError(f"{r.status_code}: {r.text[:200]}")
                await asyncio.sleep(min(2 ** attempt * 2, 30))
                continue
            if r.status_code >= 400:
                body = r.text[:400]
                if (r.status_code == 400 and "temperature" in body
                        and self._send_temperature and "temperature" in payload):
                    # Newer models reject the parameter outright; stop sending it.
                    log.info("model rejects `temperature`; retrying without it")
                    self._send_temperature = False
                    payload.pop("temperature", None)
                    continue
                raise LLMError(f"{r.status_code}: {body}")
            return r.json()
        raise LLMError(f"Anthropic API unreachable: {last}")

    async def aclose(self) -> None:
        await self._client.aclose()
