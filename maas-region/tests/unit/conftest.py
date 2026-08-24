# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import patch

import pytest
from charmlibs.rollingops._peer._worker import PeerRollingOpsAsyncWorker


@pytest.fixture(autouse=True)
def no_rolling_ops_worker():
    """Prevent rollingops from spawning a real worker subprocess.

    The worker requires the charm's deployed virtualenv, which does not exist in
    test environments. Tests drive the lock lifecycle by emitting the
    rollingops_lock_granted event themselves.
    """
    with patch.object(PeerRollingOpsAsyncWorker, "start", autospec=True):
        yield
