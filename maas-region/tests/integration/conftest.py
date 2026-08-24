# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import yaml
from _pytest.config.argparsing import Parser


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


METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
APP_NAME = METADATA["name"]
POSTGRESQL_CHANNEL = "16/stable"
HAPROXY_CHANNEL = "2.8/edge"
