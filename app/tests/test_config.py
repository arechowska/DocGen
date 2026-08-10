from __future__ import annotations

import pytest
from pydantic import ValidationError

from docgen.config import Settings


def test_resource_and_lease_defaults_are_conservative() -> None:
    settings = Settings(_env_file=None)

    assert settings.trusted_integration_hosts == ("localhost", "127.0.0.1", "::1")
    assert settings.worker_lease_seconds == 30
    assert settings.max_upload_bytes == 52_428_800
    assert settings.max_project_storage_bytes == 524_288_000
    assert settings.max_image_pixels == 40_000_000
    assert settings.max_archive_entries == 10_000
    assert settings.max_archive_uncompressed_bytes == 209_715_200
    assert settings.max_model_request_bytes == 20_971_520
    assert settings.max_job_seconds == 300


def test_trusted_hosts_are_loaded_from_json_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DOCGEN_TRUSTED_INTEGRATION_HOSTS",
        '["model.internal", "wiki.internal"]',
    )

    settings = Settings(_env_file=None)

    assert settings.trusted_integration_hosts == ("model.internal", "wiki.internal")


@pytest.mark.parametrize(
    "field_name",
    [
        "worker_lease_seconds",
        "max_upload_bytes",
        "max_project_storage_bytes",
        "max_image_pixels",
        "max_archive_entries",
        "max_archive_uncompressed_bytes",
        "max_model_request_bytes",
        "max_job_seconds",
    ],
)
def test_resource_limits_must_be_positive(field_name: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field_name: 0})
