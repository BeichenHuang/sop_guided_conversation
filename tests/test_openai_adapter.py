"""The OpenAI adapter against a stand-in client: request shape, conversion, and error handling."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx2
import openai
import pytest
from openai.lib._parsing._completions import type_to_response_format_param

from apps.insurance_claims.domain import (
    ChatMessage,
    IdentityField,
    MessageRole,
    Phase,
    PlanAsk,
    ReplyPlan,
    StatedIdType,
)
from apps.insurance_claims.llm import prompts
from apps.insurance_claims.llm.adapter import LLMError, RealizeContext, UnderstandContext
from apps.insurance_claims.llm.openai_adapter import OpenAIAdapter


def understanding_out(**overrides) -> prompts.UnderstandingOut:
    data = {
        "scope": "in_scope",
        "dialog_acts": ["provide_information"],
        "caller_role": "self",
        "representative": None,
        "identity_updates": [
            {
                "field": "dob",
                "op": "set",
                "value": "1985-03-15",
                "id_type": None,
                "evidence": "DOB is 1985-03-15",
            },
            # Malformed: a set without a value. It is dropped, not fatal.
            {"field": "phone", "op": "set", "value": None, "id_type": None, "evidence": "my phone"},
        ],
        "policy_number": None,
        "intent": "denial_question",
        "case_hint_updates": [{"field": "month", "op": "set", "value": "1", "evidence": "January"}],
        "questions": [{"topic": "denial_reason", "detail": None, "evidence": "why"}],
        "notes": [],
        "email_reply": "none",
        "emotion": {"label": "neutral", "intensity": "low"},
        "safety_concerns": [],
        "process_question": None,
        "withheld_fields": [],
    }
    data.update(overrides)
    return prompts.UnderstandingOut(**data)


class StubCompletions:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def adapter_with(completions, effort="low") -> OpenAIAdapter:
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return OpenAIAdapter(api_key="unused", model="gpt-test", reasoning_effort=effort, client=client)


def completion(parsed=None, refusal=None):
    message = SimpleNamespace(parsed=parsed, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


CONTEXT = UnderstandContext(
    message="DOB is 1985-03-15, why was my January claim denied?",
    phase=Phase.VERIFY_ID,
    recent_messages=(ChatMessage(id="a-0", role=MessageRole.ASSISTANT, text="Hi", turn=0, case_cycle_id=1),),
    provided_identity_fields=(IdentityField.FULL_NAME,),
    pending_question="id_type",
    caller_role="self",
)


def test_the_wire_schemas_are_accepted_as_strict_structured_outputs():
    for schema in (prompts.UnderstandingOut, prompts.ReplyOut):
        param = type_to_response_format_param(schema)
        assert param["json_schema"]["strict"] is True


def test_understanding_request_and_conversion():
    stub = StubCompletions(completion(understanding_out()))
    result = asyncio.run(adapter_with(stub).understand_turn(CONTEXT))
    call = stub.calls[0]
    assert call["model"] == "gpt-test"
    assert call["response_format"] is prompts.UnderstandingOut
    assert call["store"] is False
    assert call["reasoning_effort"] == "low"
    user_prompt = call["messages"][1]["content"]
    for expected in (
        "VERIFY_ID",
        "full_name",
        "id_type",
        "DOB is 1985-03-15, why was my January claim denied?",
    ):
        assert expected in user_prompt
    assert [u.field for u in result.identity_updates] == [IdentityField.DOB]  # the malformed one was dropped
    assert result.case_hint_updates[0].value == "1"
    assert result.questions[0].topic.value == "denial_reason"


def test_no_reasoning_effort_is_sent_when_disabled():
    stub = StubCompletions(completion(understanding_out()))
    asyncio.run(adapter_with(stub, effort=None).understand_turn(CONTEXT))
    assert "reasoning_effort" not in stub.calls[0]


def test_type_only_id_answers_survive_conversion():
    out = understanding_out(
        identity_updates=[
            {
                "field": "id_last4",
                "op": "set",
                "value": None,
                "id_type": "national_id_last4",
                "evidence": "national ID",
            }
        ]
    )
    result = prompts.to_understanding(out)
    assert result.identity_updates[0].id_type is StatedIdType.NATIONAL_ID_LAST4


def test_reply_request_carries_the_brief_and_previous_problems():
    stub = StubCompletions(completion(prompts.ReplyOut(text="Hello?", used_fact_ids=["policy.welcome"])))
    context = RealizeContext(
        plan=ReplyPlan(ask=PlanAsk(slot="closing")),
        facts={},
        recent_messages=(),
        violations=("the reply is empty",),
        brief={"question": "Is there anything else I can help you with today?"},
    )
    draft = asyncio.run(adapter_with(stub).realize_reply(context))
    assert draft.text == "Hello?"
    content = stub.calls[0]["messages"][1]["content"]
    assert "anything else I can help you with today" in content
    assert "problems_with_previous_attempt" in content


@pytest.mark.parametrize(
    "result",
    [completion(parsed=None, refusal="I can't help with that."), completion(parsed=None)],
)
def test_refusals_and_unparsed_output_become_llm_errors(result):
    with pytest.raises(LLMError):
        asyncio.run(adapter_with(StubCompletions(result)).understand_turn(CONTEXT))


def test_provider_errors_do_not_leak_their_message():
    error = openai.APIConnectionError(
        message="failed with key sk-secret-123", request=httpx2.Request("POST", "https://api.openai.com/v1")
    )
    with pytest.raises(LLMError) as raised:
        asyncio.run(adapter_with(StubCompletions(error=error)).understand_turn(CONTEXT))
    assert "sk-secret" not in str(raised.value)
    assert "APIConnectionError" in str(raised.value)
