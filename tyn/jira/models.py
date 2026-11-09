"""Typed views of the Jira responses this package relies on.

Unknown fields are ignored so additive API changes do not break parsing, while
missing required fields fail loudly instead of producing silent gaps.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _JiraModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)


class IssueRef(_JiraModel):
    """An issue as returned by search or get-issue."""

    id: str
    key: str
    fields: dict[str, Any] = Field(default_factory=dict)


class SearchPage(_JiraModel):
    """One page of ``/rest/api/3/search/jql`` results."""

    issues: list[IssueRef] = Field(default_factory=list)
    next_page_token: str | None = Field(default=None, alias="nextPageToken")
    is_last: bool | None = Field(default=None, alias="isLast")


class JiraComment(_JiraModel):
    """A comment with its ADF body."""

    id: str
    body: dict[str, Any] | None = None
    created: str | None = None


class CommentPage(_JiraModel):
    """One page of comments for an issue."""

    comments: list[JiraComment] = Field(default_factory=list)
    start_at: int = Field(default=0, alias="startAt")
    max_results: int = Field(default=0, alias="maxResults")
    total: int = 0


class LinkType(_JiraModel):
    """An issue link type with its inward and outward phrases."""

    id: str
    name: str
    inward: str
    outward: str


class IssueTypeMeta(_JiraModel):
    """An issue type available for creation in a project."""

    id: str
    name: str
    subtask: bool = False


class CreatedIssue(_JiraModel):
    """Identifiers returned when an issue is created."""

    id: str
    key: str


class LinkedIssueRef(_JiraModel):
    """The issue on the other end of a link."""

    id: str | None = None
    key: str


class IssueLink(_JiraModel):
    """A link as seen from the issue that was fetched.

    ``outward_issue`` set means "this issue <type.outward> outward_issue";
    ``inward_issue`` set means "this issue <type.inward> inward_issue".
    """

    id: str
    type: LinkType
    inward_issue: LinkedIssueRef | None = Field(default=None, alias="inwardIssue")
    outward_issue: LinkedIssueRef | None = Field(default=None, alias="outwardIssue")
