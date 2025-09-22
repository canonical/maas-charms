#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

import asyncio
import logging
from pathlib import Path

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
        ops_test.model.deploy(charm, application_name=APP_NAME),
        ops_test.model.wait_for_idle(
            apps=[APP_NAME], status="waiting", raise_on_blocked=True, timeout=1000
        ),
    )


@pytest.mark.abort_on_fail
async def test_charm_version_is_set(ops_test: OpsTest):
    status = await ops_test.model.get_status()
    version = status.applications[APP_NAME].workload_version
    assert version.startswith("3.6.")


@pytest.mark.abort_on_fail
async def test_region_integration(ops_test: OpsTest):
    """Verify that the charm integrates with the database.

    Assert that the charm is active if the integration is established.
    """
    # Deploy the region charm and wait for active/idle status
    await asyncio.gather(
        ops_test.model.deploy(
            "maas-region",
            application_name="maas-region",
            channel="latest/edge",
            series="noble",
            revision=211,
        ),
        ops_test.model.wait_for_idle(
            apps=["maas-region"], status="waiting", raise_on_blocked=True, timeout=1000
        ),
    )

    await asyncio.gather(
        ops_test.model.deploy(
            "postgresql",
            application_name="postgresql",
            channel="16/beta",
            series="noble",
            # workaround for https://bugs.launchpad.net/maas/+bug/2097079
            config={"plugin_audit_enable": False},
            # workaround for https://bugs.launchpad.net/maas/+bug/2097079, https://github.com/canonical/postgresql-operator/issues/1001
            revision=758,
            trust=True,
        ),
        ops_test.model.wait_for_idle(
            apps=["postgresql"], status="active", raise_on_blocked=True, timeout=1000
        ),
    )

    await asyncio.gather(
        ops_test.model.integrate("maas-region", "postgresql"),
        ops_test.model.integrate(f"{APP_NAME}", "maas-region"),
        ops_test.model.wait_for_idle(
            apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=1000
        ),
    )
