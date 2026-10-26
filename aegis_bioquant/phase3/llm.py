"""
phase3/llm.py
--------------
One exchange with Claude whose reply must be a call to a given tool, as
engines/clinical_nlp/llm_review._ask does for the FDA-decision reviews,
with three differences that matter for the Phase 3 reviews:

  - It returns what the call used (tokens, searches) so each review's
    cost is known. The Phase 3 reviews always search the web, and cost
    was the open question when this track was planned (2026-10-26).
  - It opts into server-side fallback: if the model's safety classifiers
    decline (a biology-category false positive is possible on trial
    material), the API re-runs the request on Anthropic's recommended
    fallback model instead of returning nothing. The model that actually
    answered is returned and stored with the review.
  - It uses the current web search tool version.

Kept apart from llm_review.py because that file is part of the
FDA-decision study's fingerprint and must not change.

The API key is read from settings and sent only to api.anthropic.com.
Errors are raised as llm_review.LLMError with a status code or exception
class name, never headers or the request, so the key cannot reach a log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import httpx

from aegis_bioquant.config import settings
from aegis_bioquant.engines.clinical_nlp.llm_review import ANTHROPIC_URL, ANTHROPIC_VERSION, LLMError, _clean
from aegis_bioquant.phase3 import constants as k

FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL = "web_search_20260209"
MAX_CONTINUES = 3


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    searches: int = 0

    def add(self, usage: dict) -> None:
        self.input_tokens += int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)
        self.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
        self.searches += int((usage.get("server_tool_use") or {}).get("web_search_requests") or 0)

    @property
    def cost_usd(self) -> float:
        return round(
            self.input_tokens / 1e6 * k.P3_PRICE_INPUT_PER_MTOK
            + self.output_tokens / 1e6 * k.P3_PRICE_OUTPUT_PER_MTOK
            + self.cache_read_tokens / 1e6 * k.P3_PRICE_CACHE_READ_PER_MTOK
            + self.searches * k.P3_PRICE_PER_SEARCH,
            4,
        )


@dataclass
class Answer:
    fields: dict
    found_urls: set[str] = field(default_factory=set)
    usage: Usage = field(default_factory=Usage)
    model: str = ""


async def ask(
    client: httpx.AsyncClient, system: str, user: str, tool: dict, *, web_searches: int = 0,
    max_tokens: int = k.P3_REVIEW_MAX_TOKENS, model: Optional[str] = None,
) -> Answer:
    tools = [tool]
    if web_searches:
        tools = [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": web_searches}, tool]
    instruction = f"Give your answer only by calling the {tool['name']} tool. Do not write anything else."
    messages: list[dict] = [{"role": "user", "content": user}]
    headers = {"x-api-key": settings.ANTHROPIC_API_KEY or "", "anthropic-version": ANTHROPIC_VERSION,
               "anthropic-beta": FALLBACK_BETA}
    answer = Answer(fields={})

    for _ in range(MAX_CONTINUES + 1):
        body = {
            "model": model or settings.LLM_REVIEW_MODEL,
            "max_tokens": max_tokens,
            "system": system + "\n\n" + instruction,
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "auto"},   # the model does not accept a forced tool call
            "output_config": {"effort": k.P3_REVIEW_EFFORT},
            "fallbacks": "default",
        }
        try:
            resp = await client.post(ANTHROPIC_URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"request failed: {type(exc).__name__}") from None
        if resp.status_code != 200:
            try:
                detail = _clean(resp.json().get("error", {}).get("message"), 140)
            except ValueError:
                detail = ""
            raise LLMError(f"API returned HTTP {resp.status_code}" + (f": {detail}" if detail else ""))
        reply = resp.json()
        answer.usage.add(reply.get("usage") or {})
        answer.model = str(reply.get("model") or body["model"])
        content = reply.get("content", [])
        for block in content:
            if block.get("type") == "web_search_tool_result" and isinstance(block.get("content"), list):
                answer.found_urls.update(r.get("url") for r in block["content"]
                                         if isinstance(r, dict) and isinstance(r.get("url"), str))
        stop = reply.get("stop_reason")
        if stop == "refusal":
            category = _clean((reply.get("stop_details") or {}).get("category"), 40)
            raise LLMError("declined by the model" + (f" ({category})" if category else ""))
        if stop == "max_tokens":
            raise LLMError("reply was cut off")
        if stop == "pause_turn":
            messages = messages[:1] + [{"role": "assistant", "content": content}]
            continue
        for block in content:
            if block.get("type") == "tool_use" and block.get("name") == tool["name"] and isinstance(block.get("input"), dict):
                answer.fields = block["input"]
                return answer
        raise LLMError("reply did not contain the expected fields")
    raise LLMError("search did not finish")
