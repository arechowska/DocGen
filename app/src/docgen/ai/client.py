from __future__ import annotations

import base64
import json
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from docgen.ai.prompts import VISION_SYSTEM_PROMPT
from docgen.config import Settings
from docgen.outbound import UntrustedEndpoint, validate_trusted_endpoint

T = TypeVar("T", bound=BaseModel)

_TIMEOUT_SECONDS = 120.0
_DEFAULT_MAX_RESPONSE_BYTES = 10_000_000
_DEFAULT_MAX_REQUEST_BYTES = 20_971_520


class ModelError(RuntimeError):
    """A user-safe error returned by a local model adapter."""


class ModelConfigurationError(ValueError):
    """Raised when a production model adapter cannot be configured."""


class TextModel(Protocol):
    def generate_json(self, system: str, user: str, schema: type[T]) -> T: ...


class VisionDescription(BaseModel):
    description: str


class VisionModel(Protocol):
    def describe(self, image: bytes, media_type: str) -> VisionDescription: ...


class _OpenAICompatibleModel:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: SecretStr | str | None = None,
        transport: httpx.BaseTransport | None = None,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
    ) -> None:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if max_request_bytes <= 0:
            raise ValueError("max_request_bytes must be positive")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = (
            api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        )
        self._transport = transport
        self._max_response_bytes = max_response_bytes
        self._max_request_bytes = max_request_bytes

    def _complete(self, messages: list[dict[str, object]], schema: type[T]) -> T:
        is_qwen = self._model.lower().startswith("qwen")
        payload = {
            "model": self._model,
            "messages": messages,
        }
        if is_qwen:
            payload["max_tokens"] = 8192
        else:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            }
        print("=== DOCGEN MODEL REQUEST PAYLOAD ===")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        response_bytes = self._post(payload)
        print("=== DOCGEN MODEL RESPONSE RAW ===")
        print(response_bytes.decode("utf-8", errors="replace"))
        try:
            response_data = json.loads(response_bytes)
            content = response_data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("model content is not a string")
            print("=== DOCGEN MODEL RESPONSE CONTENT ===")
            print(content)
            return schema.model_validate_json(content)
        except (IndexError, KeyError, TypeError, UnicodeDecodeError, ValidationError, json.JSONDecodeError) as exc:
            raise ModelError("Модель вернула некорректный структурированный ответ") from exc

    def _post(self, payload: dict[str, object]) -> bytes:
        payload_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload_bytes) > self._max_request_bytes:
            raise ModelError("Запрос к модели слишком большой")
        try:
            with (
                httpx.Client(transport=self._transport, timeout=_TIMEOUT_SECONDS) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    content=payload_bytes,
                    headers=self._headers(),
                ) as response,
            ):
                response.raise_for_status()
                return self._read_limited(response)
        except ModelError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ModelError("Локальная модель недоступна") from exc
        except httpx.RequestError as exc:
            raise ModelError("Не удалось подключиться к локальной модели") from exc

    def _read_limited(self, response: httpx.Response) -> bytes:
        response_body = bytearray()
        for chunk in response.iter_bytes():
            if len(response_body) + len(chunk) > self._max_response_bytes:
                raise ModelError("Ответ модели слишком большой")
            response_body.extend(chunk)
        return bytes(response_body)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers


class OpenAICompatibleTextModel(_OpenAICompatibleModel):
    def generate_json(self, system: str, user: str, schema: type[T]) -> T:
        return self._complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            schema,
        )


class OpenAICompatibleVisionModel(_OpenAICompatibleModel):
    def describe(self, image: bytes, media_type: str) -> VisionDescription:
        image_data = base64.b64encode(image).decode("ascii")
        return self._complete(
            [
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Опишите изображение для документа."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{image_data}"},
                        },
                    ],
                },
            ],
            VisionDescription,
        )


def build_text_model(settings: Settings) -> TextModel:
    if settings.local_text_base_url is None or not settings.local_text_model:
        raise ModelConfigurationError("Локальные модели не настроены")
    try:
        base_url = validate_trusted_endpoint(
            settings.local_text_base_url,
            settings.trusted_integration_hosts,
        )
    except UntrustedEndpoint:
        raise ModelConfigurationError(
            "Адрес локальной модели не разрешён настройками"
        ) from None
    return OpenAICompatibleTextModel(
        base_url=base_url,
        model=settings.local_text_model,
        api_key=settings.local_text_api_key,
        max_request_bytes=settings.max_model_request_bytes,
    )


def build_vision_model(settings: Settings) -> VisionModel:
    if settings.local_vision_base_url is None or not settings.local_vision_model:
        raise ModelConfigurationError("Локальные модели не настроены")
    try:
        base_url = validate_trusted_endpoint(
            settings.local_vision_base_url,
            settings.trusted_integration_hosts,
        )
    except UntrustedEndpoint:
        raise ModelConfigurationError(
            "Адрес локальной модели не разрешён настройками"
        ) from None
    return OpenAICompatibleVisionModel(
        base_url=base_url,
        model=settings.local_vision_model,
        api_key=settings.local_vision_api_key,
        max_request_bytes=settings.max_model_request_bytes,
    )
