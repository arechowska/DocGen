import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates

from docgen.documents.style import node_style_attribute

_package_directory = Path(__file__).parent
templates = Jinja2Templates(directory=_package_directory / "templates")
templates.env.globals["node_style_attribute"] = node_style_attribute
static_directory = _package_directory / "static"

# Appended as a `?v=` query string on first-party static asset URLs. Deriving
# it from both files prevents a deploy that only changes JavaScript from
# leaving a browser or reverse proxy on an older editor implementation.
_versioned_assets = (
    _package_directory / "static/css/docgen.css",
    _package_directory / "static/js/docgen2-editor.js",
)
_asset_digest = hashlib.sha256()
for _asset_path in _versioned_assets:
    _asset_digest.update(_asset_path.read_bytes())
static_asset_version = _asset_digest.hexdigest()[:12]
templates.env.globals["static_asset_version"] = static_asset_version

__all__ = ["static_directory", "templates"]
