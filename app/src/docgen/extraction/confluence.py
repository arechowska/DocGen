from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterable
from typing import Protocol
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import SecretStr

from docgen.config import Settings
from docgen.extraction.page_units import VirtualPageCalculator
from docgen.extraction.registry import ExtractionError, ExtractionResult, stable_block_id
from docgen.extraction.schemas import BlockKind, NormalizedBlock, Provenance

_DEFAULT_MAX_RESPONSE_BYTES = 5_000_000
_TIMEOUT_SECONDS = 30.0
_MAX_PAGE_UNITS = 150
_CONFLUENCE_URL_ERROR = "Разрешены только ссылки Confluence"
_NOT_CONFIGURED_ERROR = "Интеграция Confluence не настроена"
_ACCESS_ERROR = "Нет доступа к странице Confluence"
_OVERSIZED_ERROR = "Ответ Confluence слишком большой"
_RETRIEVAL_ERROR = "Не удалось получить страницу Confluence"
_RESPONSE_ERROR = "Не удалось обработать ответ Confluence"
_ATTACHMENT_NOT_FOUND_ERROR = "Вложение Confluence не найдено"
_ATTACHMENT_RESPONSE_ERROR = "Не удалось обработать вложение Confluence"
_ATTACHMENT_URL_ERROR = "Недопустимая ссылка вложения Confluence"
_ATTACHMENT_BUDGET_ERROR = "Общий объём вложений Confluence слишком большой"
_PAGE_LIMIT_ERROR = "Максимальный объём — 150 страниц"


class ConfluenceSource(Protocol):
    def fetch(
        self,
        url: str,
        *,
        before_external_call: Callable[[], None] | None = None,
    ) -> ExtractionResult: ...


class ConfluenceClient:
    def __init__(
        self,
        api_base: str | None = None,
        token: str | SecretStr | None = None,
        *,
        allowed_hosts: Iterable[str] | None = None,
        transport: httpx.BaseTransport | None = None,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_attachment_bytes: int | None = None,
        timeout_seconds: float = _TIMEOUT_SECONDS,
    ) -> None:
        self._api_base = api_base.rstrip("/") if api_base else None
        self._token = token
        self._transport = transport
        self._max_response_bytes = max_response_bytes
        self._max_attachment_bytes = (
            max_attachment_bytes if max_attachment_bytes is not None else max_response_bytes
        )
        self._timeout_seconds = timeout_seconds
        self._allowed_hosts = frozenset(host.lower() for host in allowed_hosts or self._api_hosts())

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_attachment_bytes: int | None = None,
    ) -> ConfluenceClient:
        return cls(
            api_base=str(settings.confluence_api_base) if settings.confluence_api_base else None,
            token=settings.confluence_token,
            allowed_hosts=settings.confluence_hosts,
            transport=transport,
            max_response_bytes=max_response_bytes,
            max_attachment_bytes=max_attachment_bytes,
        )

    def fetch(
        self,
        url: str,
        *,
        before_external_call: Callable[[], None] | None = None,
    ) -> ExtractionResult:
        token = self._configured_token()
        page_id = self._page_id_from_url(url)
        response_body = self._get_page(page_id, token, before_external_call)
        storage_html = self._storage_html(response_body)
        blocks = _normalize_storage_html(page_id, storage_html)
        page_units = VirtualPageCalculator().from_blocks(blocks)
        if page_units > _MAX_PAGE_UNITS:
            raise ExtractionError(_PAGE_LIMIT_ERROR)
        resolved_blocks: list[NormalizedBlock] = []
        attachment_bytes = 0
        for block in blocks:
            resolved_block, retained_bytes = self._resolve_native_attachment(
                page_id,
                block,
                token,
                before_external_call,
                self._max_attachment_bytes - attachment_bytes,
            )
            attachment_bytes += retained_bytes
            resolved_blocks.append(resolved_block)
        return ExtractionResult(
            blocks=resolved_blocks,
            page_units=page_units,
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

    def _get_page(
        self,
        page_id: str,
        token: str,
        before_external_call: Callable[[], None] | None,
    ) -> dict:
        assert self._api_base is not None
        endpoint = f"{self._api_base}/content/{page_id}"
        body = self._get_bytes(
            endpoint,
            token,
            params={"expand": "body.storage,version"},
            before_external_call=before_external_call,
        )
        return self._decode_json(body, _RESPONSE_ERROR)

    def _get_bytes(
        self,
        endpoint: str,
        token: str,
        *,
        params: dict[str, str | int] | None = None,
        before_external_call: Callable[[], None] | None = None,
        not_found_message: str = _RETRIEVAL_ERROR,
        max_bytes: int | None = None,
        oversized_message: str = _OVERSIZED_ERROR,
    ) -> bytes:
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout_seconds,
                follow_redirects=False,
            ) as client:
                if before_external_call is not None:
                    before_external_call()
                with client.stream(
                    "GET",
                    endpoint,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    if response.status_code in {401, 403}:
                        raise ExtractionError(_ACCESS_ERROR)
                    if response.status_code == 404:
                        raise ExtractionError(not_found_message)
                    if response.is_error or response.is_redirect:
                        raise ExtractionError(_RETRIEVAL_ERROR)
                    response_limit = (
                        max_bytes if max_bytes is not None else self._max_response_bytes
                    )
                    self._check_content_length(response, response_limit, oversized_message)
                    return self._read_limited_body(response, response_limit, oversized_message)
        except ExtractionError:
            raise
        except httpx.HTTPError as error:
            raise ExtractionError(_RETRIEVAL_ERROR) from error

    @staticmethod
    def _decode_json(body: bytes, error_message: str) -> dict:
        try:
            payload = json.loads(body)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExtractionError(error_message) from error
        if not isinstance(payload, dict):
            raise ExtractionError(error_message)
        return payload

    def _resolve_native_attachment(
        self,
        page_id: str,
        block: NormalizedBlock,
        token: str,
        before_external_call: Callable[[], None] | None,
        remaining_attachment_bytes: int,
    ) -> tuple[NormalizedBlock, int]:
        filename = block.data.get("attachment")
        if block.kind is not BlockKind.IMAGE or not isinstance(filename, str):
            return block, 0
        if remaining_attachment_bytes <= 0:
            raise ExtractionError(_ATTACHMENT_BUDGET_ERROR)
        assert self._api_base is not None
        metadata_body = self._get_bytes(
            f"{self._api_base}/content/{page_id}/child/attachment",
            token,
            params={"filename": filename, "limit": 2},
            before_external_call=before_external_call,
            not_found_message=_ATTACHMENT_NOT_FOUND_ERROR,
        )
        metadata = self._decode_json(metadata_body, _ATTACHMENT_RESPONSE_ERROR)
        results = metadata.get("results")
        if not isinstance(results, list):
            raise ExtractionError(_ATTACHMENT_RESPONSE_ERROR)
        matching = [result for result in results if isinstance(result, dict) and result.get("title") == filename]
        if not matching:
            raise ExtractionError(_ATTACHMENT_NOT_FOUND_ERROR)
        if len(matching) != 1:
            raise ExtractionError(_ATTACHMENT_RESPONSE_ERROR)
        attachment = matching[0]
        try:
            media_type = attachment["metadata"]["mediaType"]
            download_link = attachment["_links"]["download"]
        except (KeyError, TypeError) as error:
            raise ExtractionError(_ATTACHMENT_RESPONSE_ERROR) from error
        if (
            not isinstance(media_type, str)
            or not media_type.startswith("image/")
            or not isinstance(download_link, str)
        ):
            raise ExtractionError(_ATTACHMENT_RESPONSE_ERROR)

        download_url = self._same_host_download_url(download_link)
        download_limit = min(self._max_response_bytes, remaining_attachment_bytes)
        oversized_message = (
            _ATTACHMENT_BUDGET_ERROR
            if remaining_attachment_bytes < self._max_response_bytes
            else _OVERSIZED_ERROR
        )
        content = self._get_bytes(
            download_url,
            token,
            before_external_call=before_external_call,
            not_found_message=_ATTACHMENT_NOT_FOUND_ERROR,
            max_bytes=download_limit,
            oversized_message=oversized_message,
        )
        if not content:
            raise ExtractionError(_ATTACHMENT_RESPONSE_ERROR)
        if len(content) > remaining_attachment_bytes:
            raise ExtractionError(_ATTACHMENT_BUDGET_ERROR)
        return (
            block.model_copy(
                update={
                    "data": {
                        **block.data,
                        "content_base64": base64.b64encode(content).decode("ascii"),
                        "media_type": media_type,
                    }
                }
            ),
            len(content),
        )

    def _same_host_download_url(self, download_link: str) -> str:
        assert self._api_base is not None
        try:
            download_url = urljoin(f"{self._api_base}/", download_link)
            api = urlsplit(self._api_base)
            parsed = urlsplit(download_url)
            _ = api.port
            _ = parsed.port
        except (TypeError, ValueError):
            raise ExtractionError(_ATTACHMENT_URL_ERROR) from None
        if (
            parsed.scheme != api.scheme
            or parsed.netloc != api.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ExtractionError(_ATTACHMENT_URL_ERROR)
        return download_url

    @staticmethod
    def _check_content_length(
        response: httpx.Response,
        max_bytes: int,
        oversized_message: str,
    ) -> None:
        content_length = response.headers.get("Content-Length")
        if content_length is None:
            return
        try:
            is_oversized = int(content_length) > max_bytes
        except ValueError:
            return
        if is_oversized:
            raise ExtractionError(oversized_message)

    @staticmethod
    def _read_limited_body(
        response: httpx.Response,
        max_bytes: int,
        oversized_message: str,
    ) -> bytes:
        chunks: list[bytes] = []
        total_bytes = 0
        for chunk in response.iter_bytes():
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise ExtractionError(oversized_message)
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
    for element in soup.find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table", "img", "ac:image"]
    ):
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
    if element.name == "ac:image":
        attachment = element.find("ri:attachment")
        filename = attachment.get("ri:filename") if attachment is not None else None
        if not isinstance(filename, str):
            return None
        return _make_block(
            source_id,
            BlockKind.IMAGE,
            "image",
            filename,
            {"src": f"attachment:{filename}", "alt": "", "attachment": filename},
            counters,
        )
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
