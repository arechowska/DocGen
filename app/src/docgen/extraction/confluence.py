from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import SecretStr

from docgen.config import Settings
from docgen.extraction.registry import ExtractionError, ExtractionResult, stable_block_id
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance

_DEFAULT_MAX_RESPONSE_BYTES = 5_000_000
_TIMEOUT_SECONDS = 30.0
_CONFLUENCE_URL_ERROR = "Разрешены только ссылки Confluence"
_NOT_CONFIGURED_ERROR = "Интеграция Confluence не настроена"
_ACCESS_ERROR = "Нет доступа к странице Confluence"
_OVERSIZED_ERROR = "Ответ Confluence слишком большой"
_RETRIEVAL_ERROR = "Не удалось получить страницу Confluence"
_RESPONSE_ERROR = "Не удалось обработать ответ Confluence"


class ConfluenceSource(Protocol):
    def fetch(self, url: str) -> ExtractionResult: ...


class ConfluenceClient:
    def __init__(
        self,
        api_base: str | None = None,
        token: str | SecretStr | None = None,
        *,
        allowed_hosts: Iterable[str] | None = None,
        transport: httpx.BaseTransport | None = None,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        timeout_seconds: float = _TIMEOUT_SECONDS,
    ) -> None:
        self._api_base = api_base.rstrip("/") if api_base else None
        self._token = token
        self._transport = transport
        self._max_response_bytes = max_response_bytes
        self._timeout_seconds = timeout_seconds
        self._allowed_hosts = frozenset(host.lower() for host in allowed_hosts or self._api_hosts())

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> ConfluenceClient:
        return cls(
            api_base=str(settings.confluence_api_base) if settings.confluence_api_base else None,
            token=settings.confluence_token,
            allowed_hosts=settings.confluence_hosts,
            transport=transport,
            max_response_bytes=max_response_bytes,
        )

    def fetch(self, url: str) -> ExtractionResult:
        token = self._configured_token()
        page_id = self._page_id_from_url(url)
        response_body = self._get_page(page_id, token)
        storage_html = self._storage_html(response_body)
        blocks = _normalize_storage_html(page_id, storage_html)
        normalized_text = "".join(
            character for block in blocks for character in block.text if not character.isspace()
        )
        return ExtractionResult(
            blocks=blocks,
            page_units=math.ceil(max(1, len(normalized_text)) / 1800),
            warnings=[],
        )

    def _api_hosts(self) -> tuple[str, ...]:
        if self._api_base is None:
            return ()
        host = urlsplit(self._api_base).hostname
        return (host,) if host else ()

    def _configured_token(self) -> str:
        if self._api_base is None or self._token is None:
            raise ExtractionError(_NOT_CONFIGURED_ERROR)
        token = self._token.get_secret_value() if isinstance(self._token, SecretStr) else self._token
        if not token:
            raise ExtractionError(_NOT_CONFIGURED_ERROR)
        return token

    def _page_id_from_url(self, url: str) -> str:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            _ = parsed.port
        except ValueError:
            raise ExtractionError(_CONFLUENCE_URL_ERROR) from None
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or host is None
            or host.lower() not in self._allowed_hosts
        ):
            raise ExtractionError(_CONFLUENCE_URL_ERROR)

        page_ids = parse_qs(parsed.query).get("pageId", [])
        if len(page_ids) == 1 and page_ids[0].isdigit():
            return page_ids[0]
        for path_part in parsed.path.split("/"):
            if path_part.isdigit() and "/pages/" in f"{parsed.path}/":
                return path_part
        raise ExtractionError(_CONFLUENCE_URL_ERROR)

    def _get_page(self, page_id: str, token: str) -> dict:
        assert self._api_base is not None
        endpoint = f"{self._api_base}/content/{page_id}"
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout_seconds,
                follow_redirects=False,
            ) as client, client.stream(
                "GET",
                endpoint,
                params={"expand": "body.storage,version"},
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.status_code in {401, 403}:
                    raise ExtractionError(_ACCESS_ERROR)
                if response.is_error or response.is_redirect:
                    raise ExtractionError(_RETRIEVAL_ERROR)
                self._check_content_length(response)
                body = self._read_limited_body(response)
        except ExtractionError:
            raise
        except httpx.HTTPError as error:
            raise ExtractionError(_RETRIEVAL_ERROR) from error

        try:
            payload = json.loads(body)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExtractionError(_RESPONSE_ERROR) from error
        if not isinstance(payload, dict):
            raise ExtractionError(_RESPONSE_ERROR)
        return payload

    def _check_content_length(self, response: httpx.Response) -> None:
        content_length = response.headers.get("Content-Length")
        if content_length is None:
            return
        try:
            is_oversized = int(content_length) > self._max_response_bytes
        except ValueError:
            return
        if is_oversized:
            raise ExtractionError(_OVERSIZED_ERROR)

    def _read_limited_body(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total_bytes = 0
        for chunk in response.iter_bytes():
            total_bytes += len(chunk)
            if total_bytes > self._max_response_bytes:
                raise ExtractionError(_OVERSIZED_ERROR)
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _storage_html(payload: dict) -> str:
        try:
            storage_html = payload["body"]["storage"]["value"]
        except (KeyError, TypeError) as error:
            raise ExtractionError(_RESPONSE_ERROR) from error
        if not isinstance(storage_html, str):
            raise ExtractionError(_RESPONSE_ERROR)
        return storage_html


def _normalize_storage_html(page_id: str, storage_html: str) -> list[NormalizedBlock]:
    soup = BeautifulSoup(storage_html, "html.parser")
    source_id = f"confluence:{page_id}"
    counters: dict[str, int] = {"heading": 0, "paragraph": 0, "list": 0, "table": 0, "image": 0}
    blocks: list[NormalizedBlock] = []
    for element in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table", "img"]):
        block = _block_from_element(element, source_id, counters)
        if block is not None:
            blocks.append(block)
    return blocks


def _block_from_element(
    element: Tag,
    source_id: str,
    counters: dict[str, int],
) -> NormalizedBlock | None:
    if element.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        text = element.get_text(" ", strip=True)
        return _make_block(source_id, BlockKind.HEADING, "heading", text, {"level": int(element.name[1:])}, counters)
    if element.name == "p":
        if element.find_parent(["li", "td", "th"]) is not None:
            return None
        return _make_block(source_id, BlockKind.TEXT, "paragraph", element.get_text(" ", strip=True), {}, counters)
    if element.name in {"ul", "ol"}:
        if element.find_parent(["li"]) is not None:
            return None
        items = [item.get_text(" ", strip=True) for item in element.find_all("li", recursive=False)]
        return _make_block(
            source_id,
            BlockKind.LIST,
            "list",
            "\n".join(items),
            {"ordered": element.name == "ol", "items": items},
            counters,
        )
    if element.name == "table":
        rows = [
            [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)]
            for row in element.find_all("tr")
        ]
        rows = [row for row in rows if row]
        return _make_block(
            source_id,
            BlockKind.TABLE,
            "table",
            "\n".join("\t".join(row) for row in rows),
            {"rows": rows},
            counters,
        )
    if element.name == "img":
        src = element.get("src")
        alt = element.get("alt", "")
        if not isinstance(src, str) or not isinstance(alt, str):
            return None
        return _make_block(source_id, BlockKind.IMAGE, "image", alt or src, {"src": src, "alt": alt}, counters)
    return None


def _make_block(
    source_id: str,
    kind: BlockKind,
    locator_kind: str,
    text: str,
    data: dict,
    counters: dict[str, int],
) -> NormalizedBlock | None:
    if not text and kind is not BlockKind.IMAGE:
        return None
    counters[locator_kind] += 1
    locator = f"{source_id}#{locator_kind}-{counters[locator_kind]}"
    return NormalizedBlock(
        id=stable_block_id(source_id, kind, locator, text),
        kind=kind,
        text=text,
        data=data,
        provenance=[Provenance(source_id=source_id, locator=locator)],
        confidence=1.0,
    )
