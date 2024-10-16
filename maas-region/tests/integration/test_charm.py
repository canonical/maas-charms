#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

import asyncio
import logging
import time
from pathlib import Path
from subprocess import check_output

import pytest
import yaml
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
APP_NAME = METADATA["name"]


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest):
    """Build the charm-under-test and deploy it together with related charms.

    Assert on the unit status before any relations/configurations take place.
    """
    # create the maas snap cohort we need
    cohort_creation = check_output(
        ["sudo", "snap", "create-cohort", "maas"], universal_newlines=True
    )
    logger.info(f"Created cohort: {cohort_creation}")

    # Build and deploy charm from local source folder
    charm = await ops_test.build_charm(".")

    # Deploy the charm and wait for waiting/idle status
    await asyncio.gather(
        ops_test.model.deploy(
            charm, application_name=APP_NAME, config={"tls_mode": "termination"}
        ),
        ops_test.model.wait_for_idle(
            apps=[APP_NAME], status="waiting", raise_on_blocked=True, timeout=1000
        ),
    )


@pytest.mark.abort_on_fail
async def test_database_integration(ops_test: OpsTest):
    """Verify that the charm integrates with the database.

    Assert that the charm is active if the integration is established.
    """
    await asyncio.gather(
        ops_test.model.deploy(
            "postgresql",
            application_name="postgresql",
            channel="14/stable",
            trust=True,
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


@pytest.mark.abort_on_fail
async def test_tls_mode(ops_test: OpsTest):
    """Verify that the charm tls_mode configuration option works as expected.

    Assert that the agent_service is properly set up.
    """
    # Deploy the charm and haproxy and wait for active/waiting status
    await asyncio.gather(
        ops_test.model.deploy(
            "haproxy",
            application_name="haproxy",
            channel="latest/stable",
            trust=True,
        ),
        ops_test.model.wait_for_idle(
            apps=["haproxy"], status="active", raise_on_blocked=True, timeout=1000
        ),
    )
    await ops_test.model.integrate(f"{APP_NAME}", "haproxy")
    # the relation may take some time beyond the above await to fully apply
    start = time.time()
    timeout = 30
    while True:
        try:
            show_unit = check_output(
                f"JUJU_MODEL={ops_test.model.name} juju show-unit haproxy/0",
                shell=True,
                universal_newlines=True,
            )
            result = yaml.safe_load(show_unit)
            services_str = result["haproxy/0"]["relation-info"][1]["related-units"][
                "maas-region/0"
            ]["data"]["services"]
            break
        except KeyError:
            time.sleep(1)
            if time.time() > start + timeout:
                pytest.fail("Timed out waiting for relation data to apply")

    services_yaml = yaml.safe_load(services_str)

    assert len(services_yaml) == 2
    assert services_yaml[1]["service_name"] == "agent_service"
    assert services_yaml[1]["service_port"] == 80
    agent_server = services_yaml[1]["servers"][0]
    assert agent_server[0] == "api-maas-region-maas-region-0"
    assert agent_server[2] == 5240
