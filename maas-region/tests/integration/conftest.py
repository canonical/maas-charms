# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from _pytest.config.argparsing import Parser
from pytest_operator.plugin import OpsTest


def pytest_addoption(parser: Parser):
    parser.addoption(
        "--charm-file",
        action="store",
        help="Path to a pre-built charm file",
    )
    parser.addoption(
        "--series",
        action="store",
        help="Ubuntu series to run tests on",
    )
    parser.addoption(
        "--model-arch",
        action="store",
        help="Architecture constraint to set on the test model (e.g. arm64)",
    )


@pytest_asyncio.fixture(scope="module", autouse=True)
async def model_arch(ops_test: OpsTest, pytestconfig: pytest.Config):
    """Constrain the test model to the requested architecture, if provided."""
    arch = pytestconfig.getoption("--model-arch")
    if arch and ops_test.model is not None:
        await ops_test.model.set_constraints({"arch": arch})


METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
APP_NAME = METADATA["name"]
POSTGRESQL_CHANNEL = "16/stable"
HAPROXY_CHANNEL = "2.8/edge"
