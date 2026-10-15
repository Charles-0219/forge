from __future__ import annotations

import json
from typing import Any

from forge.events import ModelTurn, ProposedAction
from forge.hashing import arguments_hash, canonical_json, sha256_text


def adapt(protocol: str, response: Any) -> ModelTurn:
    if protocol == "openai":
        return _adapt_openai(response)
    if protocol == "anthropic":
        return _adapt_anthropic(response)
    raise ValueError(f"unsupported protocol: {protocol}")


def _adapt_openai(response: dict) -> ModelTurn:
    message = ((response.get("choices") or [{}])[0].get("message") or {})
    text = message.get("content") or ""
    if text is None:
        text = ""
    actions: list[ProposedAction] = []
    for index, call in enumerate(message.get("tool_calls") or []):
        call_id = str(call.get("id") or f"missing-{index}")
        if call.get("type") != "function":
            actions.append(_action(call_id, index, str(call.get("type") or "unsupported"), {}, "unsupported_tool_call"))
            continue
        function = call.get("function") or {}
        name = str(function.get("name") or "unsupported")
        raw = function.get("arguments") or "{}"
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, json.JSONDecodeError):
            actions.append(_action(call_id, index, name, {}, "invalid_arguments"))
            continue
        if not isinstance(parsed, dict):
            actions.append(_action(call_id, index, name, {}, "invalid_arguments"))
            continue
        actions.append(_action(call_id, index, name, parsed, None))
    usage = response.get("usage") or {}
    return ModelTurn(
        protocol="openai",
        model_call_id=_call_id(response),
        text=str(text),
        actions=actions,
        usage={
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    )


def _adapt_anthropic(response: Any) -> ModelTurn:
    data = _anthropic_dict(response)
    texts: list[str] = []
    actions: list[ProposedAction] = []
    for block in data["content"]:
        if block["type"] == "text":
            texts.append(str(block.get("text") or ""))
            continue
        if block["type"] != "tool_use":
            continue
        index = len(actions)
        raw_input = block.get("input") if isinstance(block.get("input"), dict) else {}
        actions.append(_action(str(block.get("id") or f"missing-{index}"), index, str(block.get("name") or ""), raw_input, None))
    usage = data.get("usage") or {}
    return ModelTurn(
        protocol="anthropic",
        model_call_id=_call_id(data),
        text="".join(texts),
        actions=actions,
        usage={
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
        },
    )


def _action(call_id: str, index: int, name: str, arguments: dict, rejection: str | None) -> ProposedAction:
    return ProposedAction(
        tool_call_id=call_id,
        call_index=index,
        tool_name=name,
        arguments=arguments,
        arguments_hash=arguments_hash(arguments),
        immediate_rejection=rejection,
    )


def _call_id(payload: dict) -> str:
    if payload.get("id"):
        return str(payload["id"])
    return "mc_" + sha256_text(canonical_json(payload))[:24]


def _anthropic_dict(response: Any) -> dict:
    if isinstance(response, dict):
        return response
    content = []
    for block in getattr(response, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "tool_use":
            content.append({
                "type": "tool_use",
                "id": getattr(block, "id", None),
                "name": getattr(block, "name", None),
                "input": getattr(block, "input", {}) or {},
            })
        elif block_type == "text":
            content.append({"type": "text", "text": getattr(block, "text", "")})
        else:
            content.append({"type": block_type})
    usage = getattr(response, "usage", None)
    usage_dict = None
    if usage is not None:
        usage_dict = {
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
        }
    return {"id": getattr(response, "id", None), "content": content, "usage": usage_dict}
