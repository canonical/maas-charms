import asyncio

import pytest
from conftest import APP_NAME, MAAS_INIT_RELATION, MAAS_PEER_NAME, POSTGRESQL_CHANNEL
from pytest_operator.plugin import OpsTest


@pytest.mark.abort_on_fail
async def test_multi_node_build(ops_test: OpsTest):
    """Build the charm-under-test and deploy it together with related charms.

    Assert on the unit status before any relations/configurations take place.
    """
    charm = await ops_test.build_charm(".")

    await asyncio.gather(
        ops_test.model.deploy(
            charm,
            application_name=APP_NAME,
            config={"tls_mode": "termination"},
            num_units=3,
        ),
        ops_test.model.wait_for_idle(
            apps=[APP_NAME], status="waiting", raise_on_blocked=True, timeout=1000, wait_for_exact_units=3,
        ),
    )


@pytest.mark.abort_on_fail
async def test_maas_peer_relations(ops_test: OpsTest):
    """Verify that the charm establishes its peer relations when multiple units are deployed.

    Assert that the relations are established when the units are related.
    """
    if ops_test.model is None:
        raise ValueError("Model is not set")

    relation_names = [
        relation.endpoints[0].name
        for relation in ops_test.model.relations
        if relation.is_peer
    ]
    assert (
        MAAS_PEER_NAME in relation_names
    ), f"'{MAAS_PEER_NAME}' peer relation not found. Relations: {relation_names}"

    assert (
        MAAS_INIT_RELATION in relation_names
    ), f"'{MAAS_INIT_RELATION}' peer relation not found. Relations: {relation_names}"



@pytest.mark.abort_on_fail
async def test_multi_node_database_integration(ops_test: OpsTest):
    """Verify that the charm integrates with the database when multiple units are deployed.

    Assert that the charm is active if the integration is established.
    """
    await asyncio.gather(
        ops_test.model.deploy(
            "postgresql",
            application_name="postgresql",
            channel=POSTGRESQL_CHANNEL,
            series="noble",
            trust=True,
            # workaround for https://bugs.launchpad.net/maas/+bug/2097079
            config={"plugin_audit_enable": False},
        ),
        ops_test.model.wait_for_idle(
            apps=["postgresql"], status="active", raise_on_blocked=True, timeout=1000
        ),
    )

    await asyncio.gather(
        ops_test.model.integrate(f"{APP_NAME}", "postgresql"),
        ops_test.model.wait_for_idle(
            apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=1000
        ),
    )
