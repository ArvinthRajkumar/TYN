"""Jira Cloud REST v3 HTTP access with bounded, idempotency-aware retries.

Retry rules:

* 429 is always retried: Jira rejected the request, so repeating it is safe.
  ``Retry-After`` is honoured as a minimum wait.
* Connection failures before anything was sent are always retried.
* 5xx and read timeouts are retried only for idempotent requests. For writes
  they raise ``AmbiguousWriteError`` so the caller can check Jira before
  trying again, because a blind retry could create a duplicate.
* Everything else fails immediately with ``JiraHttpError``.
"""

import logging
import random
import time
from collections.abc import Callable, Iterator
from typing import Any, Final

import httpx

from tyn.config import JiraSettings
from tyn.errors import AmbiguousWriteError, JiraError, JiraHttpError, JiraRetryExhaustedError
from tyn.jira.models import (
    CommentPage,
    CreatedIssue,
    IssueLink,
    IssueRef,
    IssueTypeMeta,
    JiraComment,
    LinkType,
    SearchPage,
)
from tyn.retry import RetryPolicy, parse_retry_after

logger = logging.getLogger(__name__)

API_PREFIX: Final = "/rest/api/3/"
_RETRYABLE_SERVER_ERRORS: Final = frozenset({500, 502, 503, 504})
_MAX_DETAIL_CHARS: Final = 300
# Hard ceilings so a misbehaving server cannot make pagination loop forever.
MAX_SEARCH_PAGES: Final = 100
MAX_COMMENT_PAGES: Final = 50

Sleeper = Callable[[float], None]


def build_http_client(settings: JiraSettings, timeout: float = 30.0) -> httpx.Client:
    """Create the authenticated HTTP client for one Jira site.

    TLS verification stays on (httpx default) and redirects are not followed,
    so credentials are never replayed to another host.
    """
    return httpx.Client(
        base_url=settings.jira_base_url,
        auth=httpx.BasicAuth(
            settings.jira_email.get_secret_value(), settings.jira_api_token.get_secret_value()
        ),
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers={"Accept": "application/json", "User-Agent": "tyn-release-assessment/0.1"},
        follow_redirects=False,
    )


def _error_detail(response: httpx.Response) -> str:
    """Return Jira's error messages without echoing request data."""
    try:
        payload = response.json()
    except ValueError:
        return response.reason_phrase or "no detail"
    messages: list[str] = []
    if isinstance(payload, dict):
        messages.extend(str(m) for m in payload.get("errorMessages", []) or [])
        errors = payload.get("errors") or {}
        if isinstance(errors, dict):
            messages.extend(f"{field}: {msg}" for field, msg in errors.items())
    text = "; ".join(messages) or response.reason_phrase or "no detail"
    return text[:_MAX_DETAIL_CHARS]


class JiraHttp:
    """Low-level request executor shared by the reader and the writer."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        policy: RetryPolicy | None = None,
        sleep: Sleeper = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client
        self._policy = policy or RetryPolicy()
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security

    def request(
        self,
        method: str,
        path: str,
        *,
        idempotent: bool,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """Send one logical request, retrying within the policy limits."""
        if not path.startswith(API_PREFIX):
            raise JiraError(f"refusing non REST v3 path {path}")
        policy = self._policy
        for attempt in range(1, policy.max_attempts + 1):
            retry_after: float | None = None
            try:
                response = self._client.request(method, path, params=params, json=json)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                reason = f"connection failed ({type(exc).__name__})"
            except httpx.TransportError as exc:
                if not idempotent:
                    raise AmbiguousWriteError(
                        f"Jira {method} {path} outcome unknown ({type(exc).__name__})"
                    ) from exc
                reason = f"transport error ({type(exc).__name__})"
            else:
                status = response.status_code
                if status < 400:
                    return response
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if status == 429:
                    if retry_after is not None and retry_after > policy.max_retry_after:
                        raise JiraRetryExhaustedError(
                            f"Jira rate limit asks to wait {retry_after:.0f}s, above the "
                            f"{policy.max_retry_after:.0f}s ceiling; re-run later to resume"
                        )
                    reason = f"rate limited ({response.headers.get('RateLimit-Reason', '429')})"
                elif status in _RETRYABLE_SERVER_ERRORS:
                    if not idempotent:
                        raise AmbiguousWriteError(
                            f"Jira {method} {path} outcome unknown (HTTP {status})"
                        )
                    reason = f"server error HTTP {status}"
                else:
                    raise JiraHttpError(method, path, status, _error_detail(response))
            if attempt == policy.max_attempts:
                raise JiraRetryExhaustedError(
                    f"Jira {method} {path} failed after {attempt} attempts: {reason}"
                )
            wait = policy.delay(attempt, self._rng, retry_after)
            logger.warning(
                "jira_retry method=%s path=%s attempt=%d reason=%s wait_s=%.1f",
                method,
                path,
                attempt,
                reason,
                wait,
            )
            self._sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover


class JiraReader:
    """Read-only Jira operations. Holds no method that changes Jira."""

    def __init__(self, http: JiraHttp) -> None:
        self._http = http

    def get_project(self, project_key: str) -> dict[str, Any]:
        """Return project metadata; fails if the project is not visible."""
        response = self._http.request("GET", f"{API_PREFIX}project/{project_key}", idempotent=True)
        data: dict[str, Any] = response.json()
        return data

    def search(
        self,
        jql: str,
        fields: list[str],
        *,
        max_results: int = 100,
        next_page_token: str | None = None,
    ) -> SearchPage:
        """Return one page from the enhanced JQL search endpoint."""
        body: dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": max_results}
        if next_page_token:
            body["nextPageToken"] = next_page_token
        # POST keeps long JQL out of URLs; the endpoint is read-only.
        response = self._http.request("POST", f"{API_PREFIX}search/jql", idempotent=True, json=body)
        return SearchPage.model_validate(response.json())

    def search_all(
        self, jql: str, fields: list[str], *, page_size: int = 100
    ) -> Iterator[IssueRef]:
        """Yield every issue matching ``jql``, with a hard page ceiling."""
        token: str | None = None
        for _ in range(MAX_SEARCH_PAGES):
            page = self.search(jql, fields, max_results=page_size, next_page_token=token)
            yield from page.issues
            token = page.next_page_token
            if not token or page.is_last:
                return
        raise JiraError(f"search exceeded {MAX_SEARCH_PAGES} pages; narrow the query")

    def get_issue(self, key: str, fields: list[str]) -> IssueRef:
        """Return one issue with the requested fields."""
        response = self._http.request(
            "GET",
            f"{API_PREFIX}issue/{key}",
            idempotent=True,
            params={"fields": ",".join(fields)},
        )
        return IssueRef.model_validate(response.json())

    def get_issue_links(self, key: str) -> list[IssueLink]:
        """Return the links of one issue as seen from that issue."""
        issue = self.get_issue(key, ["issuelinks"])
        raw_links = issue.fields.get("issuelinks") or []
        return [IssueLink.model_validate(item) for item in raw_links]

    def get_comments(self, key: str, *, start_at: int = 0, max_results: int = 100) -> CommentPage:
        """Return one page of comments ordered by Jira creation time."""
        response = self._http.request(
            "GET",
            f"{API_PREFIX}issue/{key}/comment",
            idempotent=True,
            params={"startAt": start_at, "maxResults": max_results, "orderBy": "created"},
        )
        return CommentPage.model_validate(response.json())

    def iter_comments(self, key: str, *, page_size: int = 100) -> Iterator[JiraComment]:
        """Yield all comments of an issue, with a hard page ceiling."""
        start = 0
        for _ in range(MAX_COMMENT_PAGES):
            page = self.get_comments(key, start_at=start, max_results=page_size)
            yield from page.comments
            start += len(page.comments)
            if not page.comments or start >= page.total:
                return
        raise JiraError(f"comments of {key} exceeded {MAX_COMMENT_PAGES} pages")

    def get_link_types(self) -> list[LinkType]:
        """Return all issue link types defined on the site."""
        response = self._http.request("GET", f"{API_PREFIX}issueLinkType", idempotent=True)
        return [LinkType.model_validate(item) for item in response.json().get("issueLinkTypes", [])]

    def get_create_issue_types(self, project_key: str) -> list[IssueTypeMeta]:
        """Return issue types the caller may create in ``project_key``."""
        response = self._http.request(
            "GET",
            f"{API_PREFIX}issue/createmeta/{project_key}/issuetypes",
            idempotent=True,
            params={"maxResults": 200},
        )
        payload = response.json()
        items = payload.get("issueTypes") or payload.get("createMetaIssueType") or []
        return [IssueTypeMeta.model_validate(item) for item in items]


class JiraWriter:
    """Write operations used only by the importer."""

    def __init__(self, http: JiraHttp) -> None:
        self._http = http

    def create_issue(self, fields: dict[str, Any]) -> CreatedIssue:
        """Create one issue and return its id and key."""
        response = self._http.request(
            "POST", f"{API_PREFIX}issue", idempotent=False, json={"fields": fields}
        )
        return CreatedIssue.model_validate(response.json())

    def add_comment(self, key: str, body: dict[str, Any]) -> str:
        """Add an ADF comment and return its id."""
        response = self._http.request(
            "POST", f"{API_PREFIX}issue/{key}/comment", idempotent=False, json={"body": body}
        )
        return str(response.json()["id"])

    def create_link(self, type_name: str, *, outward_key: str, inward_key: str) -> None:
        """Create an issue link.

        Jira documents a duplicate link request as a no-op success, so this
        call is safe to repeat; the importer still checks before creating.
        """
        self._http.request(
            "POST",
            f"{API_PREFIX}issueLink",
            idempotent=True,
            json={
                "type": {"name": type_name},
                "outwardIssue": {"key": outward_key},
                "inwardIssue": {"key": inward_key},
            },
        )

    def delete_link(self, link_id: str) -> None:
        """Delete one issue link (used only to correct a link this tool created)."""
        self._http.request("DELETE", f"{API_PREFIX}issueLink/{link_id}", idempotent=True)

    def create_link_type(self, name: str, *, inward: str, outward: str) -> LinkType:
        """Create an issue link type (needs the Administer Jira permission)."""
        response = self._http.request(
            "POST",
            f"{API_PREFIX}issueLinkType",
            idempotent=False,
            json={"name": name, "inward": inward, "outward": outward},
        )
        return LinkType.model_validate(response.json())
