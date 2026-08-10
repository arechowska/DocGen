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


def _native_attachment_transport(storage_html: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/content/42":
            return _page_response(storage_html)
        if request.url.path == "/rest/api/content/42/child/attachment":
            filename = request.url.params["filename"]
            media_type = "image/svg+xml" if filename.endswith(".svg") else "image/png"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": filename,
                            "metadata": {"mediaType": media_type},
                            "_links": {"download": f"/download/attachments/42/{filename}"},
                        }
                    ]
                },
            )
        filename = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=filename.encode())

    return httpx.MockTransport(handler)


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
    assert result.page_units == 2


def test_fetch_confluence_page_maps_native_attachment_image_macros() -> None:
    transport = _native_attachment_transport(
        '<ac:image><ri:attachment ri:filename="architecture.png" /></ac:image>'
        '<ac:image><ri:attachment ri:filename="flow.svg" /></ac:image>'
    )
    client = ConfluenceClient(
        api_base="https://wiki.example.test/rest/api",
        token="secret",
        transport=transport,
    )

    result = client.fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")

    assert [block.kind for block in result.blocks] == [BlockKind.IMAGE, BlockKind.IMAGE]
    assert [block.provenance[0].locator for block in result.blocks] == [
        "confluence:42#image-1",
        "confluence:42#image-2",
    ]
    assert result.blocks[0].data == {
        "src": "attachment:architecture.png",
        "alt": "",
        "attachment": "architecture.png",
        "content_base64": "YXJjaGl0ZWN0dXJlLnBuZw==",
        "media_type": "image/png",
    }
    assert result.blocks[0].text == "architecture.png"
    assert result.page_units == 3


def test_confluence_images_each_count_as_one_virtual_page() -> None:
    exact_text_page = "a" * 1800
    transport = _native_attachment_transport(
        f"<p>{exact_text_page}</p>"
        '<ac:image><ri:attachment ri:filename="one.png" /></ac:image>'
        '<ac:image><ri:attachment ri:filename="two.png" /></ac:image>'
    )
    client = ConfluenceClient(
        api_base="https://wiki.example.test/rest/api",
        token="secret",
        transport=transport,
    )

    result = client.fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")

    assert result.page_units == 3


def test_empty_confluence_content_has_one_virtual_text_page() -> None:
    transport = httpx.MockTransport(lambda request: _page_response("<p> \n\t </p>"))
    client = ConfluenceClient(
        api_base="https://wiki.example.test/rest/api",
        token="secret",
        transport=transport,
    )

    result = client.fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")

    assert result.blocks == []
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
        trusted_integration_hosts=("wiki.example.test",),
    )

    result = ConfluenceClient.from_settings(
        settings,
        transport=httpx.MockTransport(handler),
    ).fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")

    assert result.blocks[0].text == "Configured"
    assert observed_timeouts == [{"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}]


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://public.example/rest/api",
        "https://user:password@wiki.internal/rest/api",
        "https://wiki.internal/rest/api#fragment",
        "ftp://wiki.internal/rest/api",
    ],
)
def test_client_from_settings_rejects_untrusted_or_unsafe_api_endpoint(
    endpoint: str,
) -> None:
    settings = Settings(
        confluence_api_base=endpoint,
        confluence_token="configured-secret",
        confluence_hosts=("pages.internal",),
        trusted_integration_hosts=("wiki.internal",),
    )

    with pytest.raises(
        ExtractionError,
        match="Адрес API Confluence не разрешён настройками",
    ):
        ConfluenceClient.from_settings(settings)


def test_confluence_page_and_api_hosts_have_separate_allowlists() -> None:
    settings = Settings(
        confluence_api_base="https://api.internal/rest/api",
        confluence_token="configured-secret",
        confluence_hosts=("pages.internal",),
        trusted_integration_hosts=("api.internal",),
    )
    transport = httpx.MockTransport(lambda request: _page_response("<p>Allowed</p>"))
    client = ConfluenceClient.from_settings(settings, transport=transport)

    result = client.fetch("https://pages.internal/pages/viewpage.action?pageId=42")

    assert result.blocks[0].text == "Allowed"


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


def test_native_attachment_is_resolved_and_downloaded_with_gate_before_each_request() -> None:
    events: list[str] = []
    observed_timeouts: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        observed_timeouts.append(request.extensions["timeout"])
        if request.url.path == "/rest/api/content/42":
            events.append("page")
            return _page_response(
                '<ac:image><ri:attachment ri:filename="architecture.png" /></ac:image>'
            )
        if request.url.path == "/rest/api/content/42/child/attachment":
            events.append("metadata")
            assert request.url.params == httpx.QueryParams(
                "filename=architecture.png&limit=2"
            )
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "architecture.png",
                            "metadata": {"mediaType": "image/png"},
                            "_links": {
                                "download": "/download/attachments/42/architecture.png?version=1"
                            },
                        }
                    ]
                },
            )
        assert request.url.path == "/download/attachments/42/architecture.png"
        events.append("download")
        return httpx.Response(200, content=b"native-image")

    client = ConfluenceClient(
        api_base="https://wiki.example.test/rest/api",
        token="secret",
        transport=httpx.MockTransport(handler),
    )

    result = client.fetch(
        "https://wiki.example.test/pages/viewpage.action?pageId=42",
        before_external_call=lambda: events.append("gate"),
    )

    assert events == ["gate", "page", "gate", "metadata", "gate", "download"]
    assert observed_timeouts == [
        {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0},
        {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0},
        {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0},
    ]
    assert result.blocks[0].data == {
        "src": "attachment:architecture.png",
        "alt": "",
        "attachment": "architecture.png",
        "content_base64": "bmF0aXZlLWltYWdl",
        "media_type": "image/png",
    }


@pytest.mark.parametrize(
    ("metadata_payload", "message"),
    [
        ({"results": []}, "Вложение Confluence не найдено"),
        (
            {"results": [{"title": "architecture.png", "metadata": {}}]},
            "Не удалось обработать вложение Confluence",
        ),
    ],
)
def test_native_attachment_missing_or_malformed_metadata_is_user_safe(
    metadata_payload: dict,
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/content/42":
            return _page_response(
                '<ac:image><ri:attachment ri:filename="architecture.png" /></ac:image>'
            )
        return httpx.Response(200, json=metadata_payload)

    with pytest.raises(ExtractionError, match=message):
        ConfluenceClient(
            api_base="https://wiki.example.test/rest/api",
            token="secret",
            transport=httpx.MockTransport(handler),
        ).fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")


def test_oversized_native_attachment_download_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/content/42":
            return _page_response(
                '<ac:image><ri:attachment ri:filename="architecture.png" /></ac:image>'
            )
        if request.url.path.endswith("/child/attachment"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "architecture.png",
                            "metadata": {"mediaType": "image/png"},
                            "_links": {"download": "/download/attachments/42/architecture.png"},
                        }
                    ]
                },
            )
        return httpx.Response(200, headers={"Content-Length": "5000001"}, content=b"x")

    with pytest.raises(ExtractionError, match="Ответ Confluence слишком большой"):
        ConfluenceClient(
            api_base="https://wiki.example.test/rest/api",
            token="secret",
            transport=httpx.MockTransport(handler),
        ).fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")


def test_native_attachment_rejects_external_download_link_without_requesting_it() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if request.url.path == "/rest/api/content/42":
            return _page_response(
                '<ac:image><ri:attachment ri:filename="architecture.png" /></ac:image>'
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "architecture.png",
                        "metadata": {"mediaType": "image/png"},
                        "_links": {"download": "https://outside.example.test/image.png"},
                    }
                ]
            },
        )

    with pytest.raises(ExtractionError, match="Недопустимая ссылка вложения Confluence"):
        ConfluenceClient(
            api_base="https://wiki.example.test/rest/api",
            token="secret",
            transport=httpx.MockTransport(handler),
        ).fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")

    assert requested_hosts == ["wiki.example.test", "wiki.example.test"]


def test_confluence_rejects_over_150_page_units_before_attachment_requests() -> None:
    native_images = "".join(
        f'<ac:image><ri:attachment ri:filename="image-{index}.png" /></ac:image>'
        for index in range(150)
    )
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/rest/api/content/42":
            return _page_response(native_images)
        raise AssertionError("page limit must be checked before attachment metadata/download")

    with pytest.raises(ExtractionError, match="Максимальный объём — 150 страниц"):
        ConfluenceClient(
            api_base="https://wiki.example.test/rest/api",
            token="secret",
            transport=httpx.MockTransport(handler),
        ).fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")

    assert requested_paths == ["/rest/api/content/42"]


def test_confluence_enforces_aggregate_attachment_budget_before_retaining_second_image() -> None:
    storage_html = (
        '<ac:image><ri:attachment ri:filename="one.png" /></ac:image>'
        '<ac:image><ri:attachment ri:filename="two.png" /></ac:image>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/content/42":
            return _page_response(storage_html)
        if request.url.path.endswith("/child/attachment"):
            filename = request.url.params["filename"]
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": filename,
                            "metadata": {"mediaType": "image/png"},
                            "_links": {"download": f"/download/attachments/42/{filename}"},
                        }
                    ]
                },
            )
        return httpx.Response(200, content=b"abc")

    with pytest.raises(
        ExtractionError,
        match="Общий объём вложений Confluence слишком большой",
    ):
        ConfluenceClient(
            api_base="https://wiki.example.test/rest/api",
            token="secret",
            transport=httpx.MockTransport(handler),
            max_response_bytes=10_000,
            max_attachment_bytes=5,
        ).fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")


def test_native_attachment_maps_malformed_download_url_to_safe_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/content/42":
            return _page_response(
                '<ac:image><ri:attachment ri:filename="architecture.png" /></ac:image>'
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "architecture.png",
                        "metadata": {"mediaType": "image/png"},
                        "_links": {"download": "https://[bad"},
                    }
                ]
            },
        )

    with pytest.raises(ExtractionError, match="Недопустимая ссылка вложения Confluence"):
        ConfluenceClient(
            api_base="https://wiki.example.test/rest/api",
            token="secret",
            transport=httpx.MockTransport(handler),
        ).fetch("https://wiki.example.test/pages/viewpage.action?pageId=42")
