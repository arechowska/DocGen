from __future__ import annotations

import json

import httpx
import pytest

from docgen.ai.client import (
    ModelConfigurationError,
    ModelError,
    OpenAICompatibleTextModel,
    OpenAICompatibleVisionModel,
    VisionDescription,
    build_text_model,
    build_vision_model,
)
from docgen.config import Settings
from docgen.documents.schemas import WorkingDocument

LOCAL_URL = "http://model.example.test/v1"


@pytest.fixture
def mock_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(f"{LOCAL_URL}/chat/completions")
        assert request.method == "POST"
        request_body = json.loads(request.content)
        assert request_body["model"] == "local-text"
        assert request_body["response_format"]["type"] == "json_schema"
        assert request_body["messages"] == [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"title": "Сценарий", "template_id": "use-case", "nodes": []}
                            )
                        }
                    }
                ]
            },
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def mock_oversized_transport() -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"x" * 10_000_001)
    )


def test_text_model_parses_structured_response(mock_transport: httpx.MockTransport) -> None:
    model = OpenAICompatibleTextModel(
        base_url=LOCAL_URL, model="local-text", transport=mock_transport
    )

    result = model.generate_json("system", "user", WorkingDocument)

    assert result.template_id == "use-case"


def test_text_model_rejects_oversized_response(
    mock_oversized_transport: httpx.MockTransport,
) -> None:
    model = OpenAICompatibleTextModel(
        base_url=LOCAL_URL,
        model="local-text",
        transport=mock_oversized_transport,
        max_response_bytes=10_000_000,
    )

    with pytest.raises(ModelError, match="Ответ модели слишком большой"):
        model.generate_json("system", "user", WorkingDocument)


def test_text_model_reports_invalid_schema_without_response_body() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, json={"choices": [{"message": {"content": '{"title": 1}'}}]}
        )
    )
    model = OpenAICompatibleTextModel(base_url=LOCAL_URL, model="local-text", transport=transport)

    with pytest.raises(ModelError, match="некорректный структурированный ответ") as error:
        model.generate_json("private-system", "private-user", WorkingDocument)

    assert "private" not in str(error.value)


def test_text_model_maps_http_failures_to_safe_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(503, text="private failure"))
    model = OpenAICompatibleTextModel(base_url=LOCAL_URL, model="local-text", transport=transport)

    with pytest.raises(ModelError, match="Локальная модель недоступна") as error:
        model.generate_json("private-system", "private-user", WorkingDocument)

    assert "private" not in str(error.value)


def test_text_model_uses_120_second_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["connect"] == 120.0
        assert request.extensions["timeout"]["read"] == 120.0
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"title":"T","template_id":"use-case","nodes":[]}'}}
                ]
            },
        )

    model = OpenAICompatibleTextModel(
        base_url=LOCAL_URL, model="local-text", transport=httpx.MockTransport(handler)
    )

    assert model.generate_json("system", "user", WorkingDocument).title == "T"


def test_vision_model_describes_image_with_structured_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "local-vision"
        assert body["messages"][1]["content"][1]["image_url"]["url"] == "data:image/png;base64,AAE="
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"description":"Схема процесса"}'}}]},
        )

    model = OpenAICompatibleVisionModel(
        base_url=LOCAL_URL, model="local-vision", transport=httpx.MockTransport(handler)
    )

    assert model.describe(b"\x00\x01", "image/png") == VisionDescription(description="Схема процесса")


def test_model_factories_require_complete_configuration() -> None:
    settings = Settings()

    with pytest.raises(ModelConfigurationError, match="Локальные модели не настроены"):
        build_text_model(settings)
    with pytest.raises(ModelConfigurationError, match="Локальные модели не настроены"):
        build_vision_model(settings)


def test_model_factories_build_http_adapters_only() -> None:
    settings = Settings(
        local_text_base_url=LOCAL_URL,
        local_text_model="local-text",
        local_vision_base_url=LOCAL_URL,
        local_vision_model="local-vision",
    )

    assert type(build_text_model(settings)) is OpenAICompatibleTextModel
    assert type(build_vision_model(settings)) is OpenAICompatibleVisionModel
