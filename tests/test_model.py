import json
import threading

import httpx
import pytest

from app.config import Settings
from app.model import ModelProviderError, ReconciliationAgent, request_payload


def test_model_uses_guest_feedback_to_repair_program():
    attempts = []

    def handler(request):
        body = json.loads(request.content)
        attempts.append(body)
        if len(attempts) == 1:
            name, arguments = "run_python", {"source": "broken"}
        else:
            assert body["messages"][-1]["role"] == "tool"
            assert "report validation failed" in body["messages"][-1]["content"]
            assert "wrong result" not in body["messages"][-1]["content"]
            name, arguments = "run_python", {"source": "repaired"}
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": str(len(attempts)),
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ],
                        }
                    }
                ],
            },
        )

    class Guest:
        def check_cancelled(self):
            pass

        def execute_agent(self, source):
            self.source = source

        def read_report(self):
            return self.source

    def validate(value):
        if value != "repaired":
            raise ValueError("wrong result")

    settings = Settings(llm_base_url="https://model.invalid/v1", llm_key="fixture", llm_model="fixture")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        agent = ReconciliationAgent(settings, threading.Event(), client)
        assert agent.run(Guest(), validate) == "repaired"
    assert agent.input_tokens == 20
    assert agent.output_tokens == 10


def test_free_only_requests_have_zero_provider_price_caps():
    settings = Settings(
        llm_base_url="https://openrouter.ai/api/v1", llm_model="fixture/model:free", llm_free_only=True
    )
    payload = request_payload(settings, [])
    assert payload["model"].endswith(":free")
    assert payload["provider"]["max_price"] == {"prompt": 0, "completion": 0, "request": 0}
    assert "models" not in payload  # No paid fallback chain.


@pytest.mark.parametrize(
    "url,model",
    [
        ("https://openrouter.ai/api/v1", "fixture/paid"),
        ("https://other.invalid/v1", "fixture/model:free"),
        ("https://openrouter.ai/api/v1?redirect=elsewhere", "fixture/model:free"),
    ],
)
def test_free_only_rejects_paid_models_or_other_endpoints(url, model):
    with pytest.raises(ValueError):
        request_payload(Settings(llm_base_url=url, llm_model=model, llm_free_only=True), [])


def test_free_only_rejects_nonzero_reported_cost_before_tool_execution():
    def handler(request):
        body = json.loads(request.content)
        assert body["provider"]["max_price"]["completion"] == 0
        return httpx.Response(200, json={"usage": {"cost": 0.01}, "choices": []})

    class Guest:
        def check_cancelled(self):
            pass

    settings = Settings(
        llm_base_url="https://openrouter.ai/api/v1",
        llm_key="fixture",
        llm_model="fixture/model:free",
        llm_free_only=True,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="nonzero model cost"):
            ReconciliationAgent(settings, threading.Event(), client).run(Guest(), lambda value: None)


def test_provider_error_keeps_useful_diagnostics_and_redacts_keys():
    def handler(request):
        return httpx.Response(
            400, json={"error": {"message": "Invalid tool transcript; credential secret-key"}}
        )

    class Guest:
        def check_cancelled(self):
            pass

    settings = Settings(llm_base_url="https://model.invalid/v1", llm_key="secret-key", llm_model="fixture")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelProviderError, match="Invalid tool transcript") as error:
            ReconciliationAgent(settings, threading.Event(), client).run(Guest(), lambda value: None)
    assert "secret-key" not in str(error.value)
    assert error.value.status_code == 400
