from pathlib import Path

from fastapi.templating import Jinja2Templates

from docgen.documents.style import node_style_attribute

_package_directory = Path(__file__).parent
templates = Jinja2Templates(directory=_package_directory / "templates")
templates.env.globals["node_style_attribute"] = node_style_attribute
static_directory = _package_directory / "static"

# Appended as a `?v=` query string on static asset URLs (see base.html) so a
# deploy that changes docgen.css invalidates browsers' cached copies instead
# of leaving old CSS rules paired against new template markup -- the exact
# mismatch that made the topbar collapse when its column count changed but a
# cached stylesheet still described the old, wider column layout.
static_asset_version = str(int((_package_directory / "static/css/docgen.css").stat().st_mtime))
templates.env.globals["static_asset_version"] = static_asset_version

__all__ = ["static_directory", "templates"]
