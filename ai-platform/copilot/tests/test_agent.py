"""
Unit tests for the Milestone 5 SRECopilot conversational agent.

No cluster, network, or API key is required: the chat model is replaced with
a fake that plays back a scripted sequence of tool-call / final-answer
messages, and the underlying agents are MagicMocks.
"""

from typing import Any, List, Optional
from unittest import TestCase
from unittest.mock import MagicMock

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agent import CopilotReply, SRECopilot


class FakeToolCallingModel(BaseChatModel):
    """
    A minimal fake chat model that plays back a fixed list of AIMessages in
    order, ignoring the actual input messages/tools. Good enough to drive
    langgraph's prebuilt ReAct loop deterministically in tests: script one
    AIMessage with tool_calls per tool round-trip, then a final AIMessage
    with plain content to end the loop.
    """

    responses: List[Any] = []
    calls: List[Any] = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(messages)
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling-model"


class SRECopilotTests(TestCase):
    def setUp(self):
        self.metrics_agent = MagicMock()
        self.trace_agent = MagicMock()
        self.kubernetes_agent = MagicMock()
        self.alert_agent = MagicMock()
        self.investigation_graph = MagicMock()

    def _copilot(self, responses):
        fake_llm = FakeToolCallingModel(responses=responses, calls=[])
        return SRECopilot(
            metrics_agent=self.metrics_agent,
            trace_agent=self.trace_agent,
            kubernetes_agent=self.kubernetes_agent,
            alert_agent=self.alert_agent,
            namespace="otel-demo",
            llm=fake_llm,
            investigation_graph=self.investigation_graph,
        ), fake_llm

    def test_ask_calls_tool_then_returns_final_reply(self):
        self.alert_agent.get_active_alerts.return_value = [{"name": "CheckoutHighErrors", "severity": "critical"}]

        responses = [
            AIMessage(
                content="",
                tool_calls=[{"name": "get_active_alerts", "args": {}, "id": "call1"}],
            ),
            AIMessage(content="There is one active critical alert: CheckoutHighErrors."),
        ]
        copilot, fake_llm = self._copilot(responses)

        reply = copilot.ask("Show me the latest critical alerts", thread_id="t1")

        self.assertIsNone(reply.pending_action)
        self.assertEqual(reply.content, "There is one active critical alert: CheckoutHighErrors.")
        self.alert_agent.get_active_alerts.assert_called_once()

    def test_ask_routes_root_cause_question_to_investigate_service_tool(self):
        self.investigation_graph.investigate.return_value = {
            "rca_report": "# Root Cause Analysis Report\nLikely root cause: crash-looping pod."
        }

        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "investigate_service",
                        "args": {"service_name": "checkout"},
                        "id": "call1",
                    }
                ],
            ),
            AIMessage(content="Checkout is slow because a pod is crash-looping."),
        ]
        copilot, fake_llm = self._copilot(responses)

        reply = copilot.ask("Why is checkout slow?", thread_id="t1")

        self.assertEqual(reply.content, "Checkout is slow because a pod is crash-looping.")
        called_alert = self.investigation_graph.investigate.call_args[0][0]
        self.assertEqual(called_alert["labels"]["service_name"], "checkout")

    def test_conversation_memory_persists_across_turns_on_same_thread(self):
        # First turn: one tool call then a final answer.
        # Second turn (same thread_id): straight to a final answer, proving
        # the checkpointer retained the prior turn's messages.
        responses = [
            AIMessage(
                content="",
                tool_calls=[{"name": "get_active_alerts", "args": {}, "id": "call1"}],
            ),
            AIMessage(content="One active alert: CheckoutHighErrors."),
            AIMessage(content="It's a critical severity alert on checkout."),
        ]
        self.alert_agent.get_active_alerts.return_value = [{"name": "CheckoutHighErrors", "severity": "critical"}]
        copilot, fake_llm = self._copilot(responses)

        first = copilot.ask("Show me the latest alerts", thread_id="shared")
        second = copilot.ask("What severity is it?", thread_id="shared")

        self.assertEqual(first.content, "One active alert: CheckoutHighErrors.")
        self.assertEqual(second.content, "It's a critical severity alert on checkout.")
        # The model's second call should have seen the full prior history
        # (system + human + ai + tool + human), not just the new question.
        second_call_messages = fake_llm.calls[-1]
        self.assertGreater(len(second_call_messages), 2)

    def test_ask_retries_from_checkpoint_after_transient_model_error(self):
        # Mimics Groq's occasional malformed-tool-call error: the tool call
        # succeeds, but the *next* model call (which would normally produce
        # the final answer) blows up once before succeeding on retry.
        self.alert_agent.get_active_alerts.return_value = [{"name": "CheckoutHighErrors", "severity": "critical"}]

        responses = [
            AIMessage(
                content="",
                tool_calls=[{"name": "get_active_alerts", "args": {}, "id": "call1"}],
            ),
            RuntimeError("transient malformed tool call"),
            AIMessage(content="One active alert: CheckoutHighErrors."),
        ]
        copilot, fake_llm = self._copilot(responses)

        reply = copilot.ask("Show me the latest alerts", thread_id="t1")

        self.assertEqual(reply.content, "One active alert: CheckoutHighErrors.")
        # Tool should only have been invoked once, not re-invoked on retry —
        # proves the retry resumed from the checkpoint rather than restarting
        # the whole turn.
        self.alert_agent.get_active_alerts.assert_called_once()
        self.assertEqual(len(fake_llm.calls), 3)

    def test_ask_raises_after_exhausting_retries(self):
        responses = [
            RuntimeError("persistent failure 1"),
            RuntimeError("persistent failure 2"),
            RuntimeError("persistent failure 3"),
        ]
        copilot, fake_llm = self._copilot(responses)

        with self.assertRaises(RuntimeError):
            copilot.ask("hello", thread_id="t1", max_attempts=3)
        self.assertEqual(len(fake_llm.calls), 3)

    def test_ask_pauses_for_approval_when_remediation_tool_is_called(self):
        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "remediate_scale_deployment",
                        "args": {
                            "service_name": "checkout",
                            "desired_replicas": 1,
                            "reason": "checkout was scaled to 0, causing the outage",
                        },
                        "id": "call1",
                    }
                ],
            ),
        ]
        copilot, fake_llm = self._copilot(responses)

        reply = copilot.ask("Fix the checkout deployment", thread_id="t1")

        self.assertIsNotNone(reply.pending_action)
        self.assertEqual(reply.pending_action["action"], "scale_deployment")
        self.assertEqual(reply.pending_action["service_name"], "checkout")
        self.assertEqual(reply.pending_action["desired_replicas"], 1)
        # The remediation must NOT have touched the cluster yet.
        self.kubernetes_agent.scale_deployment.assert_not_called()

    def test_respond_to_approval_true_executes_remediation(self):
        self.kubernetes_agent.scale_deployment.return_value = {
            "name": "checkout", "namespace": "otel-demo", "replicas": 1,
        }
        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "remediate_scale_deployment",
                        "args": {"service_name": "checkout", "desired_replicas": 1, "reason": "outage"},
                        "id": "call1",
                    }
                ],
            ),
            AIMessage(content="Done — checkout is back to 1 replica."),
        ]
        copilot, fake_llm = self._copilot(responses)

        pending = copilot.ask("Fix checkout", thread_id="t1")
        self.assertIsNotNone(pending.pending_action)

        final = copilot.respond_to_approval(True, thread_id="t1")

        self.kubernetes_agent.scale_deployment.assert_called_once_with("otel-demo", "checkout", 1)
        self.assertIsNone(final.pending_action)
        self.assertEqual(final.content, "Done — checkout is back to 1 replica.")

    def test_respond_to_approval_false_skips_remediation(self):
        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "remediate_scale_deployment",
                        "args": {"service_name": "checkout", "desired_replicas": 1, "reason": "outage"},
                        "id": "call1",
                    }
                ],
            ),
            AIMessage(content="Okay, I did not make any changes."),
        ]
        copilot, fake_llm = self._copilot(responses)

        pending = copilot.ask("Fix checkout", thread_id="t1")
        self.assertIsNotNone(pending.pending_action)

        final = copilot.respond_to_approval(False, thread_id="t1")

        self.kubernetes_agent.scale_deployment.assert_not_called()
        self.assertIsNone(final.pending_action)
        self.assertEqual(final.content, "Okay, I did not make any changes.")

    def test_different_thread_ids_do_not_share_memory(self):
        responses = [
            AIMessage(content="Reply for thread A."),
            AIMessage(content="Reply for thread B."),
        ]
        copilot, fake_llm = self._copilot(responses)

        copilot.ask("hello", thread_id="a")
        copilot.ask("hello", thread_id="b")

        # Each thread's call should only contain that thread's own history
        # (system + this one human message), not the other thread's turns.
        for call_messages in fake_llm.calls:
            human_messages = [m for m in call_messages if m.type == "human"]
            self.assertEqual(len(human_messages), 1)


class CopilotReplyDefaultsTests(TestCase):
    """
    Regression: `steps` used to be a NamedTuple field defaulting to a literal
    `[]`, which Python evaluates once at class-definition time. Every reply
    built without an explicit `steps=` therefore shared one list object, so
    mutating any of them corrupted all the others plus every reply created
    afterwards. `auto_responder.record_approval_outcome` and
    `investigate_incident` both persist `reply.steps` straight into the
    incident store, so a leaked mutation would have been written to disk and
    shown in the web UI's Incidents panel against unrelated incidents.
    """

    def test_each_reply_gets_its_own_steps_list(self):
        first = CopilotReply(content="a")
        second = CopilotReply(content="b")
        self.assertIsNot(first.steps, second.steps)

    def test_mutating_one_reply_does_not_affect_another(self):
        first = CopilotReply(content="a")
        second = CopilotReply(content="b")

        first.steps.append({"type": "call", "name": "get_pod_health"})

        self.assertEqual(second.steps, [])
        self.assertEqual(CopilotReply(content="c").steps, [])

    def test_explicit_steps_are_preserved(self):
        steps = [{"type": "call", "name": "get_active_alerts"}]
        reply = CopilotReply(content="a", steps=steps)
        self.assertEqual(reply.steps, steps)

    def test_defaults_are_empty(self):
        reply = CopilotReply(content="a")
        self.assertIsNone(reply.pending_action)
        self.assertEqual(reply.steps, [])
