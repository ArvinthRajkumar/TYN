"""Jira HTTP retry rules, error handling and pagination."""

import httpx
import pytest

from tests.fakes import FakeJira, Fault
from tyn.errors import AmbiguousWriteError, JiraError, JiraHttpError, JiraRetryExhaustedError
from tyn.jira.adf import text_to_adf
from tyn.jira.client import JiraHttp, JiraReader, JiraWriter, build_http_client
from tyn.retry import RetryPolicy

COMMENT_PATH = r"/rest/api/3/issue/TYN-\d+/comment"


def test_rate_limit_is_retried_with_retry_after() -> None:
    fake = FakeJira()
    key = fake.add_issue(["scope"])
    fake.faults.append(
        Fault("POST", COMMENT_PATH, status=429, headers={"Retry-After": "9"}, times=2)
    )
    sleeps: list[float] = []
    JiraWriter(fake.http(sleeps)).add_comment(key, text_to_adf("hello"))
    assert len(fake.comments[key]) == 1
    assert len(sleeps) == 2
    assert all(s >= 9 for s in sleeps)


def test_rate_limit_wait_above_ceiling_stops() -> None:
    fake = FakeJira()
    fake.faults.append(
        Fault("GET", r"/rest/api/3/issueLinkType", status=429, headers={"Retry-After": "3600"})
    )
    with pytest.raises(JiraRetryExhaustedError, match="re-run later"):
        fake.reader().get_link_types()


def test_server_error_on_read_is_retried() -> None:
    fake = FakeJira()
    fake.faults.append(Fault("GET", r"/rest/api/3/issueLinkType", status=503, times=2))
    sleeps: list[float] = []
    assert fake.reader(sleeps).get_link_types()
    assert len(sleeps) == 2


def test_server_error_on_write_is_ambiguous_and_not_retried() -> None:
    fake = FakeJira()
    key = fake.add_issue(["scope"])
    fake.faults.append(Fault("POST", COMMENT_PATH, status=500, apply_first=True))
    with pytest.raises(AmbiguousWriteError):
        fake.writer().add_comment(key, text_to_adf("hello"))
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert len(posts) == 1


def test_read_timeout_on_write_is_ambiguous() -> None:
    fake = FakeJira()
    key = fake.add_issue(["scope"])
    fake.faults.append(Fault("POST", COMMENT_PATH, exception=httpx.ReadTimeout))
    with pytest.raises(AmbiguousWriteError):
        fake.writer().add_comment(key, text_to_adf("hello"))


def test_connect_error_on_write_is_retried() -> None:
    fake = FakeJira()
    key = fake.add_issue(["scope"])
    fake.faults.append(Fault("POST", COMMENT_PATH, exception=httpx.ConnectError))
    sleeps: list[float] = []
    fake.writer(sleeps).add_comment(key, text_to_adf("hello"))
    assert len(fake.comments[key]) == 1
    assert len(sleeps) == 1


def test_retries_are_bounded() -> None:
    fake = FakeJira()
    fake.faults.append(Fault("GET", r"/rest/api/3/issueLinkType", status=502, times=99))
    with pytest.raises(JiraRetryExhaustedError, match="after 3 attempts"):
        JiraReader(fake.http(max_attempts=3)).get_link_types()
    assert len(fake.calls) == 3


def test_client_error_carries_jira_detail() -> None:
    fake = FakeJira()
    with pytest.raises(JiraHttpError) as caught:
        fake.reader().get_issue("TYN-404", ["labels"])
    assert caught.value.status == 404
    assert "Issue does not exist" in str(caught.value)


def test_non_api_paths_are_refused() -> None:
    fake = FakeJira()
    with pytest.raises(JiraError, match="refusing"):
        fake.http().request("GET", "/secure/admin", idempotent=True)


def test_search_paginates_until_last_page() -> None:
    fake = FakeJira()
    for _ in range(7):
        fake.add_issue(["scope"])
    keys = [
        issue.key for issue in fake.reader().search_all('labels = "scope"', ["labels"], page_size=3)
    ]
    assert len(keys) == 7
    assert sum(1 for c in fake.calls if c[1].endswith("search/jql")) == 3


def test_comments_paginate() -> None:
    fake = FakeJira()
    key = fake.add_issue(["scope"])
    writer = fake.writer()
    for index in range(5):
        writer.add_comment(key, text_to_adf(f"c{index}"))
    comments = list(fake.reader().iter_comments(key, page_size=2))
    assert len(comments) == 5


def test_http_client_keeps_tls_and_does_not_follow_redirects(jira_env: None) -> None:
    from tyn.config import load_jira_settings

    with build_http_client(load_jira_settings(env_file=None)) as client:
        assert client.follow_redirects is False
        assert str(client.base_url) == "https://example-site.atlassian.net"
        assert client.timeout.connect == 10.0


def test_error_text_never_contains_authorization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errorMessages": ["bad request"]})

    client = httpx.Client(
        base_url="https://example.atlassian.net",
        transport=httpx.MockTransport(handler),
        auth=httpx.BasicAuth("user@example.com", "secret-token-value"),
    )
    with pytest.raises(JiraHttpError) as caught:
        JiraReader(JiraHttp(client, policy=RetryPolicy(), sleep=lambda _: None)).get_link_types()
    assert "secret-token-value" not in str(caught.value)
    assert "Authorization" not in str(caught.value)
