# Template Design Specification

## Position in AgentCore Architecture

- **Agent Class**: ProjectProgressReportGraph
- **L1 Base**: **AgentBaseGraph** (L1 direct)
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible)
  - Node: L1 inheritance. **`execute(self, state) -> dict` override only applies to `FunctionNode`
    subclasses.** The `GraphNode` subclass implements its own three abstract methods instead
    (see *Composition Pattern*) — it does not override `execute()`.
  - Graph: composition (`register_nodes()` for node substitution)

### AGENTIC STAR Marketplace entry boundary

The repository-root `cli.py` subclasses `ProjectProgressReportGraph` as `MarketplaceGraph`.
The generic runner adapter is insufficient even though no business node reads a `mode` or
`input_context` key: every business node requires `VERIFIED_EXTERNAL`, `pre_process` authorizes
`InvocationContext.caller_id`, `source_fetch` requires the selected source credential, and
`narrative_draft` requires an injected LLM.

Marketplace therefore resolves `caller_id`, `project_id`, `source_set_id`, and
`output_schema_id` from `STG_DEFAULT_*` deployment variables. It restricts the runtime config to
that project/source/schema, forces `VERIFIED_EXTERNAL`, disables HITL/memory, and accepts only
`period_start` / `period_end` from the request. There is no mode field in this AG and the adapter
does not invent or expose one; both existing source adapters only implement `fetch()`.
`input_context` is rebuilt as `{}` so runner `conversation_history` never enters State outside
the S-2 scan.

Azure OpenAI and the selected source credential are allowlisted and checked in
`provision_secrets()`, making absence or whitespace a startup failure. The Azure values are read
from the bound provider and a new client is built for every LLM operation; connection values and
clients are not retained on `MarketplaceGraph` or the LLM facade.

> The inner workflow graph reached through `main` is **composition, not inheritance** — the
> inheritance row above names the L1 base class only (pre-empt item 4 below).

## Architecture Overview

### Node Configuration

| Node | Responsibility | Input State | Output State | Inherits/Overrides | `required_trust_level` |
|------|---------------|-------------|--------------|-------------------|------------------------|
| initialize | framework default | — | — | InitializeNode (default) | (framework) |
| pre_process | Validate the request envelope (keys/types/allowlists), resolve the reporting period to absolute UTC bounds, apply the quality gate to **envelope-supplied** text. | `user_input`, `InvocationContext.caller_id` | `validated_input`, `rejected` | `FunctionNode` → `InputValidateNode` | `VERIFIED_EXTERNAL` |
| main | Compose the report body. Delegates to the inner workflow graph. | `validated_input` | `metrics`, `narrative`, `source_records` | `GraphNode` → `ReportComposeNode` | `VERIFIED_EXTERNAL` |
| post_process | Assemble target-schema JSON, validate it, verify each prose element against its evidence, drop unverified items into `unresolved`, re-validate. | `metrics`, `narrative`, `source_records` | `formatted_output`, `unresolved` | `FunctionNode` → `SchemaAssembleNode` | `VERIFIED_EXTERNAL` |
| finalize | framework default | — | — | FinalizeNode (default) | (framework) |

**Inner workflow graph** — `DomainWorkflowGraph(BaseGraph)`, **not `AgentBaseGraph`**.
`AgentBaseGraph` fixes exactly three domain slots (`pre_process` / `main` / `post_process`), and
this stage needs four nodes; squeezing `source_content_gate` into another slot would either drop
it or hide it inside an unrelated node. Registering four nodes explicitly on `BaseGraph` and
wiring the edges is the honest shape.

All four are `FunctionNode` with `required_trust_level = VERIFIED_EXTERNAL`:

| Node | Responsibility |
|------|---------------|
| `source_fetch` | Read through the adapter named by the resolved `source_set_id`. Read-only. **Redacts credentials/PII before returning** (S-3 scans this node's own result). |
| `source_content_gate` | Apply the **quality** gate to fetched text (length and character ratios). Credential/PII redaction happens upstream in `source_fetch` — see the note below. |
| `metrics_compute` | Deterministic aggregation. |
| `narrative_draft` | The single LLM call. |

**Config threading.** `GraphNode` does not receive `self.config` from the framework, so
`ReportComposeNode.__init__(self, config: dict)` takes it explicitly and stores it as `self._config`
(static configuration, not mutable per-invocation state — the node contract forbids caching request data on the
node). `ProjectProgressReportGraph.register_nodes()` passes `self.config` when constructing the
node, and `get_subgraph()` hands the same dict to `DomainWorkflowGraph(config=...)`.
`_validate_config()` on the outer graph fails compile when `source_sets`, `output_schemas`,
`quality_gate` or `provenance` is missing or is not a mapping — a `ConfigError` at compile, never a
runtime fallback to a default.

> **What the framework already masks, and what it does not.** S-2 masks PII only in
> `_PII_SCAN_FIELDS = ("user_input", "validated_input", "llm_response")`, before `execute()` runs.
> `source_records` is not in that set, so fetched text arrives unmasked — this gate is the only
> thing standing between it and the LLM. Two known limits of the shared detector apply and are
> recorded here rather than worked around: `detect_pii` is **ASCII-only (no NFKC)**, so full-width
> addresses/phone numbers pass through, and it does not detect Japanese personal names. Per the
> project ruling we **use the framework detector only and do not hand-roll a Japanese-name
> supplement** (it adds false positives and maintenance without closing the gap); the residual is a
> framework-level issue, not a template-level one. Consequence for this template: a note saying
> "Tanaka-san is blocked on the API design" (an invented example, not real data) reaches the LLM
> with the name intact, which is also
> what makes the blocker report useful.
>
> **Redaction happens in `source_fetch`, not here (agentcore 1.0.1).** The S-3 gate scans the
> *returning* node's own result, and in 1.0.1 it recurses into `dict`/`list`
> (`detect_credentials_in_value`) rather than looking only at top-level strings as 1.0.0 did.
> A `source_fetch` that returned raw `source_records` would therefore be blocked at its own
> output gate, before any later node could clean them. Redaction is consequently owned by the
> node that first places the text into its return dict — `source_fetch` — which is also what
> the framework's redaction-ownership rule prescribes. `source_content_gate` keeps the quality checks
> (length, control/replacement/printable ratios) that do not require the raw credential.
>
> **Every string leaf of the record, not just `text`.** Character-ratio checks alone would still
> forward an API key or a customer's contact details pasted into an issue body to an external LLM,
> so fetched content is scanned with the framework detectors (`detect_credentials` / `detect_pii` —
> reused, not re-implemented) and **redacted in place before the LLM sees it**. The scope is the
> whole record: a source system puts text wherever it likes, so `locator`, `source_id` and an
> event's `milestone_id` are redacted on the same pass. Redacting `text` alone would leave the
> rest in `source_records` and therefore in State, where a later node inspecting them is already
> too late — this node's return value *is* the boundary, and the framework gate on it scans for
> credentials only, never PII.
>
> **Structural tokens are scanned individually, and this is not a second detector.** The shared
> detectors are written for prose and match a whole token, so `taro@example.com` is found in
> `tracker/taro@example.com` but **not** in `tracker/2026Q3_taro@example.com_progress.xlsx` — the
> trailing `_progress.xlsx` is absorbed into the domain, the pattern fails, and a whole-string
> scan reports the value clean. Metadata is exactly where that shape occurs. The record is
> therefore split on structural separators (`/`, `\`, whitespace, `_`) and each token handed to
> the same shared detector. No pattern is authored here; the substrate still makes every decision.
>
> The redacted text is what `narrative_draft` receives *and* what claim verification quotes
> against, so a `quoted_span` can never reproduce a secret that was removed. If a re-scan of any
> redacted leaf still matches a detector, the node **fails the whole run** with
> `E_SOURCE_REDACTION_INCOMPLETE` rather than dropping that one record: a detector that fired and
> could not be satisfied means the redaction path itself is not doing what the rest of this design
> assumes, and continuing with the remaining records would hide that.
>
> **Why the quality gate is two-stage.** Issue bodies and progress notes only exist *after*
> `source_fetch`, so the outer `pre_process` cannot inspect them; and the framework S-2 scan
> covers `user_input` / `validated_input` / `llm_response` only — it does not walk
> `source_records`. `source_content_gate` therefore sits between fetch and the LLM, and no
> fetched text reaches `narrative_draft` without passing it. Records failing the gate are
> excluded (`quality_gate.on_failure: exclude_record`) and listed in `rejected[]` with a reason
> code; the run continues on the remaining records.

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END

main (GraphNode) delegates to the inner workflow graph:
  source_fetch → source_content_gate → metrics_compute → narrative_draft
```

**No retry edge is used.** Failures are treated as **terminal within an invocation**, and retrying
is left to the caller or the deployment. Most failure classes here are deterministic — unknown id,
malformed envelope, schema violation — and re-running the same input reproduces them, so an in-graph
retry would only spend budget. `E_SOURCE_FETCH` and `E_LLM_UNAVAILABLE` are the exceptions: they can
arise from a transient external condition and a retry might well succeed. They are still terminal
here, because the caller knows its own retry budget and idempotency requirements and this graph does
not. `max_retry` stays in `config.yaml` because the framework reads it,
but this template never returns `AgentStatus.RETRY`.

**Division of labour (the core design constraint).** Every number *in a narrative prose field* that
survives into the report **equals some value in the deterministically computed metric set — or, when
written with a `%` sign, normalises to one** (a metric of `0.5` accepts `50%` and rejects `51%`).
State the relation exactly: what is checked is numeric membership under that normalisation, not
origin. The check cannot show that the
model took a figure from `metrics_compute`, and it does not bind a figure to the metric it names — a
sentence about milestone progress passes carrying `0.5` when `0.5` happens to be the velocity. What
it does rule out is a number that matches no metric at all.

The scope is narrative prose specifically: `report_meta` carries digits of its own — the period
bounds and `schema_version` — which come from the validated request and from configuration, and are
not subject to this check. `narrative_draft` receives the
computed metrics as given values and is prompted to reference them rather than compute anything.

Note the mechanism precisely: the model **can** emit a digit sequence, and the constraint is applied
afterwards. `post_process` rejects any element containing a number that is not a member of the set of
metric values, and the S-3 hook applies the same test to the assembled document — so an invented
figure does not reach the caller. What this does not do is bind a figure to the metric it names; see
*What the provenance check does and does not establish*.

### State Definition

| Field | Type | Purpose | Producer | Required |
|-------|------|---------|----------|----------|
| `user_input` | `str` | Raw request envelope (JSON string) | caller | yes |
| `validated_input` | `dict` | `{project_id, period_start_utc, period_end_utc, source_set_id, output_schema_id}` | pre_process | yes |
| `rejected` | `list[dict]` | `{stage, locator, reason_code}` for refused inputs/records | pre_process, source_content_gate | yes (init `[]`) |
| `source_records` | `list[dict]` | Canonical record shape — see *Canonical source record / event schema*. every string leaf is **redacted** — `text`, `locator`, `source_id` and each event's `milestone_id` — by `source_fetch` before it returns | source_fetch → source_content_gate | yes (init `[]`) |
| `metrics` | `dict` | Computed figures (see *Metric definitions*) | metrics_compute | yes |
| `narrative` | `dict` | LLM draft: prose fields **each carrying its own `evidence` list** — see *LLM output contract*. Evidence travels inside `narrative`, so there is no separate `claims` channel to keep in sync | narrative_draft | yes |
| `unresolved` | `list[dict]` | `{field_path, reason_code}` for claims that failed verification | post_process | yes (init `[]`) |
| `formatted_output` | `dict` | Final target-schema JSON | post_process | yes |
| `status` | `str` | `AgentStatus.<X>.value` | every node | yes |

On a successful path every list field is initialised to `[]` by its producer, so a consumer does not
have to distinguish "empty" from "not written". That is not the whole safety story: on an error path
a node can return before writing its own key, so the initial graph state and the `[]` defaults that
`get_output()` and the consuming nodes apply are what actually make absence harmless.

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types)
- No JWT, API keys, credentials in State (checkpoint DB leakage)
- InvocationContext via `InvocationContext.from_state(state)` only (not in State)
- No Pydantic models, dataclass, arbitrary Python objects (msgpack incompatible)

### Request envelope contract

`user_input` is a JSON object. Keys outside this table, or values of the wrong type, are rejected
at the **envelope** level (`status: error`) — never coerced, because coercing a malformed request
produces a confident report over the wrong data.

| Key | Type | Required | Rule | Error code |
|-----|------|----------|------|-----------|
| `project_id` | `str` | yes | non-empty, ≤ 200 chars | `E_PROJECT_ID_INVALID` |
| `period_start` | `str` | yes | RFC 3339 **with explicit offset** | `E_PERIOD_FORMAT` |
| `period_end` | `str` | yes | RFC 3339 with explicit offset | `E_PERIOD_FORMAT` |
| `source_set_id` | `str` | yes | key of `source_sets` in `config.yaml` | `E_SOURCE_SET_UNKNOWN` |
| `output_schema_id` | `str` | yes | key of `output_schemas` | `E_SCHEMA_UNKNOWN` |
| (any other key) | — | — | rejected | `E_UNKNOWN_KEY` |

**Period determinism.** Both bounds require an explicit UTC offset — a bare local timestamp is
rejected (`E_PERIOD_FORMAT`) rather than interpreted against the host's zone, which is what makes
the result independent of where the agent runs. `pre_process` converts them **once** to
`period_start_utc` / `period_end_utc` and every downstream node reads only those. The interval is
**half-open `[start, end)`**. `end <= start` is `E_PERIOD_RANGE`. No node calls "now".

### Canonical source record / event schema

Adapters differ; `metrics_compute` must not. Each adapter **normalises** its own transport rows to
the schema below and returns canonical-shaped records. `SourceFetchNode._canonicalise` then
**independently enforces** that contract on whatever it received, in this order: it validates the
exact key set and field types (`E_ADAPTER_CONTRACT`), **redacts**, then rejects two records sharing
a `source_id` with differing content (`E_DUPLICATE_CONFLICT`) and orders by `source_id` so the
downstream figures do not depend on adapter ordering.

The order is load-bearing. Redaction rewrites `source_id` along with every other string leaf, so two
records whose identifiers differ only inside redacted text collapse onto the same identifier.
De-duplicating *after* redaction turns that collapse into `E_DUPLICATE_CONFLICT`; de-duplicating
first would let the second record silently overwrite the first in every `source_id`-keyed lookup
downstream. The division matters: the adapter knows its transport, but the node is what
`metrics_compute` relies on, so a new adapter cannot weaken the contract by getting its own
normalisation wrong.

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `source_id` | `str` | yes | Stable id, unique within the run |
| `locator` | `str` | yes | Human-resolvable pointer (URL / sheet+row) |
| `fetched_at` | `str` | yes | RFC 3339 UTC, supplied by the adapter (not by the metric path) |
| `text` | `str` | yes | Redacted free text used by the LLM and by span verification |
| `events` | `list[dict]` | yes | `{event_ts (RFC3339 UTC), kind: opened\|closed, estimate (number\|null), milestone_id (str\|null)}` |
| `open_at_period_start` | `bool` | yes | Snapshot fact the adapter resolves; `close_rate`'s denominator cannot be derived from in-period events alone |

**Duplicate handling**: two records sharing a `source_id` are collapsed **only when byte-identical**;
differing duplicates are rejected (`E_DUPLICATE_CONFLICT`) rather than resolved by adapter order,
because "last writer wins" would make the figures depend on fetch ordering.

**Estimates**: numeric; fractional values are permitted and summed as `Decimal`. `null` counts as
missing and increments `metrics.estimate_missing_count`.

### Metric definitions (deterministic)

Computed over records whose event timestamp falls in `[period_start_utc, period_end_utc)`.

| Metric | Definition | Zero-denominator rule | Rounding |
|--------|-----------|----------------------|----------|
| `close_rate` | closed-in-period ÷ (open-at-start + opened-in-period) | denominator 0 → `null` + `rejected` note; never 0.0 | `Decimal`, ROUND_HALF_UP, 3 dp |
| `velocity` | Σ estimate of items closed in period | no items → `0` (a true zero, distinct from `null`) | `Decimal`, ROUND_HALF_UP, 1 dp |
| `milestone_progress` | closed ÷ total for each milestone touched in period. Denominator = milestone membership **as of `period_end_utc`** | total 0 → milestone omitted | `Decimal`, ROUND_HALF_UP, 3 dp |

`Decimal` is used **inside the computation only**; values are converted to JSON numbers after
`quantize()` before they enter State, which stays JSON-serialisable as §*State Constraints* requires.

Records are de-duplicated by `source_id` and sorted by `(event_ts, source_id)` before
aggregation, so adapter ordering cannot change a figure. Missing estimates count as 0 and are
counted in `metrics.estimate_missing_count` rather than being silently dropped.

### LLM output contract

The **LLM draft schema is separate from the target schema** (`config/schemas/sprint_status_v1.json`):
the draft carries evidence that the final report does not.

`narrative_draft` requires exactly:

```
{ "summary":    {"text": str, "evidence": [{"source_id": str, "quoted_span": str}]},
  "risks":      [{"description": str, "evidence": [...]}],
  "blockers":   [{"description": str, "evidence": [...]}],
  "next_steps": [{"text": str, "evidence": [...]}] }
```

Each draft element's `evidence` is **projected into the target document as `source_ids`** — every
prose element in the final report (**including `summary` and each `next_steps` entry**) carries the
ids it was verified against. The `quoted_span` stays internal: it is the verification input, and
echoing it would duplicate source text into the deliverable.

What a reader can do with `source_ids` is narrower than it looks, and the limit is stated in full
under *What the provenance check does and does not establish*: the id is unique within the run and
the record's `locator` is not carried into the report either, so resolving one back to a readable
record needs a mapping the caller retains.

Every prose element carries its **own** `evidence` list, and each entry pairs one `source_id` with
the `quoted_span` drawn from *that* source — a single span shared across several ids cannot be
verified against any of them. Unknown keys, wrong types, or unparseable output are **rejected, not
repaired**: `E_LLM_CONTRACT`. The prompt embeds the schema literally (naming a schema without
including it is a known way to get an empty-but-valid document back).

**Granularity, stated exactly.** Evidence is validated **per element**, not per sentence: every
entry in an element's `evidence` list must name a known record and quote a span from it, and that
qualifies the element as a whole. Numeric tokens are handled on a separate axis — each is compared
against the set of metric values, independently of which evidence entry is present. There is no
sentence-to-evidence mapping, so a multi-sentence element is not decomposed and no individual
sentence is "covered by" a particular citation.

**Failure handling is per element, and the required fields are handled explicitly** — deleting a
field that the target schema requires would only trade an unverified claim for an invalid document:

| Element | On verification failure |
|---|---|
| `risks[]`, `blockers[]`, `next_steps[]` item | Drop **that item**; record it in `unresolved`. The arrays may legitimately be empty. |
| `summary` (required by the target schema) | **Fail the run** with `E_SUMMARY_UNVERIFIED`. It is not replaced by placeholder text, because a report whose summary silently became boilerplate is worse than no report. |

After removals, the document is validated against the target schema **again**; a document that
fails the second validation fails the run.

### Configurable surfaces — executable contract

| Surface | Contract |
|---|---|
| **Source set** (`source_set_id`) | **Required, no default.** Value must be a key of `source_sets` in `config/config.yaml` (allowlist, currently `gitlab_issues_v1`, `spreadsheet_progress_v1`). Absent/unknown → `E_SOURCE_SET_UNKNOWN` at `pre_process`. Adapters are **read-only**; there is no write mode, so no mode-specific RBAC contract is needed. A missing or non-mapping `source_sets` block is a `ConfigError` at compile time, not a runtime fallback. |
| **Output schema** (`output_schema_id`) | **Required, no default**, same rule (`E_SCHEMA_UNKNOWN`). Schemas are versioned and validated at `post_process`; a non-conforming document fails the run. `forbid_person_evaluation: true` is enforced by a schema lint test — this is what backs the EU AI Act Annex III determination recorded below. |

### Source authorization

A `VERIFIED_EXTERNAL` caller is authenticated, not authorised for every project. Without an
explicit rule, any verified caller could name any `project_id` and receive whatever the service
credential can see — an IDOR through the agent.

`pre_process` therefore checks the requested `project_id` against the caller's permitted set
before any fetch, and rejects with `E_PROJECT_FORBIDDEN`. The permitted set is resolved from
`ctx.caller_id` (**not** `session_id`, which is caller-supplied and echoed verbatim) against the
`allowed_projects` mapping in `config/config.yaml`. A caller absent from that mapping has no
permitted projects — fail-closed, not open. Each source set additionally declares its own
non-secret connection settings and the secret name it needs; a source set whose secret is
unavailable fails with `E_SOURCE_CREDENTIAL_UNAVAILABLE` rather than falling back to an
unauthenticated read.

### LLM injection seam

Neither a model ID string nor a concrete client class appears anywhere in this template. The graph
reads `self.config.get("llm")` and passes the instance down; nodes take `llm: BaseLLM | None` and
import only the `BaseLLM` type. The platform injects the real client, so "the pinned model is
outdated" cannot become a defect of this template.
`narrative` is mandatory, so unlike best-effort
enrichment templates this one fails with `E_LLM_UNAVAILABLE` when the seam is empty rather than
degrading silently.

### Secrets and availability

`narrative` is mandatory, so the LLM is mandatory (`llm_required: true`). When no client can be
constructed the run fails with `E_LLM_UNAVAILABLE` rather than substituting an empty narrative,
because a structurally valid but empty document is the failure that silently passes. (An empty
`risks`, `blockers` or `next_steps` array is a legitimate result when the model found none; what is
excluded is producing one *in place of* an unavailable model.)

`config/agent.yaml` leaves `requires.secrets: []` deliberately, and the reason is that this
template never resolves an LLM credential itself. `wire_services()` accepts an already-constructed
`BaseLLM` and places it at `runtime_config["llm"]`; no node calls `ctx.secrets.require()` for a
provider key. `requires.secrets` is a **compile-time** gate — every name listed there must resolve
before the graph will start — so listing a key this template does not read would make the agent
unstartable for a deployment that injects a client needing no such key. Source-set credentials are
different: they are named per source set and required at invocation time, and their absence is
reported at run time as `E_SOURCE_CREDENTIAL_UNAVAILABLE`. An absent LLM is likewise a run-time
condition, reported as `E_LLM_UNAVAILABLE`.

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (correlation_id, session_id, caller_trust_level, secrets)
- [ ] ConnectionPolicy (retry/timeout strategy)
- [ ] SecurityViolationError — not raised directly by this template; the S-2 hook returns an error
      state and the S-3 hook raises `RuntimeError` per the framework contract
- [x] S-2: `_extra_security_gate_input()` — envelope key/type allowlist + quality gate on
      envelope-supplied text (the framework PII scan runs first and is not replaced)
- [x] S-3: `_extra_security_gate_output()` — see the contract note below
- [x] S-4: `emit_trace_event()` — at least one domain event inside every `execute()`

> **S-3 hook signature — a hard constraint, not a preference.** The real signature is
> `_extra_security_gate_output(self, result: dict)`. **It does not receive `state`.** Therefore
> the hook cannot read `claims` / `source_records`, and a design that assumes it can forces the
> implementer into a fail-open branch. The split is:
>
> - **`execute()` does the claim→source verification** (it has the full state) and removes any
>   field whose claim failed, recording it in `unresolved`.
> - **The hook re-checks the result independently**, without trusting anything `execute()` wrote:
>   it walks every free-text leaf of `formatted_output` recursively and raises when it finds a
>   figure that is not present in `formatted_output["metrics"]`, or a credential/PII pattern.
>   It does **not** branch on a "verified" flag set by `execute()` — a gate that trusts a value
>   written by the code it is gating provides no independent assurance.
>
> **The framework scan reaches this document, but only for credentials.** On agentcore 1.0.1
> `_security_gate_output` calls `detect_credentials_in_value`, which recurses through `dict` /
> `list` / `tuple` and scans every string leaf — so a nested `dict` like `formatted_output` *is*
> scanned. (On 1.0.0 it was not: that version iterated `result.items()` and skipped every
> non-`str` value. The behaviour is version-specific. This template does **not** pin the framework
> in `pyproject.toml` — it is not resolvable from public PyPI and must be installed separately — so
> the statement here is verified against the installed wheel rather than taken from a declaration or
> a document.)
> What the framework scan does **not** do is PII detection or figure verification — it looks for
> credentials only. The hook therefore still walks the nested document itself and applies
> `detect_pii` and the figure check per leaf; for credentials it is defence in depth behind the
> framework gate, not the only line.

> **S-2/S-3 gate behaviour by node type:**
> - `FunctionNode` subclass → framework `@final` gate always runs automatically;
>   extend via `_extra_security_gate_input()` / `_extra_security_gate_output()` only
> - `GraphNode` / `RemoteAgentNode` → deliberate no-op (upstream or remote node's gate already applied)
> - Custom `BaseNode` subclass → must implement `_security_gate_input()` and
>   `_security_gate_output()` directly (`@abstractmethod` — omission raises `TypeError`)

### Composition Pattern

- **Pattern**: **GraphNode (subgraph)** — `main` delegates to the inner workflow graph
- **Composition target**: `src/graph/domain_workflow_graph.py`, class `DomainWorkflowGraph`,
  inheriting **`BaseGraph`** (its custom four-node topology is `source_fetch` →
  `source_content_gate` → `metrics_compute` → `narrative_draft`)
- **Error propagation strategy**: **propagate** (framework default; no `on_subgraph_error()`)

`ReportComposeNode(GraphNode)` implements the three abstract methods the SDK requires — it does
**not** override `execute()`:

| Method | Contract here |
|--------|--------------|
| `get_subgraph()` | Return the cached `DomainWorkflowGraph` constructed and compiled once in `__init__`, passing `self.config`. |
| `extract_input(state) -> str` | The SDK types this as **`str`**, so `validated_input` is serialised with `json.dumps(..., sort_keys=True, ensure_ascii=False)`. The inner graph parses it back in its own `pre_process`. Sorted keys keep the serialisation deterministic. |
| `merge_output(state, sub_result) -> dict` | Map **explicitly**, field by field: `metrics`, `narrative`, `source_records`, and append the inner `rejected` entries. No `**sub_result` pass-through — an unmapped inner field must not reach parent state. |

### Output surfacing

`AgentBaseGraph.get_output()` returns the standard envelope
(`output` / `status` / `trace_id` / `correlation_id` / `node_history`) and its `output` does not
carry `unresolved`. An unverified-claim list the caller never sees is worthless, so
`ProjectProgressReportGraph` overrides `get_output()` **while preserving that envelope** — it
calls `super().get_output()` and replaces only the `output` member:

```
{ ...standard envelope...,
  "output": {"report": ..., "unresolved": [...], "rejected": [...], "errors": [...]} }
```

Dropping `status` / `trace_id` / `node_history` would make success and failure indistinguishable to
the caller, so the override never rebuilds the dict from scratch.

On failure the envelope's `status` is `error`. The reason is carried in **`output.errors`**, not in
`error_log`: the framework's `get_output()` does not copy `error_log` into the envelope, so a node
that records why it failed would otherwise have that reason reach the audit event stream and
nothing else. The override therefore surfaces the node-recorded messages as `output.errors`,
de-duplicated and with any traceback tail stripped, so that a caller receives the `E_*` code that
explains the failure. Tests assert that `unresolved` reaches the caller when non-empty, that the
`E_*` code appears in `output.errors`, and that the standard envelope keys survive the override.

## EU AI Act Art.13 Design-Time Evidence

This agent is determined **not in scope** for EU AI Act Annex III, on the basis of its intended
purpose: it reports the state of a project — issue throughput, velocity, milestone progress, risks
and blockers — and is not intended to evaluate people or to inform employment decisions.

Two supporting facts, and one limit that must be stated with them:

- The output schemas provide **no per-person evaluation surface**. There is no field for an
  individual's score, ranking or performance, and `test_schema_lint_forbids_person_evaluation_surface`
  fails if a schema introduces a property named for one.
- Figures are computed deterministically from fetched records and are project-level.
- **The narrative fields are free text and are not content-classified.** Nothing in this template
  detects or removes an evaluative sentence about a named person: a source note saying
  "Employee A is underperforming" can be drafted into a risk and will pass schema validation,
  provenance verification and the S-3 hook. The determination rests on intended purpose and the
  absence of a dedicated evaluation field — **not** on an enforced inability to write such a
  sentence.

A deployment that used this agent's output to inform decisions about individuals would be operating
it outside the purpose this determination is based on, and would need its own controls and its own
scope assessment.
Recorded as ordinary design documentation.

| Evidence item | Design reference / description |
|---------------|--------------------------------|
| Intended purpose and operating context | Internal project-status reporting for a project-management audience; read-only over the team's own tracker and notes. |
| System capabilities and limitations | Structured JSON only. Metric figures are computed deterministically, and a number in narrative prose is kept only if it equals one of those metric values, or normalises to one when written as a percentage (membership, not proof of origin); prose is LLM-authored with source references. No dedicated structured evaluation field (enforced by the `forbid_person_evaluation` schema lint), and the free-text narrative is **not** content-classified — see the limitation above. The agent performs no write-back to any source system. It is intended for reporting project state, not for making scheduling or staffing decisions; nothing in it prevents free text from *mentioning* one, which is why that is stated as intended purpose rather than as an enforced restriction. |
| User-facing transparency information | Every LLM-authored field carries `source_ids`; unverifiable assertions appear in `unresolved` rather than as fact. |
| Human oversight mechanism | Output is a draft for a human reporter who remains the report's author. No autonomous action follows. |

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (no agents/base/ required)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | Fixed pipeline, termination not agent-decided. |
| Composition pattern | Standalone | GraphNode (inner graph) | **GraphNode** | Three stages with different trust characteristics; keeps LLM output a bounded surface. |
| Figure authorship | LLM drafts numbers too | Deterministic metrics, LLM prose only | **Deterministic** | Reproducibility is the template's value. |
| Source/schema selection | Optional with default | Required + allowlist | **Required + allowlist** | A silently defaulted source set produces a confident report over the wrong data. |
| Quality gate placement | `pre_process` only | Two-stage (envelope + fetched content) | **Two-stage** | Fetched text does not exist at `pre_process`, and the framework S-2 scan does not walk `source_records`. |
| Claim verification placement | S-3 hook reads state | `execute()` verifies, hook re-checks result independently | **Split** | The hook's real signature is `(self, result)` with no `state`; a design assuming otherwise forces a fail-open branch. |
| Unverified claim handling | Emit with a warning flag | Remove from body, list in `unresolved` | **Remove** | A flagged-but-present assertion still reads as fact in the rendered report. |

### Pre-empt set check

> A cross-check of this design against six findings that recurred on earlier sibling designs,
> applied here before review rather than after. "Applies?" records whether each one is relevant
> to this template; items recorded as **No** are stated rather than omitted, so that a reader can
> tell a considered non-application from an oversight.

| # | Pre-empt item | Applies? | How it is handled here |
|---|---|---|---|
| 1 | provenance must not stop at "the ID exists" | **Yes** | Three checks: `source_id` exists in `source_records`; `quoted_span` is a literal substring of that record's text with `min_quoted_span_chars: 12` (an empty or 1-char span passes `in` vacuously); every figure in a narrative field must appear in `metrics` with `numeric_tolerance: 0`. **Bidirectional**: claims→sources, and every free-text field is scanned for figures with no metric entry, so "write the number only in prose and cite nothing" does not evade it. NFKC normalisation on both sides before matching. See *Figure extraction* below for why the extractor cannot use word-boundary lookarounds. |
| 2 | persistent KB governance contract | **No** | No persistent KB. Each invocation reads sources, produces a document, retains nothing. Recorded as not-applicable rather than omitted. |
| 3 | validation plan carried in the design | **No (deliberately)** | Pilot plans and prospective-customer commitments are kept out of this document. What is recorded here is the verification the template can prove about itself — the test specification in `docs/03_test_spec.md` — rather than a deployment programme it cannot. |
| 4 | inheritance row names the base class only | **Yes** | §*Position* names `AgentBaseGraph (L1 direct)`; the inner graph appears under *Composition Pattern*. |
| 5 | external-source confirmation carries a date | **Yes, and the honest answer is that this design asserts none** | Neither shipped adapter is claimed to be verified against a live system. Both `GitLabIssueAdapter` and `SpreadsheetProgressAdapter` raise `RuntimeError("... transport is not wired")` until a deployment injects a transport, so what this template ships is the contract and the redaction/verification path around it, not a working connection. Confirming that a particular source system is reachable, and on what date, belongs to whoever wires the transport. |
| 6 | input allowlist + quality gate | **Yes** | Envelope key/type allowlist plus the **two-stage** quality gate. The template accepts text only and ships no binary-format parser, so no magic-byte sniffing is required — that attack surface is absent rather than guarded. |

> **What the provenance check does and does not establish.** Read the three checks literally.
> `source_id` must exist; `quoted_span` must be a literal substring of that record's text and at
> least `min_quoted_span_chars` long; every figure in the prose must appear in `metrics`. Three
> things follow, each of which has to be stated or the guard reads stronger than it is:
>
> 1. **No semantic check.** Nothing compares the meaning of the prose against the span it cites.
>    A sentence carrying no unapproved figure and citing a real, long-enough span from a real record
>    is accepted **whether or not the span supports it** — `"the project is in trouble"` cited
>    against a span reading `"everything is on schedule"` passes every check.
> 2. **Figures are checked against the whole metric set, not against the metric they name.**
>    `_figures_allowed` tests membership in the set of every numeric value anywhere in `metrics`,
>    so a figure attached to the wrong metric passes as long as some metric holds that value.
> 3. **Neither the span nor a resolvable pointer reaches the reader.** `_verify_element` projects
>    `source_ids` only: the `quoted_span` is consumed during verification and the record's `locator`
>    is not carried into the report either. A `source_id` is unique within the run, not a durable
>    address, so resolving one back to a record is the caller's or the deployment's responsibility
>    and needs a mapping the caller retains. Without that, `source_ids` records *that* a citation
>    was checked, not *what* it was.
>
> So the guard establishes a bounded form of **attributability** — each statement names a real
> record, and no number survives that neither equals a computed metric value nor normalises to one
> as a percentage — and nothing more.
> Describing it as preventing fabrication would overstate it.
>
> A span that matches the source exactly also still carries whatever the source said. This
> template's scope makes that acceptable — it reports project status and states no regulatory
> conclusion — but the guard must not be described as preventing unwanted conclusions either.

**Figure extraction.** The number extractor must **not** rely on `(?<![\w.])\d+(?![\w.])`-style
boundaries. Python's `\w` matches Japanese characters, so `999999円` / `999999です` / `１２３４５６円`
all extract to the empty set and the guard then silently passes everything. NFKC-normalise first,
then match digits without word-boundary lookarounds.

**Free-text fields covered by the S-3 recursive walk** (a guard applied to the main field only
always leaks through the side fields):
`narrative.summary.text`, `narrative.risks[].description`, `narrative.blockers[].description`,
`narrative.next_steps[].text` — the four paths in `_FREE_TEXT_PATHS`. The hook walks the assembled document recursively rather than reading
this list, so a newly added narrative field is covered by default; the list is the review anchor,
and a test fails when a narrative schema field is absent from it.

### Error codes (complete set, confirmed against the implementation)

The envelope table above names the codes reachable from `pre_process`. The full set the agent can
emit — including the ones introduced while implementing the stages this document specifies —
is below. It is derived from `src/` by parsing the source and collecting only string **literals**
that match `E_[A-Z0-9_]+` exactly — not by substring search, which had produced three codes the
agent never emits (`E_AUTH_TOKEN`, `E_KEYS` and `E_TEXT_PATHS` are tails of the identifiers
`INVOKE_AUTH_TOKEN`, `_ENVELOPE_KEYS` and `_FREE_TEXT_PATHS`). A code appearing in the
implementation but not here is a defect, and so is one appearing here but not in the
implementation.

| Code |
|---|
| `E_ADAPTER_CONTRACT` |
| `E_DUPLICATE_CONFLICT` |
| `E_ENVELOPE_JSON` |
| `E_ENVELOPE_TYPE` |
| `E_EVIDENCE_MISSING` |
| `E_EVIDENCE_SOURCE_UNKNOWN` |
| `E_EVIDENCE_SPAN_MISMATCH` |
| `E_EVIDENCE_SPAN_SHORT` |
| `E_FIGURE_UNVERIFIED` |
| `E_LLM_CONTRACT` |
| `E_LLM_UNAVAILABLE` |
| `E_METRICS_INPUT` |
| `E_PERIOD_FORMAT` |
| `E_PERIOD_RANGE` |
| `E_PROJECT_FORBIDDEN` |
| `E_PROJECT_ID_INVALID` |
| `E_PROVENANCE_CONFIG` |
| `E_QUALITY_CONTROL_RATIO` |
| `E_QUALITY_MAX_CHARS` |
| `E_QUALITY_PRINTABLE_RATIO` |
| `E_QUALITY_REPLACEMENT_RATIO` |
| `E_SCHEMA_REVALIDATION` |
| `E_SCHEMA_UNKNOWN` |
| `E_SCHEMA_VALIDATION` |
| `E_SENSITIVE_REDACTION` |
| `E_SOURCE_CREDENTIAL_UNAVAILABLE` |
| `E_SOURCE_FETCH` |
| `E_SOURCE_REDACTION_INCOMPLETE` |
| `E_SOURCE_SET_UNKNOWN` |
| `E_SUMMARY_UNVERIFIED` |
| `E_UNKNOWN_KEY` |
| `E_ZERO_DENOMINATOR` |

**Recorded as an implementation-confirmed delta (DDR).** The design specified the *behaviour* of
each gate (reject unknown source set, exclude a record failing the quality gate, drop an
unverified claim) but named only some of the codes. Implementation assigned a distinct code per failure
reason so that `rejected[]` / `unresolved[]` entries are machine-classifiable rather than free
text. No behaviour changed; the codes make the already-specified behaviour addressable.

### Test matrix (each contract gets a success and a failure case)

| Contract | Success case | Failure case |
|---|---|---|
| Fractional estimate | 0.5-point items → `velocity` is a fractional JSON number and **passes schema validation** | — (the integer-typed schema that would have rejected it is the defect this row guards) |
| Provenance projection | every prose element in the final document carries `source_ids`, `summary` and `next_steps` included | an element whose evidence failed verification is absent from the body and present in `unresolved` |
| Source-set allowlist | known id → adapter invoked | unknown id → `E_SOURCE_SET_UNKNOWN`, **adapter never invoked** |
| Schema allowlist | known id → validated | unknown id → `E_SCHEMA_UNKNOWN` |
| Envelope keys/types | exact key set → accepted | extra key → `E_UNKNOWN_KEY`; wrong type → error |
| Period determinism | offset-bearing bounds → same result under `TZ=UTC` and `TZ=Asia/Tokyo` | bare local timestamp → `E_PERIOD_FORMAT`; `end <= start` → `E_PERIOD_RANGE` |
| Quality gate (fetched) | clean records pass | U+FFFD-heavy record excluded, **LLM never invoked with it** |
| Redaction ownership | clean record → `redacted_record_count: 0`; PII in `locator` / `source_id` / `milestone_id` → redacted in this node's return, absent from the model prompt and the report | re-scan of the redacted text still matches → `E_SOURCE_REDACTION_INCOMPLETE`, no records returned |
| Metrics | golden fixture → expected figures | zero denominator → `null` (not 0.0); shuffled adapter order → identical figures |
| LLM contract | conforming JSON → narrative | unknown key / malformed JSON → `E_LLM_CONTRACT` |
| Claim verification | cited, quoted, figure in metrics → kept | fabricated `source_id`; span absent from the cited source; 1-char span; figure absent from metrics → each lands in `unresolved` |
| S-3 independence | clean document passes | figure absent from metrics injected directly into the document → hook raises even though `execute()` marked it verified |
| Trust gate | `VERIFIED_EXTERNAL` caller reaches `execute()` | `ANONYMOUS` caller refused **before** `execute()` runs |
| LLM availability | client present → narrative produced | no client → `E_LLM_UNAVAILABLE` (never an empty-narrative success) |
| Output surfacing | `unresolved` non-empty → present in `get_output()` | standard envelope keys (`status`/`trace_id`/`node_history`) survive the override |
| Envelope parsing | valid JSON object → accepted | non-JSON → `E_ENVELOPE_JSON`; JSON array → `E_ENVELOPE_TYPE` |
| Duplicate records | byte-identical duplicates collapse | differing duplicates → `E_DUPLICATE_CONFLICT` (not order-dependent) |
| Removal then re-validation | item dropped → document still schema-valid | unverified `summary` → `E_SUMMARY_UNVERIFIED` (no placeholder text) |
| Manifest import | `config/agent.yaml` `class` imports successfully | — (guards the manifest and the class name from diverging) |
| Source authorization | permitted `project_id` → fetch proceeds | project outside the caller's set → `E_PROJECT_FORBIDDEN`, **adapter never invoked** |
| Secret redaction | credential in issue body → redacted before the LLM call | assert the raw secret is never present in the LLM mock's received prompt |
