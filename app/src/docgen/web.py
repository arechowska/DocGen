from pathlib import Path

from fastapi.templating import Jinja2Templates

from docgen.documents.style import node_style_attribute

_package_directory = Path(__file__).parent
templates = Jinja2Templates(directory=_package_directory / "templates")
templates.env.globals["node_style_attribute"] = node_style_attribute
static_directory = _package_directory / "static"

__all__ = ["static_directory", "templates"]
