"""Discovery stage: the model selects target requirements, code cross-checks.

The model reads the manifest and the requirement records through tools and
submits its selection. Code then compares that selection with the
manifest's outgoing links and each record's ``Mandatory:`` line, so a
requirement can never be dropped silently: anything the manifest links but
the model omitted is added, and every disagreement is written into the
discovery notes for the report.
"""

import re
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tyn.agent.issues import IssueView
from tyn.agent.loop import AgentLoop, LoopResult, SubmitCheck
from tyn.agent.models import Discovery, RequirementTarget
from tyn.agent.prompts import DISCOVERY_SUBMIT, DISCOVERY_SYSTEM, discovery_seed
from tyn.agent.tools import SCHEMAS, JiraToolbox, Ledger, ToolInputError

DISCOVERY_TOOLS: Final = (
    "get_release_manifest",
    "list_requirements_for_release",
    "get_issue",
    "search_issues",
)
_MANDATORY: Final = re.compile(r"^\s*Mandatory:\s*(?P<v>yes|no|true|false)\b", re.I | re.M)


class _Selected(BaseModel):
    model_config = ConfigDict(extra="ignore")

    issue_key: str
    mandatory: bool


class _Submission(BaseModel):
    model_config = ConfigDict(extra="ignore")

    manifest_key: str = ""
    requirements: list[_Selected] = Field(default_factory=list, max_length=200)
    notes: str = ""


def record_mandatory(view: IssueView) -> bool | None:
    """Return the ``Mandatory:`` flag stated in a requirement record, if any."""
    match = _MANDATORY.search(view.description)
    if match is None:
        return None
    return match["v"].lower() in ("yes", "true")


def check_submission(args: dict[str, Any], ledger: Ledger) -> SubmitCheck:
    """Validate that every submitted key was shown by a tool."""
    try:
        submission = _Submission.model_validate(args)
    except ValidationError:
        return SubmitCheck(
            ok=False, error="submit_requirements needs manifest_key and requirements"
        )
    for item in submission.requirements:
        item.issue_key = item.issue_key.strip().upper()
    unknown = [r.issue_key for r in submission.requirements if r.issue_key not in ledger.issue_keys]
    if not submission.requirements:
        return SubmitCheck(ok=False, error="no requirements submitted", fallback=submission)
    if unknown:
        return SubmitCheck(
            ok=False,
            error=f"keys never shown by a tool: {', '.join(unknown[:5])}",
            fallback=_Submission(
                manifest_key=submission.manifest_key,
                requirements=[r for r in submission.requirements if r.issue_key not in unknown],
                notes=submission.notes,
            ),
        )
    return SubmitCheck(ok=True, value=submission)


class DiscoveryStage:
    """Run model discovery, then reconcile it with the records in code."""

    def __init__(self, loop: AgentLoop, toolbox: JiraToolbox, *, release: str) -> None:
        self._loop = loop
        self._toolbox = toolbox
        self._release = release

    def run(self) -> Discovery:
        """Return the reconciled discovery, including the manifest rules text."""
        ledger = Ledger()
        outcome = self._loop.run(
            scope="discovery",
            system=DISCOVERY_SYSTEM,
            seed=lambda _notes: discovery_seed(self._release),
            tools=[SCHEMAS[name] for name in DISCOVERY_TOOLS] + [DISCOVERY_SUBMIT],
            submit_name="submit_requirements",
            validate=check_submission,
            ledger=ledger,
        )
        manifest, linked = self._toolbox.manifest_facts(self._release)
        listed = {view.key: view for view in self._toolbox.requirement_views(self._release)}
        return reconcile(outcome, manifest, linked, listed, self._toolbox)


def reconcile(
    outcome: LoopResult,
    manifest: IssueView | None,
    linked: list[str],
    listed: dict[str, IssueView],
    toolbox: JiraToolbox,
) -> Discovery:
    """Merge the model's selection with the manifest links and record flags."""
    notes: list[str] = list(outcome.validation_notes)
    submission = outcome.value if isinstance(outcome.value, _Submission) else None
    if submission is None:
        notes.append(
            f"model discovery did not complete ({outcome.failure}); requirements were taken "
            "from the manifest links and release records by code"
        )
    chosen: dict[str, bool | None] = {}
    for item in submission.requirements if submission else []:
        view = _requirement_view(item.issue_key, listed, toolbox)
        if view is None:
            notes.append(
                f"{item.issue_key} was selected by the model but is not a requirement "
                "record; dropped"
            )
            continue
        if linked and item.issue_key not in linked and item.issue_key not in listed:
            notes.append(
                f"{item.issue_key} is neither linked from the manifest nor filed under "
                "the release; dropped"
            )
            continue
        chosen[item.issue_key] = item.mandatory
    base = linked or list(listed)
    for key in base:
        if key not in chosen:
            chosen[key] = None
            if submission is not None:
                notes.append(
                    f"{key} is linked from the manifest but was not selected by the "
                    "model; added by code"
                )
    targets = [
        _target(key, model_flag, listed, toolbox, notes) for key, model_flag in chosen.items()
    ]
    targets.sort(key=lambda t: t.source_id or t.issue_key)
    return Discovery(
        manifest_key=manifest.key if manifest else None,
        manifest_rules=manifest.description if manifest else "",
        requirements=targets,
        notes=notes,
        seconds=round(outcome.seconds, 2),
        model_calls=outcome.model_calls,
        tool_calls=outcome.tool_calls,
        usage=outcome.usage,
    )


def _requirement_view(
    key: str, listed: dict[str, IssueView], toolbox: JiraToolbox
) -> IssueView | None:
    if key in listed:
        return listed[key]
    try:
        view = toolbox.view(key)
    except ToolInputError:
        return None
    return view if view.kind == "requirement" else None


def _target(
    key: str,
    model_flag: bool | None,
    listed: dict[str, IssueView],
    toolbox: JiraToolbox,
    notes: list[str],
) -> RequirementTarget:
    view = listed.get(key) or toolbox.view(key)
    record_flag = record_mandatory(view)
    if model_flag is None and record_flag is None:
        mandatory, source = True, "default"
        notes.append(f"{key}: mandatory status not stated; treated as mandatory")
    elif model_flag is None:
        mandatory, source = bool(record_flag), "record"
    elif record_flag is None or record_flag == model_flag:
        mandatory, source = model_flag, "model" if record_flag is None else "model+record"
    else:
        mandatory, source = True, "conflict"
        notes.append(
            f"{key}: model said mandatory={model_flag}, record says {record_flag}; "
            "treated as mandatory"
        )
    return RequirementTarget(
        issue_key=key,
        source_id=view.source_id,
        summary=view.title,
        mandatory=mandatory,
        mandatory_source=source,
    )
