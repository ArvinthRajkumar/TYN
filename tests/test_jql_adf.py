"""JQL escaping and ADF conversion."""

import pytest

from tyn.errors import DatasetError
from tyn.jira import jql
from tyn.jira.adf import MAX_BODY_CHARS, adf_to_text, text_to_adf


def test_quote_escapes_quotes_and_backslashes() -> None:
    assert jql.quote('a"b\\c') == '"a\\"b\\\\c"'


@pytest.mark.parametrize("value", ["a\nb", "x\x00", "tab\there"])
def test_quote_rejects_control_characters(value: str) -> None:
    with pytest.raises(jql.JqlValueError):
        jql.quote(value)


def test_quote_rejects_overlong_values() -> None:
    with pytest.raises(jql.JqlValueError):
        jql.quote("x" * 201)


def test_injection_attempt_stays_inside_literal() -> None:
    hostile = 'scope" OR project != "X'
    clause = jql.scoped("TYN", hostile)
    assert clause == 'project = "TYN" AND labels = "scope\\" OR project != \\"X"'


def test_scoped_always_prefixes_project_and_label() -> None:
    clause = jql.scoped("TYN", "scope", jql.label_in(["a", "b"]))
    assert clause.startswith('project = "TYN" AND labels = "scope" AND (')
    assert clause.endswith('(labels in ("a", "b"))')


def test_label_in_requires_values() -> None:
    with pytest.raises(jql.JqlValueError):
        jql.label_in([])


@pytest.mark.parametrize(
    "text",
    ["one line", "a\nb\n\nc", "", "trailing\n", "café → ok", "\n\nleading"],
)
def test_adf_round_trip_is_exact(text: str) -> None:
    assert adf_to_text(text_to_adf(text)) == text


def test_adf_never_emits_empty_text_nodes() -> None:
    doc = text_to_adf("a\n\nb")
    for paragraph in doc["content"]:
        for node in paragraph["content"]:
            assert node["text"]


def test_adf_rejects_oversized_text() -> None:
    with pytest.raises(DatasetError):
        text_to_adf("x" * (MAX_BODY_CHARS + 1))


def test_adf_to_text_handles_rich_nodes() -> None:
    doc = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "b"},
                ],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "i"}]}
                        ],
                    }
                ],
            },
        ],
    }
    assert adf_to_text(doc) == "a\nb\ni"
    assert adf_to_text(None) == ""
