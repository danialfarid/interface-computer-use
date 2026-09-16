import json

from cua.llm import OpenAICompatibleClient, sanitize_observation_for_model
from cua.models import Locator
from cua.surface import Control, ReadableTarget, SurfaceObservation


def _sensitive_observation():
    return SurfaceObservation(
        "http://127.0.0.1:8765/member?person=Jos%C3%A9%20N%C3%BA%C3%B1ez",
        "José Núñez",
        "Member José Núñez Current savings balance $1,240.50",
        (
            Control("control-0", "input", "José Núñez", Locator("label", "José Núñez")),
            Control("control-1", "button", "Search", Locator("role", "button:Search")),
        ),
        (ReadableTarget("target-0", "$1,240.50", Locator("css", "#balance-value")),),
    )


def test_model_observation_keeps_structure_without_runtime_values():
    safe = sanitize_observation_for_model(
        _sensitive_observation(), {"123456", "José Núñez", "$1,240.50"}
    )
    serialized = json.dumps(safe, ensure_ascii=False)

    assert "José Núñez" not in serialized
    assert "$1,240.50" not in serialized
    assert "control-0" in serialized
    assert safe["controls"][0]["locator_strategy"] == "label"
    assert safe["readable_targets"][0]["id"] == "target-0"

    lower_ascii = SurfaceObservation(
        "http://127.0.0.1:8765/member/alice-smith",
        "alice smith",
        "alice smith current savings balance $1,240.50",
        (Control("control-0", "input", "alice smith", Locator("label", "alice smith")),),
        (),
    )
    lower_safe = sanitize_observation_for_model(lower_ascii)
    assert "alice smith" not in json.dumps(lower_safe)
    assert lower_safe["url"] == "http://127.0.0.1:8765/<REDACTED>"


def test_provider_request_uses_sanitized_observation_and_records_provenance(monkeypatch):
    response_payload = {
        "id": "chatcmpl-test-1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        "choices": [
            {
                "message": {
                    "content": json.dumps({"actions": [], "done": True, "message": "done"})
                }
            }
        ],
    }
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(response_payload).encode()

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr("cua.llm.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(
        api_key="test-key",
        model="test-model",
        endpoint="https://provider.example.test/v1/chat/completions",
    )
    client.set_parameter_names({"member_id"})
    client.set_task_context({"balance"})

    decision = client.decide("look up José Núñez", _sensitive_observation())
    body = json.loads(requests[0][0].data)
    user = json.loads(body["messages"][1]["content"])
    serialized = json.dumps(user, ensure_ascii=False)

    assert decision.done is True
    assert "José Núñez" not in serialized
    assert "$1,240.50" not in serialized
    assert client.provenance() == {
        "decision_source": "provider",
        "model": "test-model",
        "endpoint_host": "provider.example.test",
        "provider_response_id": "chatcmpl-test-1",
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
