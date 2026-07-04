"""Jinja2 template loading for HTML and plain-text email bodies."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=lambda template_name: template_name == "email.html.j2",
    trim_blocks=True,
    lstrip_blocks=True,
)

_HTML_TEMPLATE = _env.get_template("email.html.j2")
_TEXT_TEMPLATE = _env.get_template("email.txt.j2")


def render_html_template(**context: object) -> str:
    return _HTML_TEMPLATE.render(**context)


def render_text_template(**context: object) -> str:
    return _TEXT_TEMPLATE.render(**context)
