from pathlib import Path

from fastapi.templating import Jinja2Templates

_package_directory = Path(__file__).parent
templates = Jinja2Templates(directory=_package_directory / "templates")
static_directory = _package_directory / "static"

__all__ = ["static_directory", "templates"]
