#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

import asyncio
import logging
import re
from pathlib import Path
from subprocess import check_output, run
from time import sleep, time

import pytest
from conftest import APP_NAME, POSTGRESQL_CHANNEL
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)


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
    assert version.startswith("3.7.")


@pytest.mark.abort_on_fail
async def test_database_integration(ops_test: OpsTest):
    """Verify that the charm integrates with the database.

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


def generate_cert(tmp_path: Path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"

    run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=maas.test",
        ],
        check=True,
    )

    return cert.read_text(), key.read_text()


def read_haproxy_blocks(haproxy_config: str) -> dict[str, str]:
    block_pattern = re.compile(
        r"^(global|defaults|frontend|backend|listen|peers)[^\n]*\n"
        r"(?:[ \t].*\n)*",
        re.MULTILINE,
    )
    blocks = {}
    for match in block_pattern.finditer(haproxy_config):
        header = match.group(0).splitlines()[0].strip()
        blocks[header] = match.group(0)
    return blocks


@pytest.mark.abort_on_fail
async def test_haproxy_integration(ops_test: OpsTest, tmp_path):
    """Verify that the charm haproxy integration works as expected."""
    # Deploy the charm and haproxy and wait for active/waiting status
    await ops_test.model.deploy(
        "haproxy",
        application_name="haproxy",
        channel="2.8/edge",
        series="noble",
        trust=True,
        config={"vip": "10.10.0.200"}
    )
    await ops_test.model.wait_for_idle(
        apps=["haproxy"], status="active", raise_on_blocked=True, timeout=1000
    )

    cert, key = generate_cert(tmp_path=tmp_path)
    logger.info(f"cert: {cert}")
    logger.info(f"key: {key}")
    logger.info(ops_test.model.relations)

    await ops_test.model.integrate(f"{APP_NAME}:ingress-tcp", "haproxy")
    await ops_test.model.integrate(f"{APP_NAME}:ingress-tcp-tls", "haproxy")

    logger.info(ops_test.model.relations)

    await ops_test.model.applications[APP_NAME].set_config(
        {"ssl_cert_content": cert, "ssl_key_content": key, "ssl_cacert_content": cert}
    )

    logger.info(await ops_test.model.applications[APP_NAME].get_config())

    await ops_test.model.wait_for_idle(
        apps=["haproxy", APP_NAME], status="active", raise_on_error=False, timeout=1000
    )

    start = time()
    timeout = 1000
    while True:
        try:
            data = check_output(
                "juju exec --unit haproxy/0 cat /etc/haproxy/haproxy.cfg",
                shell=True,
                universal_newlines=True,
            )
            haproxy_data = read_haproxy_blocks(data)
            http_frontend = haproxy_data["frontend haproxy_route_tcp_80"]
            https_frontend = haproxy_data["frontend haproxy_route_tcp_443"]
            http_backend = haproxy_data["backend haproxy_route_tcp_80_default_backend"]
            https_backend = haproxy_data["backend haproxy_route_tcp_443_default_backend"]
            break
        except KeyError:
            sleep(1)
            if time() > start + timeout:
                pytest.fail("Timed out waiting for relation data to apply")

    assert "mode tcp" in http_frontend
    assert "bind [::]:80" in http_frontend
    assert re.search(r"server maas-region-0 (\d+\.){3}\d+:5240", http_backend)

    assert "mode tcp" in https_frontend
    assert "bind [::]:443" in https_frontend
    assert re.search(r"server maas-region-0 (\d+\.){3}\d+:5443", https_backend)
