# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import functools
from unittest.mock import patch

import pytest
from charmlibs import pathops

import charm


@pytest.fixture(autouse=True)
def rolling_ops_base_dir(tmp_path):
    """Point rollingops at a writable directory.

    It defaults to /var/lib/rollingops, which is not writeable in test environments.
    """
    manager = functools.partial(
        charm.RollingOpsManager, base_dir=pathops.LocalPath(tmp_path / "rollingops")
    )
    with patch.object(charm, "RollingOpsManager", manager):
        yield
