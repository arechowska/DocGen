import httpx
import pytest

from docgen.config import Settings
from docgen.extraction.confluence import ConfluenceClient
from docgen.extraction.registry import ExtractionError
from docgen.extraction.schemas import BlockKind


def _page_response(storage_html: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "42",
            "body": {"storage": {"value": storage_html}},
            "version": {"number": 3},
        },
    )


@pytest.fixture
def mock_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/content/42"
        assert request.url.params == httpx.QueryParams("expand=body.storage,version")
        assert request.headers["Authorization"] == "Bearer secret"
        return _page_response(
            "<h1>Guide</h1><p>Intro text</p><ul><li>First</li><li>Second</li></ul>"
            "<table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>B</td></tr></table>"
            '<img src="/download/attachments/42/diagram.png" alt="Diagram">'
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def mock_401_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(401, json={"message": "no access"}))


@pytest.fixture
def mock_oversized_transport() -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Length": "5000001"},
            content=b"x",
        )
    )


def test_fetch_confluence_page_maps_storage_html_to_normalized_blocks(
    mock_transport: httpx.MockTransport,
) -> None:
    client = ConfluenceClient(
        api_base="https://wiki.example.test/rest/api",
        token="secret",
        transport=mock_transport,
    )

    result = client.fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")

    assert [block.kind for block in result.blocks] == [
        BlockKind.HEADING,
        BlockKind.TEXT,
        BlockKind.LIST,
        BlockKind.TABLE,
        BlockKind.IMAGE,
    ]
    assert [block.provenance[0].locator for block in result.blocks] == [
        "confluence:42#heading-1",
        "confluence:42#paragraph-1",
        "confluence:42#list-1",
        "confluence:42#table-1",
        "confluence:42#image-1",
    ]
    assert result.blocks[0].data == {"level": 1}
    assert result.blocks[2].data == {"ordered": False, "items": ["First", "Second"]}
    assert result.blocks[3].data == {"rows": [["Name", "Value"], ["A", "B"]]}
    assert result.blocks[4].data == {
        "src": "/download/attachments/42/diagram.png",
        "alt": "Diagram",
    }
    assert result.blocks[0].provenance[0].source_id == "confluence:42"
    assert result.page_units == 1


def test_fetch_uses_stable_ids_and_counts_virtual_pages() -> None:
    exact_page_text = "a" * 1800
    long_text = "a" * 1801
    responses = iter(
        [
            _page_response(f"<p>{exact_page_text}</p>"),
            _page_response(f"<p>{long_text}</p>"),
            _page_response(f"<p>{long_text}</p>"),
        ]
    )
    transport = httpx.MockTransport(lambda request: next(responses))
    client = ConfluenceClient(
        api_base="https://wiki.example.test/rest/api",
        token="secret",
        transport=transport,
    )

    exact_page = client.fetch("https://wiki.example.test/pages/42/Guide")
    first = client.fetch("https://wiki.example.test/pages/42/Guide")
    second = client.fetch("https://wiki.example.test/pages/42/Guide")

    assert exact_page.page_units == 1
    assert first.page_units == second.page_units == 2
    assert [block.id for block in first.blocks] == [block.id for block in second.blocks]


def test_unauthorized_is_user_safe(mock_401_transport: httpx.MockTransport) -> None:
    with pytest.raises(ExtractionError, match="Нет доступа к странице Confluence"):
        ConfluenceClient(
            api_base="https://wiki.example.test/rest/api",
            token="secret",
            transport=mock_401_transport,
        ).fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")


def test_oversized_confluence_response_is_rejected(
    mock_oversized_transport: httpx.MockTransport,
) -> None:
    with pytest.raises(ExtractionError, match="Ответ Confluence слишком большой"):
        ConfluenceClient(
            api_base="https://wiki.example.test/rest/api",
            token="secret",
            transport=mock_oversized_transport,
            max_response_bytes=5_000_000,
        ).fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")


def test_streamed_response_over_default_limit_is_rejected_without_content_length() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=httpx.ByteStream(b"x" * 5_000_001))
    )

    with pytest.raises(ExtractionError, match="Ответ Confluence слишком большой"):
        ConfluenceClient(
            api_base="https://wiki.example.test/rest/api",
            token="secret",
            transport=transport,
        ).fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")


def test_unconfigured_client_fails_before_sending_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an unconfigured client must not send HTTP requests")

    with pytest.raises(ExtractionError, match="Интеграция Confluence не настроена"):
        ConfluenceClient(transport=httpx.MockTransport(handler)).fetch(
            "https://wiki.example.test/pages/viewpage.action?pageId=42"
        )


def test_client_rejects_page_url_outside_allowed_hosts() -> None:
    client = ConfluenceClient(
        api_base="https://wiki.example.test/rest/api",
        token="secret",
        allowed_hosts=("wiki.example.test",),
        transport=httpx.MockTransport(lambda request: _page_response("<p>unexpected</p>")),
    )

    with pytest.raises(ExtractionError, match="Разрешены только ссылки Confluence"):
        client.fetch("https://outside.example.test/pages/viewpage.action?pageId=42")


def test_client_from_settings_sends_secret_and_uses_30_second_timeout() -> None:
    observed_timeouts: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_timeouts.append(request.extensions["timeout"])
        assert request.headers["Authorization"] == "Bearer configured-secret"
        return _page_response("<p>Configured</p>")

    settings = Settings(
        confluence_api_base="https://wiki.example.test/rest/api",
        confluence_token="configured-secret",
        confluence_hosts=("wiki.example.test",),
    )

    result = ConfluenceClient.from_settings(
        settings,
        transport=httpx.MockTransport(handler),
    ).fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")

    assert result.blocks[0].text == "Configured"
    assert observed_timeouts == [{"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}]


def test_client_rejects_credential_bearing_page_url() -> None:
    client = ConfluenceClient(
        api_base="https://wiki.example.test/rest/api",
        token="secret",
        transport=httpx.MockTransport(lambda request: _page_response("<p>unexpected</p>")),
    )

    with pytest.raises(ExtractionError, match="Разрешены только ссылки Confluence"):
        client.fetch("https://user:password@wiki.example.test/pages/viewpage.action?pageId=42")


def test_settings_allow_confluence_to_be_optional_at_startup() -> None:
    settings = Settings(confluence_api_base=None, confluence_token=None)

    assert settings.confluence_api_base is None
    assert settings.confluence_token is None
