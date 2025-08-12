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
async def test_charm_version_is_set(ops_test: OpsTest):
    status = await ops_test.model.get_status()
    version = status.applications[APP_NAME].charm_version
    assert version.startswith("3.6.")


@pytest.mark.abort_on_fail
async def test_database_integration(ops_test: OpsTest):
    """Verify that the charm integrates with the database.

    Assert that the charm is active if the integration is established.
    """
    await asyncio.gather(
        ops_test.model.deploy(
            "postgresql",
            application_name="postgresql",
            channel="16/beta",
            series="noble",
            trust=True,
            # workaround for https://bugs.launchpad.net/maas/+bug/2097079
            config={"plugin_audit_enable": False},
            # workaround for https://bugs.launchpad.net/maas/+bug/2097079, https://github.com/canonical/postgresql-operator/issues/1001
            revision=758,
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
            series="noble",
            trust=True,
        ),
        ops_test.model.wait_for_idle(
            apps=["haproxy"], status="active", raise_on_blocked=True, timeout=1000
        ),
    )
    await ops_test.model.integrate(f"{APP_NAME}", "haproxy")

    # the relation may take some time beyond the above await to fully apply
    start = time.time()
    timeout = 1000
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
