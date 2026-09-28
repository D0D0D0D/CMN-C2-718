"""The caller must be able to tell *why* a run failed, not just that it did."""

from tests.helpers import FakeAdapter, make_config, request_envelope
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.graph import ProjectProgressReportGraph


def _run(**cfg_kwargs):
    config = make_config(adapter=FakeAdapter(), **cfg_kwargs)
    graph = ProjectProgressReportGraph(config=config)
    graph.compile()
    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, caller_id="caller-a")
    return graph.invoke(request_envelope(), session_id="s1", ctx=ctx)


def test_standard_envelope_survives_the_override():
    result = _run()
    for key in ("status", "trace_id", "node_history"):
        assert key in result, f"{key} was dropped by the get_output override"


def test_failure_reason_reaches_the_caller():
    """A denied project must surface its reason code, not just status=error."""
    result = _run(project_id="other-project")
    assert result["status"] == "error"
    surfaced = (
        " ".join(result["output"]["rejected"][0].values())
        if result["output"]["rejected"]
        else " ".join(result["output"]["errors"])
    )
    assert "E_" in surfaced, (
        "the caller received status=error with no reason; error_log is not in the standard "
        "envelope, so the override must surface it"
    )


def test_errors_are_deduplicated_and_traceback_free():
    result = _run(project_id="other-project")
    errors = result["output"]["errors"]
    assert len(errors) == len(set(errors)), "error_log is an additive channel; de-duplicate it"
    assert not any("Traceback" in e for e in errors), "tracebacks must not reach the caller"
