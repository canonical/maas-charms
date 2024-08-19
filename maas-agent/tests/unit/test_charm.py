# Copyright 2024 Canonical
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import socket
import unittest
from typing import List
from unittest.mock import patch

import ops
import ops.testing
from charms.maas_region.v0 import maas

from charm import MAAS_RACK_PORTS, MAAS_SNAP_CHANNEL, MaasRackCharm


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = ops.testing.Harness(MaasRackCharm)
        self.harness.add_network("10.0.0.10")
        self.addCleanup(self.harness.cleanup)

    @patch("charm.MaasHelper", autospec=True)
    def test_start(self, mock_helper):
        mock_helper.get_installed_version.return_value = "mock-ver"
        mock_helper.get_installed_channel.return_value = MAAS_SNAP_CHANNEL
        self.harness.begin_with_initial_hooks()
        self.harness.evaluate_status()
        mock_helper.install.assert_called_once_with(MAAS_SNAP_CHANNEL)
        mock_helper.set_running.assert_called_once_with(True)
        mock_helper.get_installed_version.assert_called_once()
        mock_helper.get_installed_channel.assert_called_once()
        self.assertEqual(
            self.harness.model.unit.status, ops.WaitingStatus("Waiting for enrollment token")
        )
        self.assertEqual(self.harness.get_workload_version(), "mock-ver")

    @patch("charm.MaasHelper", autospec=True)
    def test_remove(self, mock_helper):
        mock_helper.get_maas_mode.return_value = "rack"
        self.harness.begin()
        self.harness.charm.on.remove.emit()
        mock_helper.uninstall.assert_called_once()


class TestEnrollment(unittest.TestCase):
    def setUp(self):
        self.remote_app = "maas-region"
        self.agent_name = socket.getfqdn()
        self.maas_secret = "my_secret"
        self.api_url = "http://region:5240/MAAS"
        self.harness = ops.testing.Harness(MaasRackCharm)
        self.harness.add_network("10.0.0.10")
        self.harness.set_leader(True)
        self.harness.begin()
        self.addCleanup(self.harness.cleanup)

    def _enroll(self, rel_id: int, regions: List[str]):
        secret_id = self.harness.add_model_secret(
            self.remote_app, {"maas-secret": self.maas_secret}
        )
        self.harness.grant_secret(secret_id, "maas-agent")
        app_data = maas.MaasProviderAppData(
            api_url=self.api_url,
            regions=regions,
            maas_secret_id=secret_id,
        )
        databag = {}
        app_data.dump(databag)
        self.harness.update_relation_data(rel_id, self.remote_app, databag)

    @patch("charm.MaasHelper", autospec=True)
    def test_enrollment(self, mock_helper):
        # send enrollment request
        rel_id = self.harness.add_relation(maas.DEFAULT_ENDPOINT_NAME, self.remote_app)
        self.assertEqual(
            self.harness.get_relation_data(rel_id, self.harness._unit_name),
            {"unit": "maas-agent/0", "url": self.agent_name},
        )
        mock_helper.setup_rack.assert_not_called()
        # mock enrollment data from region
        self._enroll(rel_id, ["region.local"])
        mock_helper.setup_rack.assert_called_once_with(self.api_url, self.maas_secret)
        self.assertCountEqual(self.harness.model.unit.opened_ports(), MAAS_RACK_PORTS)

    @patch("charm.MaasHelper", autospec=True)
    def test_enrollment_region_plus_rack(self, mock_helper):
        # send enrollment request
        rel_id = self.harness.add_relation(maas.DEFAULT_ENDPOINT_NAME, self.remote_app)
        self.assertEqual(
            self.harness.get_relation_data(rel_id, self.harness._unit_name),
            {"unit": "maas-agent/0", "url": self.agent_name},
        )
        mock_helper.setup_rack.assert_not_called()
        # mock enrollment data from region
        self._enroll(rel_id, [self.agent_name])
        mock_helper.setup_rack.assert_not_called()
        self.assertCountEqual(self.harness._backend.opened_ports(), [])
