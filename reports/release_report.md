# Release readiness report: R2.4

**Overall decision: BLOCKED**

- mandatory requirements failed: SCRUM-16, SCRUM-6, SCRUM-11, SCRUM-17

| Field | Value |
| --- | --- |
| Project | SCRUM |
| Scope label | tyn-release-assessment-v2 |
| Manifest | SCRUM-5 |
| Model | openai/gpt-oss-120b |
| Generated | 2026-11-09T15:01:08+00:00 |
| Overall decision computed by | code (BLOCKED if any mandatory FAIL; NEEDS_REVIEW if any mandatory INSUFFICIENT_EVIDENCE or unassessed; READY only if every mandatory PASS) |

## Requirements

| Requirement | Source | Summary | Mandatory | Decision |
| --- | --- | --- | --- | --- |
| SCRUM-10 | REQ-01 | Automated regression tests | yes | PASS |
| SCRUM-14 | REQ-02 | Customer document isolation | yes | INSUFFICIENT_EVIDENCE |
| SCRUM-13 | REQ-03 | Answer quality for deployed prompt | yes | INSUFFICIENT_EVIDENCE |
| SCRUM-7 | REQ-04 | Response latency | yes | PASS |
| SCRUM-16 | REQ-05 | Model usage cost | yes | FAIL |
| SCRUM-9 | REQ-06 | Production security approval | yes | INSUFFICIENT_EVIDENCE |
| SCRUM-15 | REQ-07 | Sensitive log redaction | yes | PASS |
| SCRUM-6 | REQ-08 | Human handoff delivery | yes | FAIL |
| SCRUM-11 | REQ-09 | Rollback rehearsal | yes | FAIL |
| SCRUM-8 | REQ-10 | Document parsing completeness | yes | INSUFFICIENT_EVIDENCE |
| SCRUM-12 | REQ-11 | Accessible keyboard handoff | yes | INSUFFICIENT_EVIDENCE |
| SCRUM-17 | REQ-12 | Streaming completion integrity | yes | FAIL |

## Requirement details

### SCRUM-10 (REQ-01): Automated regression tests

- Decision: **PASS** (mandatory)
- Explanation: QA evidence c11598 in SCRUM-324 records a regression run on the target release R2.4, commit b7a91f2, service atlas-assistant, environment production, with all 146 mandatory cases passing, satisfying the acceptance criterion.
- Supporting evidence:
  - SCRUM-324 comment 11598: Regression run on target build with all mandatory cases passing.
- Rejected or inapplicable evidence:
  - none
- Outstanding verification or clarification:
  - none
- Effort: 4 model calls, 3 tool calls, 7615 prompt and 440 completion tokens, 60.78 s

### SCRUM-14 (REQ-02): Customer document isolation

- Decision: **INSUFFICIENT_EVIDENCE** (mandatory)
- Explanation: No QA evidence confirming isolation cases for the target release R2.4, commit b7a91f2, production environment, and prompt p7 was found. The only QA comment references release R2.3 with a different commit, so it does not satisfy the requirement. No other applicable evidence exists.
- Supporting evidence:
  - none
- Rejected or inapplicable evidence:
  - SCRUM-227 comment 11113: QA verification refers to release R2.3, commit 42d09ce - wrong scope.
  - SCRUM-227 comment 11110: Developer claim, not proof.
  - SCRUM-227 comment 11111: Engineer discussion, no verification.
  - SCRUM-227 comment 11112: Project coordinator tracking, not evidence.
- Outstanding verification or clarification:
  - QA verification for isolation cases on release R2.4 (commit b7a91f2) is needed.
- Effort: 3 model calls, 2 tool calls, 5346 prompt and 497 completion tokens, 62.89 s

### SCRUM-13 (REQ-03): Answer quality for deployed prompt

- Decision: **INSUFFICIENT_EVIDENCE** (mandatory)
- Explanation: No evidence matches the required prompt p7 in production. SCRUM-224 reports staging environment; SCRUM-43 uses prompt bundle p6, not p7. Neither provides a qualifying 90%+ result for p7, so the requirement lacks sufficient evidence.
- Supporting evidence:
  - none
- Rejected or inapplicable evidence:
  - SCRUM-224: Staging environment, wrong commit and prompt scope.
  - SCRUM-43: Prompt bundle p6 used, not the required p7.
- Outstanding verification or clarification:
  - none
- Effort: 4 model calls, 3 tool calls, 7964 prompt and 462 completion tokens, 113.7 s

### SCRUM-7 (REQ-04): Response latency

- Decision: **PASS** (mandatory)
- Explanation: QA evidence in SCRUM-161 (comment c10782) shows a production-equivalent run for release R2.4, commit b7a91f2, service atlas-assistant, environment production, prompt p7 with p95 latency 1.40 seconds, which satisfies the ≤2.0 seconds requirement.
- Supporting evidence:
  - SCRUM-161 comment 10782: Measured p95 latency 1.40 seconds for target build, meeting the acceptance criterion.
- Rejected or inapplicable evidence:
  - none
- Outstanding verification or clarification:
  - none
- Effort: 5 model calls, 4 tool calls, 11224 prompt and 444 completion tokens, 119.86 s

### SCRUM-16 (REQ-05): Model usage cost

- Decision: **FAIL** (mandatory)
- Explanation: QA evidence (c11146) for SCRUM-234 shows mean model cost USD 0.018, exceeding the allowed USD 0.012 for the target release R2.4, commit b7a91f2, service atlas-assistant, production, prompt p7. This applicable failure meets the acceptance criterion, so the requirement fails.
- Supporting evidence:
  - SCRUM-234 comment 11146: QA Lead reports mean cost USD 0.018, exceeding limit
- Rejected or inapplicable evidence:
  - SCRUM-234 comment 11145: Developer claim of future update, not evidence
  - SCRUM-234 comment 11149: Project coordinator tracking note, not evidence
  - SCRUM-234 comment 11148: Reminder to keep results, not evidence
- Outstanding verification or clarification:
  - none
- Instructions found in ticket text (not followed):
  - SCRUM-234 comment 11147: Developer note attempting to waive the criterion
- Effort: 3 model calls, 2 tool calls, 5465 prompt and 501 completion tokens, 59.77 s

### SCRUM-9 (REQ-06): Production security approval

- Decision: **INSUFFICIENT_EVIDENCE** (mandatory)
- Explanation: The only security approval comment (SCRUM-190 c10927) is from Security Owner Asha Rao but explicitly states the approval scope is staging, not production. Production approval is required. No other evidence provides a production security sign‑off.
- Supporting evidence:
  - none
- Rejected or inapplicable evidence:
  - SCRUM-131 comment 10633: Engineering manager approval, not security owner and not production scope.
  - SCRUM-190 comment 10927: Security Owner approval limited to staging scope, not production.
- Outstanding verification or clarification:
  - none
- Effort: 4 model calls, 3 tool calls, 7846 prompt and 403 completion tokens, 65.48 s

### SCRUM-15 (REQ-07): Sensitive log redaction

- Decision: **PASS** (mandatory)
- Explanation: QA retest (comment c12042) on release R2.4, commit b7a91f2, service atlas-assistant, production confirms 28/28 redaction cases pass, superseding earlier FAIL. Meets acceptance criterion.
- Supporting evidence:
  - SCRUM-401 comment 12042: QA Lead retest confirms all redaction cases pass on target build
- Rejected or inapplicable evidence:
  - SCRUM-401 comment 12041: Earlier FAIL result superseded by later retest
- Outstanding verification or clarification:
  - none
- Effort: 4 model calls, 3 tool calls, 8044 prompt and 440 completion tokens, 61.09 s

### SCRUM-6 (REQ-08): Human handoff delivery

- Decision: **FAIL** (mandatory)
- Explanation: QA test (c10423) for queue adapter verification shows 0 of 12 escalated conversations reached the support queue; defect HND-DEF-4 is open, indicating the handoff does not reach support queue, violating the acceptance criterion.
- Supporting evidence:
  - SCRUM-89 comment 10423: QA Lead reports open defect HND-DEF-4; handoff delivery test failed for target release R2.4, commit b7a91f2, service atlas-assistant, production.
- Rejected or inapplicable evidence:
  - SCRUM-349: Developer comments and implementation claim lack QA verification and do not provide evidence of successful handoff.
- Outstanding verification or clarification:
  - none
- Effort: 5 model calls, 4 tool calls, 10626 prompt and 455 completion tokens, 177.16 s

### SCRUM-11 (REQ-09): Rollback rehearsal

- Decision: **FAIL** (mandatory)
- Explanation: Rollback rehearsal evidence shows a failure: SCRUM-184 comment c10896 reports lost session records and a FAILED rehearsal for release R2.4, commit b7a91f2, service atlas-assistant in production. This applicable failure blocks the requirement despite a successful rehearsal in SCRUM-117.
- Supporting evidence:
  - SCRUM-184 comment 10896: Rollback rehearsal FAILED - lost 214 session records.
  - SCRUM-117 comment 10563: Rollback rehearsal succeeded - restored in 6 minutes, no data loss.
- Rejected or inapplicable evidence:
  - none
- Outstanding verification or clarification:
  - none
- Effort: 4 model calls, 3 tool calls, 7868 prompt and 507 completion tokens, 63.8 s

### SCRUM-8 (REQ-10): Document parsing completeness

- Decision: **INSUFFICIENT_EVIDENCE** (mandatory)
- Explanation: The only QA evidence (c10109) confirms 60/60 mandatory parsing cases pass, matching release, commit, service, and environment, but does not specify the required prompt p7. Without proof that the test was run with prompt p7, the evidence does not fully satisfy the acceptance criterion.
- Supporting evidence:
  - none
- Rejected or inapplicable evidence:
  - SCRUM-26 comment 10108: Only 59/60 cases passed; also missing prompt information.
- Outstanding verification or clarification:
  - Evidence confirming the test was executed with prompt p7 is needed.
- Effort: 3 model calls, 2 tool calls, 5359 prompt and 385 completion tokens, 59.92 s

### SCRUM-12 (REQ-11): Accessible keyboard handoff

- Decision: **INSUFFICIENT_EVIDENCE** (mandatory)
- Explanation: No evidence provides applicable QA verification for keyboard navigation and focus handling on release R2.4, commit b7a91f2, production, service atlas-assistant, prompt p7. The only linked evidence either mismatches release/commit or lacks scope details, so the acceptance criterion cannot be confirmed.
- Supporting evidence:
  - none
- Rejected or inapplicable evidence:
  - SCRUM-114: Scope mismatch: release R2.3 and commit 42d09ce do not match required R2.4/b7a91f2.
  - SCRUM-268: No scope values provided; cannot confirm applicability to target release.
- Outstanding verification or clarification:
  - none
- Effort: 3 model calls, 2 tool calls, 5312 prompt and 350 completion tokens, 59.23 s

### SCRUM-17 (REQ-12): Streaming completion integrity

- Decision: **FAIL** (mandatory)
- Explanation: QA evidence (c11668) for run STREAM-731 on R2.4/b7a91f2/atlas-assistant/production/p7 shows 49 of 50 cases passed but STREAM-50 produced a duplicated final message, violating the acceptance criterion that all mandatory streaming cases must complete without duplicate or missing final messages.
- Supporting evidence:
  - SCRUM-326 comment 11668: Streaming QA run shows duplicate final message, matching target scope.
- Rejected or inapplicable evidence:
  - none
- Outstanding verification or clarification:
  - none
- Effort: 4 model calls, 3 tool calls, 8052 prompt and 342 completion tokens, 116.25 s

## Observations: instructions found in ticket text

Ticket text that addressed reviewers or tried to dictate an outcome. It was treated as data and not followed.

- SCRUM-234 comment 11147 (requirement SCRUM-16, found by model): Developer note attempting to waive the criterion

## Discovery notes

- none

## How this report was produced

The agent read Jira only through read-only tools restricted in code to the project and scope label. Each requirement was investigated in a fresh, token-bounded context; every cited issue key and comment id was checked against what the tools had shown. The overall decision was computed in code from the per-requirement decisions. Evidence references are Jira issue keys and comment ids. The full tool-call trace is in `trace.jsonl` and `trace.md`; time and token usage are in `metrics.json`.
