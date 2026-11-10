"""In-memory fake of the Jira Cloud REST v3 endpoints the package uses.

It is served through ``httpx.MockTransport`` so tests exercise the real
client, retry and importer code. Faults can be injected per request to
simulate rate limits, server errors and writes that apply but then fail.
"""

import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from tyn.jira.adf import adf_to_text
from tyn.jira.client import JiraHttp, JiraReader, JiraWriter
from tyn.retry import RetryPolicy

API = "/rest/api/3/"


@dataclass
class Fault:
    """A fault applied to matching requests.

    ``after`` lets that many matching requests through first, and
    ``apply_first`` performs the write before failing the response.
    """

    method: str
    path_pattern: str
    status: int | None = None
    exception: type[Exception] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    times: int = 1
    apply_first: bool = False
    after: int = 0

    def matches(self, method: str, path: str) -> bool:
        return method == self.method and re.fullmatch(self.path_pattern, path) is not None


class FakeJira:
    """A small, deterministic stand-in for one Jira project."""

    def __init__(
        self,
        project: str = "TYN",
        *,
        allow_link_type_create: bool = True,
        invert_link_semantics: bool = False,
        issue_types: tuple[str, ...] = ("Task", "Bug"),
    ) -> None:
        self.project = project
        self.allow_link_type_create = allow_link_type_create
        self.invert_link_semantics = invert_link_semantics
        self.issue_types = issue_types
        self.issues: dict[str, dict[str, Any]] = {}
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.links: dict[str, dict[str, Any]] = {}
        self.link_types: list[dict[str, str]] = [
            {"id": "10000", "name": "Relates", "inward": "relates to", "outward": "relates to"},
            {"id": "10001", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        ]
        self.faults: list[Fault] = []
        self.calls: list[tuple[str, str]] = []
        self.queries: list[str] = []
        self._ids = itertools.count(10_000)
        self._numbers = itertools.count(1)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url="https://example.atlassian.net", transport=self.transport())

    def http(self, sleeps: list[float] | None = None, max_attempts: int = 5) -> JiraHttp:
        recorded = sleeps if sleeps is not None else []
        return JiraHttp(
            self.client(),
            policy=RetryPolicy(max_attempts=max_attempts),
            sleep=recorded.append,
        )

    def reader(self, sleeps: list[float] | None = None) -> JiraReader:
        return JiraReader(self.http(sleeps))

    def writer(self, sleeps: list[float] | None = None) -> JiraWriter:
        return JiraWriter(self.http(sleeps))

    def write_calls(self) -> list[tuple[str, str]]:
        return [
            (method, path)
            for method, path in self.calls
            if method in ("POST", "PUT", "DELETE") and not path.endswith("/search/jql")
        ]

    def comment_texts(self, key: str) -> list[str]:
        return [adf_to_text(c["body"]) for c in self.comments.get(key, [])]

    def link(self, from_key: str, to_key: str, type_name: str = "Relates") -> None:
        """Add a link directly, bypassing the API (test setup only)."""
        self.links[str(next(self._ids))] = {"from": from_key, "to": to_key, "type": type_name}

    def add_issue(self, labels: list[str], summary: str = "existing") -> str:
        key = f"{self.project}-{next(self._numbers)}"
        self.issues[key] = {
            "id": str(next(self._ids)),
            "key": key,
            "fields": {"summary": summary, "labels": list(labels), "description": None},
        }
        self.comments[key] = []
        return key

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        fault = self._take_fault(method, path)
        if fault is not None and not fault.apply_first:
            return self._fault_response(fault, request)
        response = self._route(method, path, request)
        if fault is not None:
            return self._fault_response(fault, request)
        return response

    def _take_fault(self, method: str, path: str) -> Fault | None:
        for fault in self.faults:
            if fault.times > 0 and fault.matches(method, path):
                if fault.after > 0:
                    fault.after -= 1
                    return None
                fault.times -= 1
                return fault
        return None

    def _fault_response(self, fault: Fault, request: httpx.Request) -> httpx.Response:
        if fault.exception is not None:
            raise fault.exception("injected", request=request)  # type: ignore[call-arg]
        return httpx.Response(fault.status or 500, headers=fault.headers, json={})

    def _route(self, method: str, path: str, request: httpx.Request) -> httpx.Response:
        routes: list[tuple[str, str, Callable[..., httpx.Response]]] = [
            ("GET", rf"{API}project/([A-Z0-9_]+)", self._get_project),
            ("GET", rf"{API}issue/createmeta/([A-Z0-9_]+)/issuetypes", self._createmeta),
            ("POST", rf"{API}search/jql", self._search),
            ("POST", rf"{API}issue", self._create_issue),
            ("GET", rf"{API}issue/([A-Z0-9_]+-\d+)", self._get_issue),
            ("GET", rf"{API}issue/([A-Z0-9_]+-\d+)/comment", self._get_comments),
            ("POST", rf"{API}issue/([A-Z0-9_]+-\d+)/comment", self._add_comment),
            ("GET", rf"{API}issueLinkType", self._get_link_types),
            ("POST", rf"{API}issueLinkType", self._create_link_type),
            ("POST", rf"{API}issueLink", self._create_link),
            ("DELETE", rf"{API}issueLink/(\d+)", self._delete_link),
        ]
        for route_method, pattern, handler in routes:
            match = re.fullmatch(pattern, path)
            if route_method == method and match:
                return handler(request, *match.groups())
        return httpx.Response(404, json={"errorMessages": [f"no route {method} {path}"]})

    def _get_project(self, request: httpx.Request, key: str) -> httpx.Response:
        if key != self.project:
            return httpx.Response(404, json={"errorMessages": ["No project could be found"]})
        return httpx.Response(200, json={"id": "1", "key": key, "name": "Test"})

    def _createmeta(self, request: httpx.Request, key: str) -> httpx.Response:
        types = [
            {"id": str(20 + i), "name": name, "subtask": False}
            for i, name in enumerate(self.issue_types)
        ]
        types.append({"id": "99", "name": "Sub-task", "subtask": True})
        return httpx.Response(200, json={"issueTypes": types, "total": len(types)})

    def _search(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        jql = body["jql"]
        project = re.search(r'project = "((?:[^"\\]|\\.)*)"', jql)
        wanted = [_unescape(v) for v in re.findall(r'labels = "((?:[^"\\]|\\.)*)"', jql)]
        self.queries.append(jql)
        keys = re.search(r"key in \(([^)]*)\)", jql)
        allowed = {k.strip() for k in keys.group(1).split(",")} if keys else None
        text = re.search(r'summary ~ "((?:[^"\\]|\\.)*)"', jql)
        words = _unescape(text.group(1)).lower().split() if text else []
        hits = [
            issue
            for issue in self.issues.values()
            if (project is None or issue["key"].startswith(_unescape(project.group(1)) + "-"))
            and all(label in issue["fields"]["labels"] for label in wanted)
            and (allowed is None or issue["key"] in allowed)
            and all(word in issue["fields"]["summary"].lower() for word in words)
        ]
        start = int(body.get("nextPageToken") or 0)
        size = int(body.get("maxResults", 50))
        page = hits[start : start + size]
        payload: dict[str, Any] = {
            "issues": [self._project_fields(i, body.get("fields", [])) for i in page],
            "isLast": start + size >= len(hits),
        }
        if start + size < len(hits):
            payload["nextPageToken"] = str(start + size)
        return httpx.Response(200, json=payload)

    def _create_issue(self, request: httpx.Request) -> httpx.Response:
        fields = json.loads(request.content)["fields"]
        if any(" " in label for label in fields.get("labels", [])):
            return httpx.Response(400, json={"errors": {"labels": "labels cannot contain spaces"}})
        key = self.add_issue(fields["labels"], fields["summary"])
        self.issues[key]["fields"]["description"] = fields["description"]
        return httpx.Response(201, json={"id": self.issues[key]["id"], "key": key})

    def _get_issue(self, request: httpx.Request, key: str) -> httpx.Response:
        if key not in self.issues:
            return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
        wanted = request.url.params.get("fields", "").split(",")
        return httpx.Response(200, json=self._project_fields(self.issues[key], wanted))

    def _project_fields(self, issue: dict[str, Any], wanted: list[str]) -> dict[str, Any]:
        fields = {k: v for k, v in issue["fields"].items() if k in wanted}
        if "issuelinks" in wanted:
            fields["issuelinks"] = self._links_of(issue["key"])
        return {"id": issue["id"], "key": issue["key"], "fields": fields}

    def _get_comments(self, request: httpx.Request, key: str) -> httpx.Response:
        if key not in self.issues:
            return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
        start = int(request.url.params.get("startAt", 0))
        size = int(request.url.params.get("maxResults", 100))
        items = self.comments[key]
        return httpx.Response(
            200,
            json={
                "comments": items[start : start + size],
                "startAt": start,
                "maxResults": size,
                "total": len(items),
            },
        )

    def _add_comment(self, request: httpx.Request, key: str) -> httpx.Response:
        if key not in self.issues:
            return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
        body = json.loads(request.content)["body"]
        comment = {"id": str(next(self._ids)), "body": body, "created": "2026-01-01T00:00:00Z"}
        self.comments[key].append(comment)
        return httpx.Response(201, json=comment)

    def _get_link_types(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"issueLinkTypes": self.link_types})

    def _create_link_type(self, request: httpx.Request) -> httpx.Response:
        if not self.allow_link_type_create:
            return httpx.Response(403, json={"errorMessages": ["You do not have permission"]})
        body = json.loads(request.content)
        created = {"id": str(next(self._ids)), **body}
        self.link_types.append(created)
        return httpx.Response(201, json=created)

    def _create_link(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        outward, inward = body["outwardIssue"]["key"], body["inwardIssue"]["key"]
        if self.invert_link_semantics:
            outward, inward = inward, outward
        type_name = body["type"]["name"]
        for link in self.links.values():
            if (link["from"], link["to"], link["type"]) == (outward, inward, type_name):
                return httpx.Response(201)
        link_id = str(next(self._ids))
        self.links[link_id] = {"from": outward, "to": inward, "type": type_name}
        return httpx.Response(201)

    def _delete_link(self, request: httpx.Request, link_id: str) -> httpx.Response:
        if self.links.pop(link_id, None) is None:
            return httpx.Response(404, json={"errorMessages": ["No link"]})
        return httpx.Response(204)

    def _links_of(self, key: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for link_id, link in self.links.items():
            link_type = next(t for t in self.link_types if t["name"] == link["type"])
            if link["from"] == key:
                result.append(
                    {"id": link_id, "type": link_type, "outwardIssue": {"key": link["to"]}}
                )
            if link["to"] == key:
                result.append(
                    {"id": link_id, "type": link_type, "inwardIssue": {"key": link["from"]}}
                )
        return result

    def outgoing(self) -> set[tuple[str, str, str]]:
        """Return links as (from_key, to_key, type) triples."""
        return {(link["from"], link["to"], link["type"]) for link in self.links.values()}


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")
