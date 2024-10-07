from unittest.mock import patch

import opentelemetry
import pytest
from charms.tempo_k8s.v2.tracing import ProtocolType, Receiver, TracingProviderAppData
from scenario import Context, Relation, State

from charm import MaasRackCharm


@pytest.fixture
def tracing_relation():
    db = {}
    TracingProviderAppData(
        receivers=[
            Receiver(
                url="http://foo.com:81",
                protocol=ProtocolType(name="otlp_http", type="http"),
            )
        ]
    ).dump(db)
    tracing = Relation("tracing", remote_app_data=db)
    return tracing


def test_charm_tracing_config(tracing_relation):
    ctx = Context(MaasRackCharm)
    state_in = State(relations=[tracing_relation])
    with patch(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter.export"
    ) as f:
        f.return_value = opentelemetry.sdk.trace.export.SpanExportResult.SUCCESS
        state_out = ctx.run(ctx.on.collect_unit_status(), state_in)

    assert state_out.relations == {tracing_relation}

    span = f.call_args_list[0].args[0][0]
    assert span.resource.attributes["service.name"] == "maas-agent-charm"
    assert span.resource.attributes["compose_service"] == "maas-agent-charm"
    assert span.resource.attributes["charm_type"] == "MaasRackCharm"
