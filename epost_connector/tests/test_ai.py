"""Which Flow `ask_json` calls: frappe's built-in one, the separate `flow` app, or none.

The modules are faked in `sys.modules`, so no model is called and the test holds
whether or not the bench has frappe >= 16.60 or the `flow` app installed.
"""

from __future__ import annotations

import inspect
import sys
from importlib.machinery import ModuleSpec
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from epost_connector import ai

SCHEMA = {"type": "object"}
MESSAGES = [{"role": "user", "content": "Read this letter."}]


def _module(name: str) -> ModuleType:
	# importlib.util.find_spec raises on a module in sys.modules whose __spec__ is None.
	module = ModuleType(name)
	module.__spec__ = ModuleSpec(name, None)
	return module


def _frappe_flow(completion: MagicMock, model_params: dict | None = None) -> dict[str, ModuleType]:
	model = _module("frappe.flow.lib.model")
	model.model_config = MagicMock(
		return_value={
			"model": "anthropic/m",
			"api_key": "k",
			"api_base": None,
			"model_params": model_params or {"temperature": 0},
		}
	)
	model.litellm_route = lambda model_id, api_base: (model_id, api_base)
	lib, flow = _module("frappe.flow.lib"), _module("frappe.flow")
	lib.model, flow.lib = model, lib
	return {
		"frappe.flow": flow,
		"frappe.flow.lib": lib,
		"frappe.flow.lib.model": model,
		"litellm": SimpleNamespace(completion=completion),
	}


def _flow_app(model_class: MagicMock) -> dict[str, ModuleType]:
	model = _module("flow.lib.model")
	model.Model = model_class
	lib, flow = _module("flow.lib"), _module("flow")
	lib.model, flow.lib = model, lib
	return {"flow": flow, "flow.lib": lib, "flow.lib.model": model}


NO_FRAPPE_FLOW = dict.fromkeys(("frappe.flow", "frappe.flow.lib", "frappe.flow.lib.model"))
NO_FLOW_APP = dict.fromkeys(("flow", "flow.lib", "flow.lib.model"))


def _reply(content: str) -> SimpleNamespace:
	usage = {"prompt_tokens": 7, "completion_tokens": 3}
	return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class AskJsonTest(IntegrationTestCase):
	def setUp(self):
		# The backend is cached per process; each test fakes a different one.
		ai._chat_function.cache_clear()
		self.addCleanup(ai._chat_function.cache_clear)

	def test_frappe_flow_is_preferred(self):
		completion = MagicMock(return_value=_reply('{"a": 1}'))
		app_model = MagicMock()
		modules = _frappe_flow(completion)
		with patch.dict(sys.modules, {**modules, **_flow_app(app_model)}):
			answer, got = ai.ask_json("Claude", MESSAGES, SCHEMA)
		self.assertEqual(answer, {"a": 1})
		self.assertEqual(got, {"prompt_tokens": 7, "completion_tokens": 3})
		modules["frappe.flow.lib.model"].model_config.assert_called_once_with("Claude")
		kwargs = completion.call_args.kwargs
		self.assertEqual((kwargs["model"], kwargs["api_key"], kwargs["temperature"]), ("anthropic/m", "k", 0))
		self.assertEqual(kwargs["timeout"], ai.TIMEOUT)
		self.assertNotIn("api_base", kwargs)
		app_model.assert_not_called()

	def test_extra_params_timeout_wins(self):
		completion = MagicMock(return_value=_reply('{"a": 1}'))
		with patch.dict(sys.modules, _frappe_flow(completion, {"timeout": 5, "temperature": 0})):
			answer, _ = ai.ask_json("Claude", MESSAGES, SCHEMA)
		self.assertEqual(answer, {"a": 1})
		kwargs = completion.call_args.kwargs
		self.assertEqual((kwargs["timeout"], kwargs["temperature"]), (5, 0))

	def test_refused_model_leaves_no_message(self):
		completion = MagicMock()
		modules = _frappe_flow(completion)
		earlier = ["kept"]

		def refuse(*args, **kwargs):
			ai.frappe.local.message_log.append("You don't have permission to use Claude.")
			raise PermissionError

		modules["frappe.flow.lib.model"].model_config.side_effect = refuse
		with (
			patch.dict(sys.modules, modules),
			patch.object(ai.frappe.local, "message_log", earlier, create=True),
			patch.object(ai.frappe, "log_error") as log,
		):
			self.assertEqual(ai.ask_json("Claude", MESSAGES, SCHEMA), (None, None))
		self.assertEqual(earlier, ["kept"])
		log.assert_called_once()
		completion.assert_not_called()

	def test_broken_frappe_flow_is_a_logged_failure(self):
		app_model = MagicMock()
		modules = {**_frappe_flow(MagicMock()), "frappe.flow.lib.model": None, **_flow_app(app_model)}
		traceback = []
		with (
			patch.dict(sys.modules, modules),
			patch.object(
				ai.frappe, "log_error", side_effect=lambda *a, **k: traceback.append(sys.exc_info()[0])
			),
		):
			self.assertEqual(ai.ask_json("Claude", MESSAGES, SCHEMA), (None, None))
		self.assertEqual(len(traceback), 1)
		self.assertTrue(issubclass(traceback[0], ImportError))
		app_model.assert_not_called()

	def test_frappe_flow_api_matches(self):
		"""The real module, where the bench has frappe >= 16.60: the names and arguments ai.py uses."""
		try:
			from frappe.flow.lib.model import litellm_route, model_config
		except ImportError:
			self.skipTest("frappe has no built-in Flow")
		rest = list(inspect.signature(model_config).parameters.values())[1:]
		self.assertTrue(all(p.default is not inspect.Parameter.empty for p in rest))
		self.assertEqual(list(inspect.signature(litellm_route).parameters), ["model_id", "api_base"])

	def test_flow_app_when_frappe_has_no_flow(self):
		app_model = MagicMock()
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
