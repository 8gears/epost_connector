"""One JSON answer from a Frappe Flow model.

Flow is an optional app, so everything here degrades to `None` rather than
raising: a letter the model could not read is the same outcome as a letter no
model was asked about.

The schema goes into the prompt instead of `response_format`. litellm 1.83.7
emulates `response_format` for Anthropic models it does not list as supporting
structured output by forcing a tool call, and newer models refuse that with
"tool_choice: type "tool" and "any" are not supported for this model". Every
caller validates the answer against what it may contain anyway, so a prompt-level
schema loses nothing.
"""

from __future__ import annotations

import json
import re
from typing import Any

import frappe

# The flow app's Model default.
TIMEOUT = 60
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def ask_json(model_name: str, messages: list[dict], schema: dict) -> tuple[dict | None, dict | None]:
	"""Return (answer, usage). The answer is None when there is no usable JSON object."""
	chat = _chat_function()
	if chat is None:
		frappe.log_error("ePost: Frappe Flow is not installed", reference_doctype="ePost Settings")
		return None, None

	messages = [dict(m) for m in messages]
	messages[0]["content"] = (
		f"{messages[0]['content']}\n\nReply with one JSON object and nothing else. "
		f"It must validate against this JSON Schema:\n{json.dumps(schema)}"
	)

	try:
		content, usage = chat(model_name, messages)
	except Exception:
		frappe.log_error("ePost: model call failed", reference_doctype="ePost Settings")
		return None, None

	return parse_object(content), usage


def _chat_function():
	"""frappe's built-in Flow (frappe >= 16.60) when present, else the separate `flow` app."""
	try:
		from frappe.flow.lib import model
	except ImportError:
		pass
	else:
		return _chat_frappe_flow
	try:
		from flow.lib.model import Model
	except ImportError:
		return None
	return _chat_flow_app


def _chat_frappe_flow(model_name: str, messages: list[dict]) -> tuple[str | None, dict | None]:
	"""frappe's Flow has no chat call outside its daemon, so litellm is called with its config.
	No permission check: the old app's `Model` had none, and the callers run as scheduled jobs."""
	import litellm
	from frappe.flow.lib.model import litellm_route, model_config

	config = model_config(model_name, check_permission=False)
	model, api_base = litellm_route(config["model"], config.get("api_base"))
	response = litellm.completion(
		model=model,
		api_key=config.get("api_key"),
		api_base=api_base,
		messages=messages,
		timeout=TIMEOUT,
		**config.get("model_params", {}),
	)
	return response.choices[0].message.content, _usage(getattr(response, "usage", None))


def _chat_flow_app(model_name: str, messages: list[dict]) -> tuple[str | None, dict | None]:
	from flow.lib.model import Model

	response = Model(model_name).chat(messages)
	return response.content, getattr(response, "usage", None)


def _usage(usage: Any) -> dict | None:
	if not usage:
		return None
	return {
		"prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
		"completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
	}


def parse_object(content: str | None) -> dict | None:
	"""The JSON object in `content`, tolerating a Markdown fence around it."""
	if not content:
		return None
	text = FENCE.sub("", content.strip())
	start, end = text.find("{"), text.rfind("}")
	if start < 0 or end < start:
		return None
	try:
		value: Any = json.loads(text[start : end + 1])
	except ValueError:
		return None
	return value if isinstance(value, dict) else None
