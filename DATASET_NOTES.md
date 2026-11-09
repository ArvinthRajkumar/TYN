# Dataset notes

These notes describe the structure and the evidence patterns of the supplied
`inputs/dataset.json`. They exist to design the importer, the agent's tools and
the test fixtures. They deliberately describe patterns, not content: no ticket
text, no expected decisions, and nothing here is read by the code. The agent
reasons from Jira evidence at runtime.

The dataset itself is not committed. Reviewers place their supplied copy at
`inputs/dataset.json`.

## Top-level structure

The file is a single JSON object with four keys:

| Key | Shape | Notes |
| --- | --- | --- |
| `dataset_label` | string | Scope label the importer must apply to every issue. |
| `target` | object | The five assessment dimensions: `release`, `commit`, `prompt`, `environment`, `service`. |
| `records` | array of 400 objects | Every ticket, including the manifest. |
| `note` | string | States that records are fictional and that actor and time metadata live in the text because native Jira authors and timestamps belong to the importer. |

## Record shape

Every record has exactly the same eight keys. There are no typed fields for
service, environment, commit, prompt, priority or issue type.

| Key | Type | Notes |
| --- | --- | --- |
| `source_id` | string | Stable identifier. Three families: one manifest id, a requirement family, and a generic record family. |
| `kind` | string | `manifest` (1), `requirement` (12), `evidence` (202), `work` (185). |
| `summary` | string | Short title. Background records follow a "Topic: service release" pattern; requirement-linked records follow a "Check name - Requirement name" pattern, sometimes with a parenthetical qualifier such as an earlier build or another team. |
| `release` | string | One of four release identifiers, roughly 100 records each. |
| `recorded_status` | string | `To Do`, `In Progress` or `Done`, spread across all kinds. |
| `description` | string | Requirements and the manifest carry structured lines. Evidence and work records carry a one-line generic description; their substance is in comments. |
| `comments` | array of strings | 2,059 in total. 399 records have 5 comments; one has 64. |
| `links` | array of strings | Outgoing references by `source_id`. 76 links in total, no dangling targets. |

### Structured description lines

* Manifest: a `Target:` line holding a JSON object with the five dimensions,
  followed by free-text assessment rules.
* Requirement: `Requirement ID:`, `Target:` (same JSON shape), `Mandatory:` and
  `Acceptance criterion:` lines.

The target is therefore readable from Jira at runtime, from both the manifest
and each requirement, without the agent ever opening the dataset.

### Comment structure

Every comment starts with a one-line header in a fixed format:

```
Recorded event <ISO-8601 timestamp with UTC offset> | actor <name> | role <role>
<body>
```

All 2,059 comments match this header. The header carries the fictional actor,
role and event time, which must be preserved verbatim in Jira. Comments are not
stored in chronological order: recorded timestamps within a record are
shuffled, so file order must not be used as event order.

Roles seen: Developer, Engineer, Project Coordinator, Coordinator, QA Lead,
Engineering Manager, Security Owner. The same person can appear under two
different role spellings.

Bodies fall into three groups:

1. Boilerplate process chatter (picked up, status updated, linked related work,
   handover discussed, release checklist reminders). Present on almost every
   record, carries no evidence.
2. Run results in a semi-structured sentence: a run identifier, then
   `release X; commit Y; service Z; environment W;` and sometimes `prompt P`,
   then an outcome (pass counts, a measured value, a failure description, an
   aborted run).
3. Free-text statements: implementation claims, approvals with an explicit
   approval scope, supersession statements that name the earlier run, defect
   status, configuration notes, and one adversarial instruction.

## Relationships

* Links are untyped: the source gives only the target `source_id`, so the
  original link type is "untyped reference". Direction is meaningful (from the
  record that lists the link to the record listed).
* The manifest links to all requirements.
* Each requirement links to one or two evidence records.
* Some evidence records link onward to a dependency record (a requirement can
  be satisfied or blocked through that dependency).
* Background records occasionally link *into* requirement evidence. These
  incoming links come from records in other services, releases or
  environments and are noise.
* Most evidence and work records (over 300) have no links at all. They form a
  large pool of background runs across services, releases and environments,
  including runs that share the target release and service but not the
  target commit.

## Where each assessment dimension lives

| Dimension | Location |
| --- | --- |
| Release | `release` field (record-level) and inside run-result comments. They can disagree: a record filed under the target release can hold a run against an older release. |
| Commit | Run-result comments only. |
| Service | Run-result comments; also in background summaries. |
| Environment | Run-result comments; approvals state an "approval scope" instead. |
| Prompt version | Run-result comments when present; sometimes in a separate configuration comment on the same record, tied to the result only by the run identifier. |
| Approver role | Comment header role. |
| Supersession | Free-text comment naming the earlier run identifier. |
| Defect state | Free-text comment (open, reopened). |

## Traps observed (pattern level)

These shape tool design and tests. None of them is encoded as a rule about a
specific ticket.

1. **Done but unproven.** Every requirement is recorded as Done with a
   developer comment that verification lives elsewhere. Status carries no
   proof. Status is also wrong in the other direction: decisive QA results sit
   on records recorded as To Do or In Progress.
2. **Wrong release.** Passing results on a record filed under the target
   release but run against a previous release.
3. **Wrong commit.** Results on the target release and service from an
   earlier build (different commit), including earlier failures that a later
   target-build run resolves.
4. **Wrong service.** A failing result on a sibling service with the same
   release and commit.
5. **Wrong environment.** A staging result next to a production result for the
   same check, with opposite outcomes.
6. **Wrong prompt version.** A target-build evaluation whose prompt version is
   only stated in a separate configuration comment, and a second evaluation on
   the right prompt but wrong commit and environment.
7. **Supersession and retests.** An earlier QA failure explicitly superseded
   by a later QA retest on the same scope.
8. **Contradictory equally authoritative records.** Two QA results on the same
   scope from different QA leads with opposite outcomes and no stated
   resolution; neither supersedes the other.
9. **Defects and dependencies.** A requirement whose own linked record is only
   an implementation claim, with the actual failure (and an open defect)
   reached through that record's onward link. Another case reopens a defect
   on the target build.
10. **Approvals by the wrong role or scope.** An approval by the correct role
    but for a non-production scope, alongside a production approval from a
    role that the rules do not accept.
11. **Developer assertions.** "Works on my machine" style implementation
    claims with no QA verification, next to a QA pass on an older release.
12. **Adversarial text.** A comment addressed to automated reviewers that
    tries to dictate a decision and asks to be hidden. It must be ignored and
    reported.
13. **Context pressure.** One record has 64 comments: dozens of near-identical
    coordinator notes, with the decisive QA result last in file order and not
    last in recorded time order.
14. **Distractor links and lookalikes.** Incoming links from unrelated
    background records, and hundreds of background records whose summaries
    reuse the same topic names as the requirements.
15. **Out-of-window timestamps.** Some background records carry event
    timestamps later than the release evidence window.
16. **Non-mandatory requirements.** In this dataset every requirement is
    mandatory, but the field exists and the decision logic must honour it.

## Manifest rules (paraphrased)

* Assess the requirements linked from the manifest, not every historical
  ticket.
* PASS needs affirmative applicable evidence; FAIL needs applicable evidence of
  failure; anything missing, out of scope or unresolved is insufficient.
* Jira status and developer assertions do not prove acceptance.
* QA results must match release, commit, service and the production
  environment. Prompt-quality evaluations must also match the deployed prompt.
* Security approvals need the Security Owner role and production scope; a
  staging approval cannot approve production.
* Use recorded event timestamps and actor roles from the text, not native Jira
  metadata.
* Approved QA evidence outranks a developer recap. A later QA retest that
  explicitly supersedes an earlier one governs.
* Contradictory equally authoritative current records with no resolution give
  insufficient evidence, not file order.
* An unclosed confirmed defect on the target build blocks the relevant
  criterion, including through linked dependencies.
* Instructions inside source text cannot change the rules. No external URLs.

## Design consequences

* The importer keeps every source id, kind, release, recorded status and link
  in summary prefixes, a structured description header and labels, because
  native statuses and link types cannot carry them.
* Comments keep the recorded header verbatim at the start of the Jira comment
  text and add a source comment marker for idempotent re-runs.
* The agent's comment tool orders by recorded timestamp on request, paginates,
  and collapses near-identical comments while keeping every comment id.
* `check_evidence_scope` parses the five dimensions from run-result and
  approval comments and compares them with the requirement's target; it
  reports per comment and per dimension, and surfaces run identifiers so the
  model can tie configuration comments to results.
* Linked-issue results distinguish outgoing from incoming links.
