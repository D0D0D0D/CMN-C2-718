# Test Specification

## Test Strategy

- Contract coverage target: all TC-01–TC-11, PB-1–PB-7, and every contract-matrix row
- Test types: unit, integration, proof of boundary
- External LLM and source systems: injected fakes only; tests perform no network access
- Security tests use provisioned controls paired with blocked inputs to prevent false-green `KeyError` cases

## Framework Compliance Tests

| TC-ID | Test | Expected result | Result (2026-08-20) |
|---|---|---|---|
| TC-01 | Flat TypedDict State | No Pydantic/dataclass/arbitrary objects | PASS |
| TC-02 | Malformed input is refused, not repaired | A malformed request envelope is rejected at `pre_process` rather than repaired | PASS |
| TC-03 | Credential-free State | No credential-like State field or persisted secret value | PASS |
| TC-04 | InvocationContext construction | Nodes use `InvocationContext.from_state()` only | PASS |
| TC-05 | Lifecycle event ownership | No node duplicates `node_start`/`complete`/`error` | PASS |
| TC-06 | S-2 final gate | `_security_gate_input()` override raises `TypeError` | PASS |
| TC-07 | S-3 final gate | `_security_gate_output()` override raises `TypeError` | PASS |
| TC-08 | Trust boundary | Every domain node declares `VERIFIED_EXTERNAL`; anonymous caller is denied before execute | PASS |
| TC-09 | Domain S-2 | Envelope and fetched-content hooks accept clean controls and block/exclude unsafe inputs | PASS |
| TC-10 | Domain S-3 | Nested credential/PII and unapproved figure injection is blocked independently | PASS |
| TC-11 | Domain audit events | Every domain `execute()` emits at least one non-lifecycle trace event | PASS |

## Proof-of-Boundary Tests

| PB-ID | Boundary | Evidence | Result (2026-08-20) |
|---|---|---|---|
| PB-1 | BaseNode → audit emitter | Each FunctionNode execution emits a domain event | PASS |
| PB-2 | State serialization | Full post-invoke output round-trips through strict JSON (`allow_nan=False`) | PASS |
| PB-3 | Template → external source | Injected adapter receives project/period and a context-bound mock credential; output contains no credential | PASS |
| PB-4 | Import isolation | AST scan finds no Level 0 imports | PASS |
| PB-5 | Checkpoint safety | `memory_enabled` and HITL are disabled | AUTO-SKIP — checkpointing disabled |
| PB-6 | Invoke order | Dynamic all-node scan proves S-1 → lifecycle start → S-2 → execute → S-3 → lifecycle complete | PASS |
| PB-7 | HITL propagation | Exact-name test file retained; `hitl.enabled` is absent | AUTO-SKIP — non-HITL |

## Documented limits pinned by tests

Some of what `docs/02_design.md` says is a statement about what the agent does **not** do. Those are
easy to let rot, because nothing fails when the document quietly becomes wrong. The following test
exists to hold one of them:

| Documented limit | Test | What it asserts |
|---|---|---|
| The provenance check establishes attributability, not entailment — a statement contradicting its own citation is accepted when it carries no unapproved figure | `tests/unit/test_post_process.py::test_a_valid_citation_is_not_an_entailment_check` | A risk reading "the project has already failed", citing a span reading "everything is on schedule", is **accepted** and projects `source_ids`. Control: the same element with an unapproved figure is removed with `E_FIGURE_UNVERIFIED`, so the first assertion reflects the absent entailment check rather than a verification path that never runs. |

If entailment checking is ever implemented, this test fails, and the design document's claim has to
be revisited deliberately rather than drifting.

## Contract Matrix

| Contract | Success evidence | Failure evidence | Result |
|---|---|---|---|
| Fractional estimate | 0.5-point close yields velocity `0.5` and passes schema | Integer-only schema regression test would fail | PASS |
| Provenance projection | Summary, risks, and next steps carry `source_ids` | Invalid optional item is removed and listed in `unresolved` | PASS |
| Source allowlist | Known source invokes fake adapter | Unknown source returns `E_SOURCE_SET_UNKNOWN`; adapter call count stays zero | PASS |
| Schema allowlist | Known schema is loaded and validated | Unknown schema returns `E_SCHEMA_UNKNOWN` | PASS |
| Envelope keys/types | Exact object accepted | Extra key and wrong type rejected | PASS |
| Period determinism | UTC and Asia/Tokyo host settings produce identical UTC bounds | Bare timestamp and reversed period rejected | PASS |
| Fetched quality gate | Clean record reaches LLM | Replacement-heavy record is excluded and absent from prompt | PASS |
| Redaction ownership | Clean record counts 0; PII in `text`, `locator`, `source_id` or an event's `milestone_id` is redacted before `source_fetch` returns, and reaches neither the model prompt nor the report | Incomplete redaction fails the run with `E_SOURCE_REDACTION_INCOMPLETE` | PASS |
| Metrics | Golden fixture and shuffled input are identical | Zero denominator yields `null`, never `0.0` | PASS |
| LLM contract | Exact JSON draft accepted | Malformed JSON and unknown key return `E_LLM_CONTRACT` | PASS |
| Claim verification | Known id, literal span, and allowlisted figure retained | Fabricated id, span absent from the cited source, short span, and absent figure become unresolved | PASS |
| S-3 independence | Clean assembled document passes | Directly injected Japanese-adjacent/full-width figure raises | PASS |
| Trust gate | Verified external caller reaches execute | Anonymous caller never reaches execute | PASS |
| LLM availability | Injected `BaseLLM` produces narrative | Missing client returns `E_LLM_UNAVAILABLE` | PASS |
| Output surfacing | Non-empty unresolved list reaches caller | Standard status/trace/correlation/history envelope survives | PASS |
| Envelope parsing | JSON object accepted | Non-JSON and JSON array rejected | PASS |
| Duplicate records | Byte-identical duplicate collapses | Conflicting duplicate returns `E_DUPLICATE_CONFLICT` | PASS |
| Removal + revalidation | Removed optional item leaves schema-valid document | Unverified summary returns `E_SUMMARY_UNVERIFIED` | PASS |
| Manifest import | Manifest class imports as `ProjectProgressReportGraph` | Placeholder scan is empty | PASS |
| Source authorization | Permitted project fetches | Forbidden project returns `E_PROJECT_FORBIDDEN`; adapter stays uncalled | PASS |
| Secret redaction | Shared detectors redact source text | Raw mock secret is absent from LLM messages and output | PASS |
| Startup wiring | `TestClient` lifespan calls wiring and exposes health | Manifest identity assertions fail if service wiring drifts | PASS |

## Execution Summary

- Date: 2026-08-20
- `pytest tests/ -q --tb=line`: **76 passed, 3 skipped, 0 failed**
- `pytest tests/proof_of_boundary/ -q --tb=line`: **29 passed, 3 skipped, 0 failed**
- Skips: PB-5 once; PB-7 twice
- `ruff check src tests`: **PASS**
- Warning: the installed Starlette TestClient reports its upstream `httpx` deprecation; no test or runtime failure results

## Live LLM verification

| Item | Value |
|---|---|
| Date | 2026-08-20 |
| LLM | a live model client injected over the `config["llm"]` seam |
| Invocation | full graph `invoke()` with secrets provisioned (`GITLAB_READ_TOKEN`) and a `VERIFIED_EXTERNAL` caller authorised for the requested project |
| Result | `status: success`, LLM called **once**, `narrative.summary.text` non-empty with a populated `source_ids` |

This check exists because a mock LLM returns a finished object and therefore hides the failure
mode where the prompt does not carry the schema: the agent then emits a structurally valid but
empty document and still reports success. The assertion is on **non-empty content**, not merely
on the absence of a degradation flag.

Two defects were found and fixed through this run:

1. `get_output()` returned `status: error` with no reason — the failing reason code lived only in
   the S-4 event stream because the standard envelope does not carry `error_log`. The override now
   surfaces `output.errors` (de-duplicated, tracebacks stripped).
2. The generic live-LLM harness used during development cannot reach the LLM for this agent
   because it provisions no secrets, and `source_fetch` fails closed with
   `E_SOURCE_CREDENTIAL_UNAVAILABLE` before the narrative stage. That is correct agent behaviour;
   the verification was performed with an equivalent harness that provisions the secret.
