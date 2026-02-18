#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

import asyncio
import logging
import re
from pathlib import Path
from subprocess import run
from time import sleep, time

import pytest
from conftest import APP_NAME, HAPROXY_CHANNEL, POSTGRESQL_CHANNEL
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


def generate_cert(ip_address: str, tmp_path: Path):
    ca_key = tmp_path / "ca.key"
    ca_crt = tmp_path / "ca.crt"
    maas_key = tmp_path / "maas.key"
    maas_csr = tmp_path / "maas.csr"
    maas_crt = tmp_path / "maas.crt"
    conf = tmp_path / "maas.conf"

    conf.write_text(f"""\
[ req ]
default_bits       = 4096
prompt             = no
default_md         = sha256
distinguished_name = dn
req_extensions     = req_ext

[ dn ]
CN = {ip_address}

[ req_ext ]
subjectAltName = @alt_names

[ alt_names ]
IP.1 = {ip_address}
""")

    for key in [ca_key, maas_key]:
        run(["openssl", "genrsa", "-out", str(key), "4096"], check=True)

    run(
        [
            "openssl",
            "req",
            "-x509",
            "-new",
            "-nodes",
            "-key",
            str(ca_key),
            "-sha256",
            "-days",
            "1000",
            "-out",
            str(ca_crt),
            "-subj",
            "/CN=MAAS-Integration-CA",
        ],
        check=True,
    )
    run(
        [
            "openssl",
            "req",
            "-new",
            "-key",
            str(maas_key),
            "-out",
            str(maas_csr),
            "-config",
            str(conf),
        ],
        check=True,
    )

    run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(maas_csr),
            "-CA",
            str(ca_crt),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(maas_crt),
            "-days",
            "100",
            "-sha256",
            "-extensions",
            "req_ext",
            "-extfile",
            str(conf),
        ],
        check=True,
    )

    return maas_key.read_text(), ca_crt.read_text(), maas_crt.read_text()


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
        channel=HAPROXY_CHANNEL,
        series="noble",
        trust=True,
        config={"vip": "10.10.0.200"},
    )
    await ops_test.model.wait_for_idle(
        apps=["haproxy", APP_NAME], status="active", raise_on_blocked=True, timeout=1000
    )

    address = await ops_test.model.applications[APP_NAME].units[0].get_public_address()

    key, cacert, cert = generate_cert(ip_address=address, tmp_path=tmp_path)
    await ops_test.model.integrate(f"{APP_NAME}:ingress-tcp", "haproxy")
    await ops_test.model.integrate(f"{APP_NAME}:ingress-tcp-tls", "haproxy")
    await ops_test.model.applications[APP_NAME].set_config({"ssl_cert_content": cert})
    await ops_test.model.applications[APP_NAME].set_config({"ssl_key_content": key})
    await ops_test.model.applications[APP_NAME].set_config({"ssl_cacert_content": cacert})

    await ops_test.model.wait_for_idle(
        apps=["haproxy", APP_NAME], status="active", raise_on_error=False, timeout=1000
    )

    start = time()
    timeout = 1000
    while True:
        try:
            return_code, stdout, _ = await ops_test.juju(
                "exec",
                "--unit",
                "haproxy/0",
                "--",
                "cat",
                "/etc/haproxy/haproxy.cfg",
            )

            if return_code == 0:
                haproxy_data = read_haproxy_blocks(stdout)
                http_frontend = haproxy_data["frontend haproxy_route_tcp_80"]
                https_frontend = haproxy_data["frontend haproxy_route_tcp_443"]
                http_backend = haproxy_data["backend haproxy_route_tcp_80_default_backend"]
                https_backend = haproxy_data["backend haproxy_route_tcp_443_default_backend"]
                break
        except KeyError:
            pass

        if time() > start + timeout:
            pytest.fail("Timed out waiting for relation data to apply")

        sleep(1)

    assert "mode tcp" in http_frontend
    assert "bind [::]:80" in http_frontend
    assert re.search(r"server maas-region-0 (\d+\.){3}\d+:5240", http_backend)

    assert "mode tcp" in https_frontend
    assert "bind [::]:443" in https_frontend
    assert re.search(r"server maas-region-0 (\d+\.){3}\d+:5443", https_backend)
