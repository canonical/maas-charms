# Copyright 2024 Canonical
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import json
import socket
import subprocess
import unittest
from unittest.mock import PropertyMock, patch

import ops
import ops.testing
import yaml
from charms.maas_region.v0 import maas
from charms.maas_site_manager_k8s.v0 import enroll
from charms.operator_libs_linux.v2.snap import SnapError

from charm import (
    MAAS_API_RELATION,
    MAAS_DB_NAME,
    MAAS_HTTP_PORT,
    MAAS_PEER_NAME,
    MAAS_PROXY_PORT,
    MAAS_SNAP_CHANNEL,
    MaasRegionCharm,
)


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
                "password": "my_secret",
            },
        )
        mock_helper.setup_region.assert_called_once_with(
            f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS",
            "postgres://test_maas_db:my_secret@30.0.0.1:5432/maas_region_db",
            "region",
        )

    @patch("charm.MaasHelper", autospec=True)
    def test_database_connected_creates_admin(self, mock_helper):
        mock_helper.set_prometheus_metrics.return_value = None
        mock_helper.create_admin_user.return_value = None
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
                "password": "my_secret",
            },
        )
        credentials = self.harness.model.get_secret(label="maas-admin").get_content()
        self.assertEqual(credentials["username"], "maas-admin-internal")

    @patch("charm.MaasHelper", autospec=True)
    def test_database_removed(self, mock_helper):
        self.harness.begin()
        db_rel = self.harness.add_relation(MAAS_DB_NAME, "postgresql")
        self.harness.remove_relation(db_rel)
        mock_helper.stop.assert_called_once()

    @patch("charm.MaasHelper", autospec=True)
    def test_database_removed_error(self, mock_helper):
        mock_helper.stop.side_effect = SnapError()
        self.harness.begin()
        db_rel = self.harness.add_relation(MAAS_DB_NAME, "postgresql")
        self.harness.remove_relation(db_rel)


class TestMsmEnroll(unittest.TestCase):
    REMOTE_APP = "msm-k8s"

    def setUp(self):
        self.harness = ops.testing.Harness(MaasRegionCharm)
        self.harness.add_network("10.0.0.10")
        self.addCleanup(self.harness.cleanup)

    def _enroll(self, rel_id: int, jwt: str):
        secret_id = self.harness.add_model_secret(self.REMOTE_APP, {enroll.TOKEN_SECRET_KEY: jwt})
        self.harness.grant_secret(secret_id, self.harness.model.app)
        databag = {}
        app_data = enroll.EnrollProviderAppData(secret_id)
        app_data.dump(databag)
        self.harness.update_relation_data(rel_id, self.REMOTE_APP, databag)

    @patch("charm.MaasHelper", autospec=True)
    def test_enroll(self, mock_helper):
        mock_helper.get_maas_uuid.return_value = "MAAS-CLUSTER-UUID"
        self.harness.set_leader(True)
        self.harness.begin()

        # send enrollment request
        rel_id = self.harness.add_relation(enroll.DEFAULT_ENDPOINT_NAME, self.REMOTE_APP)

        self.assertEqual(
            self.harness.get_relation_data(rel_id, self.harness.model.app),
            {"uuid": "MAAS-CLUSTER-UUID"},
        )
        # mock enrollment data from MSM
        self._enroll(rel_id, "TOKEN")

        data = self.harness.get_relation_data(rel_id, self.REMOTE_APP)
        self.assertIn("token_id", data)  # codespell:ignore
        token = self.harness.model.get_secret(id=data["token_id"]).get_content()
        self.assertEqual(token["enroll-token"], "TOKEN")
        mock_helper.msm_enroll.assert_called_once_with(token["enroll-token"])

    @patch("charm.MaasHelper", autospec=True)
    def test_enroll_only_leader(self, mock_helper):
        mock_helper.get_maas_uuid.return_value = "MAAS-CLUSTER-UUID"
        self.harness.begin()

        # other unit send enrollment request
        rel_id = self.harness.add_relation(enroll.DEFAULT_ENDPOINT_NAME, self.REMOTE_APP)

        self.assertEqual(
            self.harness.get_relation_data(rel_id, self.harness.model.app),
            {},
        )
        # mock enrollment data from MSM
        self._enroll(rel_id, "TOKEN")
        mock_helper.msm_enroll.assert_not_called()


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
    def test_ha_proxy_data(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        ha = self.harness.add_relation(
            MAAS_API_RELATION, "haproxy", unit_data={"public-address": "proxy.maas"}
        )

        ha_data = yaml.safe_load(self.harness.get_relation_data(ha, "maas-region/0")["services"])
        self.assertEqual(len(ha_data), 1)
        self.assertIn("service_name", ha_data[0])  # codespell:ignore
        self.assertIn("service_host", ha_data[0])  # codespell:ignore
        self.assertEqual(len(ha_data[0]["servers"]), 1)
        self.assertEqual(ha_data[0]["servers"][0][1], "10.0.0.10")

    @patch("charm.MaasHelper", autospec=True)
    def test_ha_proxy_data_tls_termination(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.update_config({"tls_mode": "termination"})
        self.harness.begin()
        ha = self.harness.add_relation(
            MAAS_API_RELATION, "haproxy", unit_data={"public-address": "proxy.maas"}
        )

        ha_data = yaml.safe_load(self.harness.get_relation_data(ha, "maas-region/0")["services"])
        self.assertEqual(len(ha_data), 2)
        self.assertIn("service_name", ha_data[1])  # codespell:ignore
        self.assertIn("service_host", ha_data[1])  # codespell:ignore
        self.assertEqual(len(ha_data[1]["servers"]), 1)
        self.assertEqual(ha_data[1]["servers"][0][1], "10.0.0.10")
        self.assertEqual(ha_data[0]["servers"][0][2], 5240)

    @patch("charm.MaasHelper", autospec=True)
    def test_ha_proxy_data_tls_passthrough(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.update_config(
            {
                "tls_mode": "passthrough",
                "ssl_cert_content": "BEGIN CERTIFICATE",
                "ssl_key_content": "BEGIN_PRIVATE_KEY",
            }
        )
        self.harness.begin()
        ha = self.harness.add_relation(
            MAAS_API_RELATION, "haproxy", unit_data={"public-address": "proxy.maas"}
        )

        ha_data = yaml.safe_load(self.harness.get_relation_data(ha, "maas-region/0")["services"])
        self.assertEqual(len(ha_data), 2)
        self.assertIn("service_name", ha_data[1])  # codespell:ignore
        self.assertIn("service_host", ha_data[1])  # codespell:ignore
        self.assertEqual(len(ha_data[1]["servers"]), 1)
        self.assertEqual(ha_data[1]["servers"][0][1], "10.0.0.10")
        self.assertEqual(ha_data[0]["servers"][0][2], 5443)

    @patch("charm.MaasHelper", autospec=True)
    def test_invalid_tls_mode(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        ha = self.harness.add_relation(
            MAAS_API_RELATION, "haproxy", unit_data={"public-address": "proxy.maas"}
        )
        with self.assertRaises(ValueError):
            self.harness.update_config({"tls_mode": "invalid_mode"})

        ha_data = yaml.safe_load(self.harness.get_relation_data(ha, "maas-region/0")["services"])
        self.assertEqual(len(ha_data), 1)

    @patch("charm.MaasHelper", autospec=True)
    def test_bad_ssl_cert_key_config(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        self.harness.add_relation(
            MAAS_API_RELATION, "haproxy", unit_data={"public-address": "proxy.maas"}
        )
        with self.assertRaises(ValueError):
            self.harness.update_config(
                {"tls_mode": "passthrough", "ssl_cert_content": "test_cert"}
            )

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
            unit_data={"unit": f"{remote_app}/0", "url": "some_url"},
        )
        mock_helper.setup_region.assert_not_called()
        data = self.harness.get_relation_data(rel_id, "maas-region")
        self.assertEqual(data["api_url"], "http://10.0.0.10:5240/MAAS")
        self.assertEqual(data["regions"], f'["{socket.getfqdn()}"]')
        self.assertIn("maas_secret_id", data)  # codespell:ignore

    @patch("charm.MaasHelper", autospec=True)
    def test_on_maas_cluster_changed_prometheus_enabled(self, mock_helper):
        mock_helper.get_maas_mode.return_value = "region"
        mock_helper.get_maas_secret.return_value = "very-secret"
        mock_helper.create_admin_user.return_value = None
        self.harness.set_leader(True)
        self.harness.begin()
        remote_app = "maas-agent"
        self.harness.add_relation(
            maas.DEFAULT_ENDPOINT_NAME,
            remote_app,
            unit_data={"unit": f"{remote_app}/0", "url": "some_url"},
        )
        mock_helper.set_prometheus_metrics.assert_called_with(
            "maas-admin-internal", "10.0.0.10", True
        )

    @patch(
        "charm.MaasRegionCharm.connection_string",
        new_callable=PropertyMock(return_value="postgres://"),
    )
    @patch("charm.MaasHelper", autospec=True)
    def test_ha_proxy_update_api_url(self, mock_helper, _mock_conn_id):
        mock_helper.get_maas_mode.return_value = "region"
        mock_helper.get_maas_secret.return_value = "very-secret"
        self.harness.set_leader(True)
        self.harness.begin()
        self.harness.add_relation(
            MAAS_API_RELATION, "haproxy", unit_data={"public-address": "proxy.maas"}
        )
        mock_helper.setup_region.assert_called_once_with(
            f"http://proxy.maas:{MAAS_PROXY_PORT}/MAAS",
            "postgres://",
            "region",
        )

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
            unit_data={"unit": f"{remote_app}/0", "url": my_fqdn},
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
            unit_data={"unit": f"{remote_app}/0", "url": "some_url"},
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
            unit_data={"unit": f"{remote_app}/0", "url": my_fqdn},
        )
        self.harness.begin()
        self.harness.remove_relation_unit(rel_id, f"{remote_app}/0")
        mock_helper.setup_region.assert_called_once_with(
            f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS",
            "postgres://",
            "region",
        )

    @patch("charm.MaasHelper", autospec=True)
    def test_config_change_prometheus_updated(self, mock_helper):
        mock_helper.get_installed_version.return_value = "mock-ver"
        mock_helper.get_installed_channel.return_value = MAAS_SNAP_CHANNEL
        mock_helper.set_prometheus_metrics.return_value = None
        mock_helper.create_admin_user.return_value = None
        self.harness.set_leader(True)
        self.harness.begin_with_initial_hooks()
        # make admin secret be set
        db_rel = self.harness.add_relation(MAAS_DB_NAME, "postgresql")
        self.harness.update_relation_data(
            db_rel,
            "postgresql",
            {
                "endpoints": "30.0.0.1:5432",
                "read-only-endpoints": "30.0.0.2:5432",
                "username": "test_maas_db",
                "password": "my_secret",
            },
        )
        self.harness.update_config({"enable_prometheus_metrics": False})
        mock_helper.set_prometheus_metrics.assert_called_with(
            "maas-admin-internal", "10.0.0.10", False
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
        mock_helper.create_admin_user.side_effect = subprocess.CalledProcessError(1, "maas")
        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action(
                "create-admin",
                {"username": "my_user", "password": "my_secret", "email": "my_email"},
            )
        err = e.exception
        self.assertEqual(err.message, "Failed to create user my_user")

    @patch("charm.MaasHelper", autospec=True)
    def test_get_api_key_action(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        mock_helper.get_api_key.return_value = "aaa.bb.cccc\n"

        output = self.harness.run_action("get-api-key", {"username": "my_user"})

        self.assertEqual(output.results["api-key"], "aaa.bb.cccc")
        mock_helper.get_api_key.assert_called_once_with("my_user")

    @patch("charm.MaasHelper", autospec=True)
    def test_get_api_key_action_fail(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        mock_helper.get_api_key.side_effect = subprocess.CalledProcessError(1, "maas")
        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("get-api-key", {"username": "my_user"})
        err = e.exception
        self.assertEqual(err.message, "Failed to get key for user my_user")

    def test_get_api_endpoint_action(self):
        self.harness.set_leader(True)
        self.harness.begin()
        output = self.harness.run_action("get-api-endpoint")
        self.assertEqual(output.results["api-url"], "http://10.0.0.10:5240/MAAS")

    def test_list_controllers_action_solo(self):
        self.harness.set_leader(True)
        self.harness.begin()
        output = self.harness.run_action("list-controllers")
        self.assertEqual(json.loads(output.results["regions"]), [socket.getfqdn()])
        self.assertEqual(json.loads(output.results["agents"]), [])

    def test_list_controllers_action_complex(self):
        self.harness.set_leader(True)
        rel_id = self.harness.add_relation(
            MAAS_PEER_NAME, "maas-region", unit_data={"system-name": json.dumps(socket.getfqdn())}
        )
        self.harness.add_relation_unit(rel_id, "maas-region/1")
        self.harness.update_relation_data(
            rel_id, "maas-region/1", {"system-name": json.dumps("other.host.local")}
        )
        self.harness.add_relation(
            maas.DEFAULT_ENDPOINT_NAME,
            "maas-agent",
            unit_data={"unit": "maas-agent/0", "url": "agent.local"},
        )
        self.harness.begin()
        output = self.harness.run_action("list-controllers")
        self.assertCountEqual(
            json.loads(output.results["regions"]), [socket.getfqdn(), "other.host.local"]
        )
        self.assertCountEqual(json.loads(output.results["agents"]), ["agent.local"])

    def test_create_backup_action(self) -> None:
        self.harness.set_leader(True)
        self.harness.begin()
        backup_type = "full"

        output = self.harness.run_action("create-backup", params={"type": backup_type})
        self.assertEqual(output.results, {"backup-id": "backup-id"})

    def test_create_backup_action_fail_no_type(self) -> None:
        self.harness.set_leader(True)
        self.harness.begin()

        with self.assertRaises(KeyError) as e:
            self.harness.run_action("create-backup")
        self.assertEqual(e.exception.args, ("type",))

    def test_create_backup_action_fail_wrong_type(self) -> None:
        self.harness.set_leader(True)
        self.harness.begin()
        backup_type = "unknown"

        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("create-backup", params={"type": backup_type})

        err = e.exception
        self.assertEqual(err.message, f"Unknown backup type: '{backup_type}'")

    def test_restore_from_backup_action(self) -> None:
        self.harness.set_leader(True)
        self.harness.begin()
        self.harness.run_action("restore-from-backup", params={"backup-id": "backup_id"})

    def test_restore_from_backup_action_fail_no_id(self) -> None:
        self.harness.set_leader(True)
        self.harness.begin()

        with self.assertRaises(KeyError) as e:
            self.harness.run_action("restore-from-backup")
        self.assertEqual(e.exception.args, ("backup-id",))

    def test_list_backups_action(self) -> None:
        self.harness.set_leader(True)
        self.harness.begin()
        output = self.harness.run_action("list-backups")
        self.assertEqual(
            output.results,
            {"backups": {"backup-id": {"timestamp": "backup-time", "type": "backup-type"}}},
        )
