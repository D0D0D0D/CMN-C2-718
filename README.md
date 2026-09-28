# CMN-C2-718 — ProjectProgressReportAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: CMN

## Overview

ProjectProgressReportAgent turns project-tracking records and unstructured progress notes into a
project-progress / sprint-status report emitted as structured JSON conforming to a configurable target
schema. Numeric metrics — issue close rate, velocity, milestone progress — are computed deterministically
from the fetched records; the language model writes only the four narrative groups — summary, risks,
blockers and next steps.

Every narrative element must name a fetched record and quote a literal span from it, and every number
appearing in the prose must equal one of the computed metric values, or normalise to one when it is
written with a `%` sign (a metric of `0.5` accepts `50%`). An element failing either check
is removed from the report body and listed under `unresolved` rather than published as fact.

It is worth being precise about what that does and does not establish, because the checks are
narrower than they sound:

- The model *may* write a number; it is checked afterwards. What the check enforces is that no
  number survives that is not already a metric value — but it compares against **all** metric values
  at once, so it does not confirm that a figure was attached to the metric it actually describes.
- The output carries `source_ids` and nothing else about the citation: the quoted span is used for
  verification and dropped, and the record's locator is not carried either. A `source_id` is unique
  within a single run rather than a durable address, so turning one back into a readable record is
  the caller's responsibility and needs a mapping the caller keeps.
- Nothing compares the *meaning* of a sentence to the span it cites. A statement that contradicts
  its own citation is accepted as long as it carries no unapproved number. There is no semantic
  entailment check and none is claimed.

Rendering to spreadsheet or slide formats, and any write-back to the source systems, are out of scope.
The two source adapters that ship here define the read contract but carry no transport: each raises
until a deployment injects one, so adapting this template means wiring your own source access.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- a `BaseLLM` implementation as `config["llm"]`. No provider client is bundled and no model is
  named. The narrative stage is mandatory (`llm_required: true`), so with no client the run ends
  as `E_LLM_UNAVAILABLE` rather than emitting a report whose narrative fields are simply absent;
- a read-only transport for each source adapter you enable. The two adapters that ship here define
  the read contract and carry no transport of their own — each raises until a deployment injects
  one, so adapting this template means wiring your own source access;
- source credentials through the runtime secret provider, named per source set
  (`secret_name`). An unavailable credential is `E_SOURCE_CREDENTIAL_UNAVAILABLE`;
- caller-to-project grants in `allowed_projects`;
- caller authentication in front of the entry adapter.

Runtime parameters read from `config/config.yaml`:

| Key | Meaning |
|---|---|
| `source_sets` | Allowlist of readable source sets. A request's `source_set_id` must be one of these keys — an unknown id is `E_SOURCE_SET_UNKNOWN`, never defaulted. Adding a key here is the only way to make a new source reachable |
| `allowed_projects` | `{caller_id: [project_id, ...]}`. Resolved from `ctx.caller_id`, never from the caller-supplied `session_id`. A caller absent from this mapping has no permitted projects — absence denies |
| `output_schemas` | Allowlist of versioned target schemas. The assembled JSON is validated against the named schema; an unknown id is `E_SCHEMA_UNKNOWN`, and a version is never inferred from payload shape |
| `quality_gate` | Bounds on text that may reach the model: per-field length, control-character and replacement-character ratios, minimum printable ratio, and whether a failure excludes the record or fails the run |
| `provenance` | `min_quoted_span_chars` (below which `span in source_text` is vacuously true), `numeric_tolerance` (`0` — figures must match exactly), and what happens to an unverified claim |
| `llm_required` (`true`) | Whether a missing model client fails the run |
| `max_retry` (`3`), `timeout_s` (`30`) | Framework retry budget and timeout |

The template does not select a model, bundle a provider client, parse PDF or spreadsheet binaries,
write back to any source system, or make staffing and individual-evaluation decisions.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is the agent's import-time dependency: `src/` imports `framework.*` and `shared.*`
directly, so with no SDK installed the process does not start at all. That is what "does not run
standalone" means here — there is no degraded mode that runs without the framework.

With the SDK installed but no platform around it, the standalone server in `src/api/server.py`
does come up, and it is worth being exact about what it can and cannot do:

- `/health` answers `{"status": "ok", "agent": "ProjectProgressReportAgent"}`.
- Start-up calls `wire_services()` with no arguments, which loads `config/config.yaml` and the
  manifest but injects **no language-model client and no source adapters**. An authenticated
  `/invoke` therefore ends as `E_LLM_UNAVAILABLE`, and would fail at source fetch even before
  that once adapters are reached. `wire_services(config=..., llm=..., adapters=...)` is the seam a
  deployment — or a test — uses to supply them.
- The entry tokens are read from the framework's secret provider, not from the process
  environment: `INVOKE_AUTH_TOKEN` promotes an anonymous caller to `VERIFIED_EXTERNAL`, and the
  separate `STG_INTERNAL_RUNNER_TOKEN` maps exactly to `INTERNAL`. The external token can never
  reach `INTERNAL`. When neither is configured the caller stays `ANONYMOUS` and is refused by the
  node trust gate instead of at the door.

The test suite runs without a platform connection, using injected fakes for the model and the
source adapters.

## Input

`POST /invoke` takes a JSON body with `input` and an optional `session_id`. `input` is itself a
JSON string carrying the request envelope, which is closed: exactly these five keys, no more.

| Field | Meaning |
|---|---|
| `project_id` | The project to report on. Must appear in the caller's `allowed_projects` entry |
| `period_start`, `period_end` | Reporting period. Both must carry an explicit UTC offset; they are converted deterministically rather than assumed to be local time |
| `source_set_id` | One of the `source_sets` keys |
| `output_schema_id` | One of the `output_schemas` keys |

Anything else fails closed with a stable reason code rather than being coerced: a body that is not
JSON (`E_ENVELOPE_JSON`), a non-object (`E_ENVELOPE_TYPE`), a missing or extra key (`E_KEYS`,
`E_UNKNOWN_KEY`), a malformed or reversed period (`E_PERIOD_FORMAT`, `E_PERIOD_RANGE`), an unknown
id (`E_SOURCE_SET_UNKNOWN`, `E_SCHEMA_UNKNOWN`), or a project the caller is not granted
(`E_PROJECT_FORBIDDEN`) — the last of these is decided *before* any source is read.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "{\"project_id\": \"proj-1\", \"period_start\": \"2026-07-01T00:00:00+00:00\", \"period_end\": \"2026-07-14T00:00:00+00:00\", \"source_set_id\": \"gitlab_issues_v1\", \"output_schema_id\": \"sprint_status_v1\"}",
    "session_id": "sprint-42"
  }'
```

## Output

The standard AgentCore envelope is preserved. `output` is the schema-validated report document —
`report_meta`, `metrics` and `narrative` for `sprint_status_v1` — and the run also carries:

- `unresolved` — narrative elements removed from the report body after evidence verification
  failed, kept so that a thin report is visibly thin rather than quietly short;
- `rejected` — records and metric notes refused along the way, each with its stage, locator and a
  stable reason code (a source record failing the quality gate, a metric with a zero denominator,
  a rejected envelope).

`metrics` figures are computed; `narrative` prose is model-authored and carries `source_ids`.

## Security and Limitations

Every domain node requires verified-external trust, so an anonymous caller is stopped before any
node body runs, and project authorization is resolved from `ctx.caller_id` — not from the
caller-supplied `session_id`, which the framework echoes verbatim. Credentials and personal
information are redacted from source text before it reaches the model, and the assembled output is
independently rescanned for credentials, personal information and numbers that are not in the
computed metrics. Adapters are read-only; there is no write-back path to any source system.

Known limits, recorded rather than hidden:

- **The numeric check is membership, not attribution.** A figure in the prose is compared against
  *all* metric values at once, so it confirms the number exists among the metrics — not that it
  belongs to the metric the sentence is about.
- **There is no semantic entailment check.** A sentence that contradicts the span it cites is
  accepted as long as it carries no unapproved number.
- **A `source_id` is not a durable address.** It is unique within one run; the quoted span is used
  for verification and then dropped, and the record locator is not carried, so mapping an id back
  to a readable record needs a mapping the caller keeps.
- **The shared personal-information detector has known gaps**, notably full-width contact data and
  Japanese personal names. This template deliberately does not add a separate language-specific
  detector; deployments should minimise the source text they expose, enforce least-privilege
  source access, and keep human review of generated drafts.
- **`forbid_person_evaluation` constrains the schema, not the prose.** It rejects a target schema
  that introduces a per-person evaluation property. The narrative fields are free text and are not
  content-classified, so it does not prevent an evaluative sentence about a named person from
  being drafted.

Output is machine-generated and assistive. It is intended for reporting project state, not for
making scheduling or staffing decisions, and any decision that matters should be made against the
source records by a person.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), with the domain workflow nested in the
  `main` slot as a subgraph
- Generation mode: `llm` (narrative only; every metric figure is computed)
- HITL: not enabled
- Persistence: none — no checkpointer is configured
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not — see
*Behaviour without the platform* above.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the provenance and claim-verification
design and the security design. `docs/03_test_spec.md` records the test specification and the
verification results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
