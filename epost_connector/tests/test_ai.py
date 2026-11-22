"""Which Flow `ask_json` calls: frappe's built-in one, the separate `flow` app, or none.

The modules are faked in `sys.modules`, so no model is called and the test holds
whether or not the bench has frappe >= 16.60 or the `flow` app installed.
"""

from __future__ import annotations

import inspect
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from epost_connector import ai

SCHEMA = {"type": "object"}
MESSAGES = [{"role": "user", "content": "Read this letter."}]


def _modules(**named: ModuleType | None) -> dict[str, ModuleType | None]:
	return {name.replace("__", "."): module for name, module in named.items()}


def _frappe_flow(completion: MagicMock, model_params: dict | None = None) -> dict[str, ModuleType | None]:
	model = ModuleType("frappe.flow.lib.model")
	model.model_config = MagicMock(
		return_value={
			"model": "anthropic/m",
			"api_key": "k",
			"api_base": None,
			"model_params": model_params or {"temperature": 0},
		}
	)
	model.litellm_route = lambda model_id, api_base: (model_id, api_base)
	lib, flow = ModuleType("frappe.flow.lib"), ModuleType("frappe.flow")
	lib.model, flow.lib = model, lib
	return _modules(
		frappe__flow=flow,
		frappe__flow__lib=lib,
		frappe__flow__lib__model=model,
		litellm=SimpleNamespace(completion=completion),
	)


def _flow_app(model_class: MagicMock) -> dict[str, ModuleType | None]:
	model = ModuleType("flow.lib.model")
	model.Model = model_class
	lib, flow = ModuleType("flow.lib"), ModuleType("flow")
	lib.model, flow.lib = model, lib
	return _modules(flow=flow, flow__lib=lib, flow__lib__model=model)


NO_FRAPPE_FLOW = _modules(frappe__flow=None, frappe__flow__lib=None, frappe__flow__lib__model=None)
NO_FLOW_APP = _modules(flow=None, flow__lib=None, flow__lib__model=None)


def _reply(content: str) -> SimpleNamespace:
	usage = SimpleNamespace(prompt_tokens=7, completion_tokens=3)
	return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class AskJsonTest(IntegrationTestCase):
	def test_frappe_flow_is_preferred(self):
		completion = MagicMock(return_value=_reply('{"a": 1}'))
		app_model = MagicMock()
		modules = _frappe_flow(completion)
		with patch.dict(sys.modules, {**modules, **_flow_app(app_model)}):
			answer, got = ai.ask_json("Claude", MESSAGES, SCHEMA)
		self.assertEqual(answer, {"a": 1})
		self.assertEqual(got, {"prompt_tokens": 7, "completion_tokens": 3})
		modules["frappe.flow.lib.model"].model_config.assert_called_once_with(
			"Claude", check_permission=False
		)
		kwargs = completion.call_args.kwargs
		self.assertEqual((kwargs["model"], kwargs["api_key"], kwargs["temperature"]), ("anthropic/m", "k", 0))
		self.assertEqual(kwargs["timeout"], ai.TIMEOUT)
		app_model.assert_not_called()

	def test_extra_params_timeout_does_not_collide(self):
		completion = MagicMock(return_value=_reply('{"a": 1}'))
		with patch.dict(sys.modules, _frappe_flow(completion, {"timeout": 5, "temperature": 0})):
			answer, _ = ai.ask_json("Claude", MESSAGES, SCHEMA)
		self.assertEqual(answer, {"a": 1})
		kwargs = completion.call_args.kwargs
		self.assertEqual((kwargs["timeout"], kwargs["temperature"]), (ai.TIMEOUT, 0))

	def test_frappe_flow_api_matches(self):
		"""The real module, where the bench has frappe >= 16.60: the names and keywords ai.py uses."""
		try:
			from frappe.flow.lib.model import litellm_route, model_config
		except ImportError:
			self.skipTest("frappe has no built-in Flow")
		check = inspect.signature(model_config).parameters["check_permission"]
		self.assertEqual(check.kind, inspect.Parameter.KEYWORD_ONLY)
		self.assertEqual(list(inspect.signature(litellm_route).parameters), ["model_id", "api_base"])

	def test_flow_app_when_frappe_has_no_flow(self):
		app_model = MagicMock()
		# The flow app's ChatResponse.usage: a dict with total_tokens too.
		app_model.return_value.chat.return_value = SimpleNamespace(
			content='{"b": 2}', usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
		)
		with patch.dict(sys.modules, {**NO_FRAPPE_FLOW, **_flow_app(app_model)}):
			answer, usage = ai.ask_json("Claude", MESSAGES, SCHEMA)
		self.assertEqual(answer, {"b": 2})
		self.assertEqual(usage, {"prompt_tokens": 1, "completion_tokens": 2})
		app_model.assert_called_once_with("Claude")

	def test_no_flow_at_all(self):
		with (
			patch.dict(sys.modules, {**NO_FRAPPE_FLOW, **NO_FLOW_APP}),
			patch.object(ai.frappe, "log_error") as log,
		):
			self.assertEqual(ai.ask_json("Claude", MESSAGES, SCHEMA), (None, None))
		log.assert_called_once()
