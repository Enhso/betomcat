"""A tiny document model rendered as plain HTML and as plain text.

The HTML is tables and one heading per section with inline styles only, so
it reads on a phone and survives mail clients that drop `<style>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

UNAVAILABLE = "unavailable"

_FONT = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
_BODY = f"margin:0;padding:16px;background:#ffffff;color:#111111;font-family:{_FONT}"
_WRAP = "max-width:640px;margin:0 auto;font-size:16px;line-height:1.45"
_H1 = "font-size:21px;margin:0 0 8px"
_H2 = (
    "font-size:17px;margin:22px 0 8px;padding-bottom:4px;"
    "border-bottom:1px solid #cccccc"
)
_P = "margin:6px 0"
_TABLE = "border-collapse:collapse;width:100%;font-size:15px;margin:6px 0"
_TH = "text-align:left;padding:4px 10px 4px 0;border-bottom:2px solid #999999"
_TD = "padding:4px 10px 4px 0;border-bottom:1px solid #dddddd;vertical-align:top"


@dataclass(frozen=True)
class Section:
    """One headed block: optional paragraphs, then an optional table."""

    title: str
    notes: tuple[str, ...] = ()
    header: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()


def unavailable(title: str) -> Section:
    """A section whose source failed."""
    return Section(title, notes=(UNAVAILABLE,))


def _html_table(section: Section) -> str:
    head = "".join(f'<th style="{_TH}">{escape(h)}</th>' for h in section.header)
    body = "".join(
        "<tr>"
        + "".join(f'<td style="{_TD}">{escape(cell)}</td>' for cell in row)
        + "</tr>"
        for row in section.rows
    )
    thead = f"<thead><tr>{head}</tr></thead>" if head else ""
    return f'<table style="{_TABLE}">{thead}<tbody>{body}</tbody></table>'


def render_html(title: str, sections: list[Section]) -> str:
    """The sections as a minimal, phone-readable HTML page."""
    blocks = [f'<h1 style="{_H1}">{escape(title)}</h1>']
    for section in sections:
        blocks.append(f'<h2 style="{_H2}">{escape(section.title)}</h2>')
        blocks.extend(f'<p style="{_P}">{escape(note)}</p>' for note in section.notes)
        if section.rows:
            blocks.append(_html_table(section))
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title></head>"
        f'<body style="{_BODY}"><div style="{_WRAP}">{"".join(blocks)}</div>'
        "</body></html>"
    )


def render_text(title: str, sections: list[Section]) -> str:
    """The sections as plain text: `==` headings, `|`-separated table rows."""
    lines = [title]
    for section in sections:
        lines += ["", f"== {section.title} =="]
        lines.extend(section.notes)
        if section.rows:
            if section.header:
                lines.append(" | ".join(section.header))
            lines.extend(" | ".join(row) for row in section.rows)
    return "\n".join(lines) + "\n"
