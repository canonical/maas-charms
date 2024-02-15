# Copyright 2024 Canonical
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import socket
import unittest
from unittest.mock import PropertyMock, patch

import ops
import ops.testing
from charm import MAAS_DB_NAME, MAAS_HTTP_PORT, MAAS_PEER_NAME, MAAS_SNAP_CHANNEL, MaasRegionCharm
from charms.maas_region_charm.v0 import maas


class TestCharm(unittest.TestCase):

    def setUp(self):
        self.harness = ops.testing.Harness(MaasRegionCharm)
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
            self.harness.model.unit.status, ops.WaitingStatus("Waiting for database DSN")
        )
        self.assertEqual(self.harness.get_workload_version(), "mock-ver")

    @patch("charm.MaasHelper", autospec=True)
    def test_remove(self, mock_helper):
        self.harness.begin()
        self.harness.charm.on.remove.emit()
        mock_helper.uninstall.assert_called_once()


class TestDBRelation(unittest.TestCase):

    def setUp(self):
        self.harness = ops.testing.Harness(MaasRegionCharm)
        self.harness.add_network("10.0.0.10")
        self.addCleanup(self.harness.cleanup)

    @patch("charm.MaasHelper", autospec=True)
    def test_database_connected(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        db_rel = self.harness.add_relation(MAAS_DB_NAME, "postgresql")
        self.harness.update_relation_data(
            db_rel,
            "postgresql",
            {
                "endpoints": "30.0.0.1:5432",
                "read-only-endpoints": "30.0.0.2:5432",
                "username": "test_maas_db",
                "password": "my_password",
            },
        )
        mock_helper.setup_region.assert_called_once_with(
            f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS",
            "postgres://test_maas_db:my_password@30.0.0.1:5432/maas_region_db",
            "region",
        )


class TestClusterUpdates(unittest.TestCase):

    def setUp(self):
        self.harness = ops.testing.Harness(MaasRegionCharm)
        self.harness.add_network("10.0.0.10")
        self.addCleanup(self.harness.cleanup)

    def test_peer_relation_data(self):
        self.harness.set_leader(True)
        self.harness.begin()
        app_name = self.harness.charm.app.name
        rel_id = self.harness.add_relation(MAAS_PEER_NAME, app_name)
        self.harness.charm.set_peer_data(self.harness.charm.app, "test_key", "test_value")
        self.assertEqual(
            self.harness.get_relation_data(rel_id, app_name)["test_key"], '"test_value"'
        )
        self.assertEqual(
            self.harness.charm.get_peer_data(self.harness.charm.app, "test_key"), "test_value"
        )
        self.harness.charm.set_peer_data(self.harness.charm.app, "test_key", None)
        self.assertEqual(self.harness.get_relation_data(rel_id, app_name)["test_key"], "{}")

    @patch("charm.MaasHelper", autospec=True)
    def test_on_maas_cluster_changed_new_agent(self, mock_helper):
        mock_helper.get_maas_mode.return_value = "region"
        mock_helper.get_maas_secret.return_value = "very-secret"
        self.harness.set_leader(True)
        self.harness.begin()
        remote_app = "maas-agent"
        rel_id = self.harness.add_relation(
            maas.DEFAULT_ENDPOINT_NAME,
            remote_app,
            unit_data={"model": "my_model", "unit": f"{remote_app}/0", "url": "some_url"},
        )
        mock_helper.setup_region.assert_not_called()
        data = self.harness.get_relation_data(rel_id, "maas-region")
        self.assertEqual(data["api_url"], "http://10.0.0.10:5240/MAAS")
        self.assertEqual(data["regions"], f'["{socket.getfqdn()}"]')
        self.assertIn("maas_secret_id", data)

    @patch(
        "charm.MaasRegionCharm.connection_string",
        new_callable=PropertyMock(return_value="postgres://"),
    )
    @patch("charm.MaasHelper", autospec=True)
    def test_on_maas_cluster_changed_new_agent_same_machine(self, mock_helper, _mock_conn_id):
        mock_helper.get_maas_mode.return_value = "region"
        mock_helper.get_maas_secret.return_value = "very-secret"
        my_fqdn = socket.getfqdn()
        self.harness.set_leader(True)
        self.harness.begin()
        remote_app = "maas-agent"
        self.harness.add_relation(
            maas.DEFAULT_ENDPOINT_NAME,
            remote_app,
            unit_data={"model": "my_model", "unit": f"{remote_app}/0", "url": my_fqdn},
        )
        mock_helper.setup_region.assert_called_once_with(
            f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS",
            "postgres://",
            "region+rack",
        )

    @patch("charm.MaasHelper", autospec=True)
    def test_on_maas_cluster_changed_remove_agent(self, mock_helper):
        mock_helper.get_maas_mode.return_value = "region"
        mock_helper.get_maas_secret.return_value = "very-secret"
        self.harness.set_leader(True)
        remote_app = "maas-agent"
        rel_id = self.harness.add_relation(
            maas.DEFAULT_ENDPOINT_NAME,
            remote_app,
            unit_data={"model": "my_model", "unit": f"{remote_app}/0", "url": "some_url"},
        )
        self.harness.begin()
        self.harness.remove_relation_unit(rel_id, f"{remote_app}/0")
        mock_helper.setup_region.assert_not_called()

    @patch(
        "charm.MaasRegionCharm.connection_string",
        new_callable=PropertyMock(return_value="postgres://"),
    )
    @patch("charm.MaasHelper", autospec=True)
    def test_on_maas_cluster_changed_remove_agent_same_machine(self, mock_helper, _mock_conn_id):
        mock_helper.get_maas_mode.return_value = "region+rack"
        mock_helper.get_maas_secret.return_value = "very-secret"
        my_fqdn = socket.getfqdn()
        self.harness.set_leader(True)
        remote_app = "maas-agent"
        rel_id = self.harness.add_relation(
            maas.DEFAULT_ENDPOINT_NAME,
            remote_app,
            unit_data={"model": "my_model", "unit": f"{remote_app}/0", "url": my_fqdn},
        )
        self.harness.begin()
        self.harness.remove_relation_unit(rel_id, f"{remote_app}/0")
        mock_helper.setup_region.assert_called_once_with(
            f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS",
            "postgres://",
            "region",
        )


class TestCharmActions(unittest.TestCase):

    def setUp(self):
        self.harness = ops.testing.Harness(MaasRegionCharm)
        self.harness.add_network("10.0.0.10")
        self.addCleanup(self.harness.cleanup)

    @patch("charm.MaasHelper", autospec=True)
    def test_create_admin_action(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()

        output = self.harness.run_action(
            "create-admin", {"username": "my_user", "password": "my_secret", "email": "my_email"}
        )

        self.assertEqual(output.results["info"], "user my_user successfully created")
        mock_helper.create_admin_user.assert_called_once_with(
            "my_user", "my_secret", "my_email", None
        )

    @patch("charm.MaasHelper", autospec=True)
    def test_create_admin_action_with_key(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()

        output = self.harness.run_action(
            "create-admin",
            {
                "username": "my_user",
                "password": "my_secret",
                "email": "my_email",
                "ssh-import": "lp:my-id",
            },
        )

        self.assertEqual(output.results["info"], "user my_user successfully created")
        mock_helper.create_admin_user.assert_called_once_with(
            "my_user", "my_secret", "my_email", "lp:my-id"
        )

    @patch("charm.MaasHelper", autospec=True)
    def test_create_admin_action_fail(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        mock_helper.create_admin_user.return_value = False
        with self.assertRaises(ops.testing.ActionFailed):
            self.harness.run_action(
                "create-admin",
                {"username": "my_user", "password": "my_secret", "email": "my_email"},
            )
