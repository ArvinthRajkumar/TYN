"""Conversion between plain text and Atlassian Document Format (ADF).

Each source line becomes its own paragraph so the original line structure
survives a round trip exactly; ``adf_to_text(text_to_adf(t)) == t``.
"""

from typing import Any, Final

from tyn.errors import DatasetError

# Jira rejects very large rich-text bodies. Evidence is never truncated, so an
# oversized body is reported as an error instead of being cut.
MAX_BODY_CHARS: Final = 32_000

_BLOCK_TYPES: Final = frozenset(
    {
        "paragraph",
        "heading",
        "blockquote",
        "codeBlock",
        "listItem",
        "bulletList",
        "orderedList",
        "panel",
        "rule",
        "table",
        "tableRow",
        "tableCell",
        "tableHeader",
        "mediaSingle",
    }
)


def text_to_adf(text: str) -> dict[str, Any]:
    """Return an ADF document with one paragraph per line of ``text``."""
    if len(text) > MAX_BODY_CHARS:
        raise DatasetError(f"text of {len(text)} characters exceeds the {MAX_BODY_CHARS} limit")
    paragraphs: list[dict[str, Any]] = []
    for line in text.split("\n"):
        content = [{"type": "text", "text": line}] if line else []
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs}


def adf_to_text(node: dict[str, Any] | None) -> str:
    """Flatten an ADF node to text, one line per block."""
    if not node:
        return ""
    if node.get("type") == "doc":
        return "\n".join(_block_text(child) for child in node.get("content", []))
    return _block_text(node)


def _block_text(node: dict[str, Any]) -> str:
    node_type = node.get("type")
    if node_type == "text":
        return str(node.get("text", ""))
    if node_type == "hardBreak":
        return "\n"
    if node_type in ("mention", "emoji", "status", "date", "inlineCard"):
        attrs = node.get("attrs", {})
        return str(attrs.get("text") or attrs.get("url") or attrs.get("timestamp") or "")
    children = node.get("content", [])
    if any(child.get("type") in _BLOCK_TYPES for child in children):
        return "\n".join(_block_text(child) for child in children)
    return "".join(_block_text(child) for child in children)
