from __future__ import annotations

import re
from typing import Any

from markupsafe import Markup, escape

STYLE_ALIASES = {
    "background_color": "background-color",
    "font_family": "font-family",
    "font_size": "font-size",
    "font_style": "font-style",
    "font_weight": "font-weight",
    "line_height": "line-height",
    "margin_bottom": "margin-bottom",
    "margin_left": "margin-left",
    "margin_right": "margin-right",
    "margin_top": "margin-top",
    "text_align": "text-align",
    "text_decoration": "text-decoration",
    "text_indent": "text-indent",
}
ALLOWED_STYLE_PROPERTIES = (
    "color",
    "background-color",
    "font-family",
    "font-size",
    "font-style",
    "font-weight",
    "line-height",
    "margin-bottom",
    "margin-left",
    "margin-right",
    "margin-top",
    "text-align",
    "text-decoration",
    "text-indent",
)
STYLE_VALUE_PATTERN = re.compile(r"^[#%(),.0-9A-Za-z _+-]+$")


def normalized_style(data: dict[str, Any]) -> dict[str, str]:
    style: dict[str, str] = {}
    raw_style = data.get("style")
    if isinstance(raw_style, dict):
        style.update(_normalized_style_items(raw_style))
    style.update(_normalized_style_items(data))
    if data.get("bold") is True:
        style["font-weight"] = "700"
    return style


def normalized_style_attribute(value: str) -> str:
    return _serialized_style(
        _normalized_style_items(
            dict(_style_declarations(value)),
        )
    )


def node_style_attribute(node: Any) -> Markup:
    style = _serialized_style(normalized_style(getattr(node, "data", {}) or {}))
    if not style:
        return Markup("")
    return Markup(f' style="{escape(style)}"')


def _normalized_style_items(items: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_property, raw_value in items.items():
        property_name = STYLE_ALIASES.get(str(raw_property), str(raw_property)).strip().lower()
        if property_name not in ALLOWED_STYLE_PROPERTIES:
            continue
        if raw_value is None or isinstance(raw_value, bool):
            continue
        value = str(raw_value).strip()
        if not value or not STYLE_VALUE_PATTERN.match(value):
            continue
        result[property_name] = value
    return result


def _style_declarations(value: str):
    for declaration in value.split(";"):
        if ":" not in declaration:
            continue
        property_name, property_value = declaration.split(":", 1)
        yield property_name.strip(), property_value.strip()


def _serialized_style(style: dict[str, str]) -> str:
    return ";".join(
        f"{property_name}:{style[property_name]}"
        for property_name in ALLOWED_STYLE_PROPERTIES
        if property_name in style
    )
