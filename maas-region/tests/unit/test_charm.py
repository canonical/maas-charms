# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import subprocess
import unittest
from ipaddress import ip_address
from json import dumps
from pathlib import Path
from typing import ClassVar
from unittest.mock import PropertyMock, call, patch

import ops
import ops.testing
import yaml
from charms.maas_site_manager_k8s.v0 import enroll
from charms.operator_libs_linux.v2.snap import SnapError

from charm import (
    HAPROXY_INTERNAL_HTTP_API,
    HAPROXY_NON_TLS,
    HAPROXY_TEMPORAL,
    HAPROXY_TLS,
    MAAS_AGENT_METRICS_ENDPOINT,
    MAAS_AGENT_METRICS_PORT,
    MAAS_CLUSTER_METRICS_PORT,
    MAAS_DB_NAME,
    MAAS_HTTP_PORT,
    MAAS_HTTPS_PORT,
    MAAS_INTERNAL_HTTP_API_PORT,
    MAAS_PEER_NAME,
    MAAS_PROXY_PORT,
    MAAS_REGION_METRICS_PORT,
    MAAS_ROLLING_OPS_RELATION,
    MAAS_SNAP_CHANNEL,
    MAAS_TEMPORAL_PORT,
    MAAS_TLS_PROXY_PORT,
    MAAS_TRACK_BASES,
    MaasRegionCharm,
    _next_track,
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
        self.harness.add_relation(MAAS_ROLLING_OPS_RELATION, "maas-region")

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
        # This is needed to trigger initialize as this normally happens as a separate
        # charm event after the rolling ops data is written to the databag.
        self.harness.charm.on.rollingops_lock_granted.emit()
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
        self.harness.charm.on.rollingops_lock_granted.emit()
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
    def _make_harness(self):
        harness = ops.testing.Harness(MaasRegionCharm)
        harness.add_network("10.0.0.10")
        self.addCleanup(harness.cleanup)
        return harness

    def setUp(self):
        self.harness = self._make_harness()
        self.harness.add_relation(MAAS_ROLLING_OPS_RELATION, "maas-region")

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
    def test_bad_ssl_cert_key_config(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        self.harness.add_relation(
            HAPROXY_NON_TLS, "haproxy", unit_data={"public-address": "proxy.maas"}
        )
        with self.assertRaises(ValueError):
            self.harness.update_config({"ssl_cert_content": "test_cert"})

    def test_invalid_maas_url_config(self):
        """Test that invalid maas_url raises ValueError on config change."""
        invalid_urls = ["not-a-url", "/just/a/path", "http://"]
        for invalid_url in invalid_urls:
            with self.subTest(url=invalid_url):
                harness = self._make_harness()
                harness.begin()
                with self.assertRaises(ValueError) as cm:
                    harness.update_config({"maas_url": invalid_url})
                self.assertIn("Invalid maas_url", str(cm.exception))

    @patch("charm.MaasHelper", autospec=True)
    def test_on_maas_cluster_changed_prometheus_enabled(self, mock_helper):
        mock_helper.get_maas_mode.return_value = "region"
        mock_helper.create_admin_user.return_value = None
        self.harness.set_leader(True)
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
        self.harness.update_config({"enable_rack_mode": False})
        self.harness.begin()
        self.harness.update_config({"enable_rack_mode": True})
        mock_helper.set_prometheus_metrics.assert_called_with(
            "maas-admin-internal", f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS", True, ""
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
            "maas-admin-internal", f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS", False, ""
        )

    @patch("charm.MaasHelper", autospec=True)
    def test_config_change_rack_mode_enabled(self, mock_helper):
        mock_helper.get_installed_version.return_value = "mock-ver"
        mock_helper.get_installed_channel.return_value = MAAS_SNAP_CHANNEL
        mock_helper.setup_region.return_value = None
        mock_helper.create_admin_user.return_value = None
        self.harness.set_leader(True)
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
        self.harness.update_config({"enable_rack_mode": True})
        self.harness.begin_with_initial_hooks()
        mock_helper.setup_region.assert_has_calls(
            [
                call(
                    f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS",
                    "postgres://test_maas_db:my_secret@30.0.0.1:5432/maas_region_db",
                    "region+rack",
                )
            ]
        )

    @patch("charm.MaasHelper", autospec=True)
    def test_config_change_rack_mode_updated(self, mock_helper):
        mock_helper.get_installed_version.return_value = "mock-ver"
        mock_helper.get_installed_channel.return_value = MAAS_SNAP_CHANNEL
        mock_helper.setup_region.return_value = None
        mock_helper.create_admin_user.return_value = None
        self.harness.set_leader(True)
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
        self.harness.update_config({"enable_rack_mode": False})
        self.harness.begin_with_initial_hooks()
        self.harness.update_config({"enable_rack_mode": True})
        mock_helper.setup_region.assert_has_calls(
            [
                call(
                    f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS",
                    "postgres://test_maas_db:my_secret@30.0.0.1:5432/maas_region_db",
                    "region",
                ),
                call(
                    f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS",
                    "postgres://test_maas_db:my_secret@30.0.0.1:5432/maas_region_db",
                    "region+rack",
                ),
            ]
        )

    def test_haproxy_relation__leader_sets_http_data(self):
        self.harness.set_leader(True)

        http_rel_id = self.harness.add_relation(HAPROXY_NON_TLS, "haproxy")
        self.harness.add_relation_unit(http_rel_id, "haproxy/0")
        temporal_rel_id = self.harness.add_relation(HAPROXY_TEMPORAL, "haproxy")
        self.harness.add_relation_unit(temporal_rel_id, "haproxy/0")
        internal_api_rel_id = self.harness.add_relation(HAPROXY_INTERNAL_HTTP_API, "haproxy")
        self.harness.add_relation_unit(internal_api_rel_id, "haproxy/0")
        self.harness.begin()

        with patch.object(
            self.harness.charm.haproxy_non_tls_route,
            "configure_hosts",
        ) as configure_hosts:
            self.harness.charm._reconcile_ha_proxy(None)
            configure_hosts.assert_called_once()

            args = configure_hosts.call_args.args
            self.assertEqual(args, ([ip_address("10.0.0.10")],))

    def test_haproxy_relation__leader_sets_https_data(self):
        self.harness.set_leader(True)

        self.harness.update_config(
            {
                "ssl_cert_content": "placeholder-cert",
                "ssl_key_content": "placeholder-key",
            }
        )

        http_rel_id = self.harness.add_relation(HAPROXY_NON_TLS, "haproxy")
        self.harness.add_relation_unit(http_rel_id, "haproxy/0")

        temporal_rel_id = self.harness.add_relation(HAPROXY_TEMPORAL, "haproxy")
        self.harness.add_relation_unit(temporal_rel_id, "haproxy/0")

        internal_api_rel_id = self.harness.add_relation(HAPROXY_INTERNAL_HTTP_API, "haproxy")
        self.harness.add_relation_unit(internal_api_rel_id, "haproxy/0")

        https_rel_id = self.harness.add_relation(HAPROXY_TLS, "haproxy")
        self.harness.add_relation_unit(https_rel_id, "haproxy/0")

        self.harness.begin()

        with patch.object(
            self.harness.charm.haproxy_tls_route,
            "configure_hosts",
        ) as configure_hosts:
            self.harness.charm._reconcile_ha_proxy(None)
            configure_hosts.assert_called_once()

            args = configure_hosts.call_args.args

            self.assertEqual(args, ([ip_address("10.0.0.10")],))

    def test_haproxy_relation__leader_has_correct_data(self):
        cases = [
            # (http_enabled, https_enabled, tls_enabled, valid)
            (False, False, False, True),  # No HAProxy, MAAS no TLS - Valid
            (False, True, False, False),  # Only HAProxy TLS, MAAS no TLS - Invalid
            (False, False, True, True),  # No HAProxy, MAAS TLS - Valid
            (False, True, True, False),  # Only HAProxy TLS, MAAS TLS - Invalid
            (True, False, False, True),  # All required, no HAProxy TLS, MAAS no TLS - Valid
            (True, True, False, False),  # All required, HAProxy TLS, MAAS no TLS - Invalid
            (True, False, True, False),  # All required, no HAProxy TLS, MAAS TLS - Invalid
            (True, True, True, True),  # All required, HAProxy TLS, MAAS TLS - Valid
        ]
        for http_enabled, https_enabled, tls_enabled, valid in cases:
            with self.subTest(http=http_enabled, https=https_enabled, tls=tls_enabled):
                harness = self._make_harness()
                harness.set_leader(True)

                if http_enabled:
                    http_rel_id = harness.add_relation(HAPROXY_NON_TLS, "haproxy")
                    harness.add_relation_unit(http_rel_id, "haproxy/0")
                    temporal_rel_id = harness.add_relation(HAPROXY_TEMPORAL, "haproxy")
                    harness.add_relation_unit(temporal_rel_id, "haproxy/0")
                    internal_api_rel_id = harness.add_relation(
                        HAPROXY_INTERNAL_HTTP_API, "haproxy"
                    )
                    harness.add_relation_unit(internal_api_rel_id, "haproxy/0")

                if https_enabled:
                    https_rel_id = harness.add_relation(HAPROXY_TLS, "haproxy")
                    harness.add_relation_unit(https_rel_id, "haproxy/0")

                if tls_enabled:
                    harness.update_config(
                        {
                            "ssl_cert_content": "placeholder-cert",
                            "ssl_key_content": "placeholder-key",
                        }
                    )

                harness.begin()

                harness.charm._reconcile_ha_proxy(None)

                if http_enabled:
                    http_data = harness.get_relation_data(http_rel_id, harness.charm.app.name)
                    self.assertEqual(http_data["port"], str(MAAS_PROXY_PORT))
                    self.assertEqual(http_data["backend_port"], str(MAAS_HTTP_PORT))
                    temporal_data = harness.get_relation_data(
                        temporal_rel_id, harness.charm.app.name
                    )
                    self.assertEqual(temporal_data["port"], str(MAAS_TEMPORAL_PORT))
                    self.assertEqual(temporal_data["backend_port"], str(MAAS_TEMPORAL_PORT))
                    internal_api_data = harness.get_relation_data(
                        internal_api_rel_id, harness.charm.app.name
                    )
                    self.assertEqual(internal_api_data["port"], str(MAAS_INTERNAL_HTTP_API_PORT))
                    self.assertEqual(
                        internal_api_data["backend_port"], str(MAAS_INTERNAL_HTTP_API_PORT)
                    )
                    # hosts are only set if topology is valid
                    if valid:
                        self.assertEqual(http_data["hosts"], dumps(["10.0.0.10"]))
                        self.assertEqual(temporal_data["hosts"], dumps(["10.0.0.10"]))
                        self.assertEqual(internal_api_data["hosts"], dumps(["10.0.0.10"]))
                    else:
                        self.assertNotIn("hosts", http_data)
                        self.assertNotIn("hosts", temporal_data)
                        self.assertNotIn("hosts", internal_api_data)

                if https_enabled:
                    https_data = harness.get_relation_data(https_rel_id, harness.charm.app.name)
                    self.assertEqual(https_data["port"], str(MAAS_TLS_PROXY_PORT))
                    self.assertEqual(https_data["backend_port"], str(MAAS_HTTPS_PORT))
                    # hosts are only set if topology is valid
                    if valid:
                        self.assertEqual(https_data["hosts"], dumps(["10.0.0.10"]))
                    else:
                        self.assertNotIn("hosts", https_data)

    @patch("charm.MaasHelper", autospec=True)
    def test_haproxy_relation__reported_statuses(self, mock_helper):
        mock_helper.get_installed_version.return_value = "mock-ver"
        mock_helper.get_installed_channel.return_value = MAAS_SNAP_CHANNEL

        cases = [
            # (maas_tls, relations, expected_status_message)
            (
                False,
                [HAPROXY_TLS],
                "Invalid HAProxy configuration: "
                f"Cannot have `{HAPROXY_TLS}` relation when MAAS TLS is not enabled; "
                "Set the `ssl_cert_content` and `ssl_key_content` configuration options.",
            ),
            (
                True,
                [HAPROXY_NON_TLS, HAPROXY_TEMPORAL, HAPROXY_INTERNAL_HTTP_API],
                f"Invalid HAProxy configuration: Missing `{HAPROXY_TLS}` relation "
                "when MAAS TLS is enabled.",
            ),
            (
                True,
                [HAPROXY_TLS],
                "Invalid HAProxy configuration: "
                f"`{HAPROXY_TLS}` relation requires all base relations: "
                f"`{HAPROXY_NON_TLS}`, `{HAPROXY_TEMPORAL}`, and `{HAPROXY_INTERNAL_HTTP_API}`.",
            ),
            (
                False,
                [HAPROXY_NON_TLS],
                "Invalid HAProxy configuration: "
                f"All of `{HAPROXY_NON_TLS}`, `{HAPROXY_TEMPORAL}`, and `{HAPROXY_INTERNAL_HTTP_API}` "
                "relations must be present together if any are provided.",
            ),
            (
                False,
                [HAPROXY_TEMPORAL],
                "Invalid HAProxy configuration: "
                f"All of `{HAPROXY_NON_TLS}`, `{HAPROXY_TEMPORAL}`, and `{HAPROXY_INTERNAL_HTTP_API}` "
                "relations must be present together if any are provided.",
            ),
            (
                False,
                [HAPROXY_INTERNAL_HTTP_API],
                "Invalid HAProxy configuration: "
                f"All of `{HAPROXY_NON_TLS}`, `{HAPROXY_TEMPORAL}`, and `{HAPROXY_INTERNAL_HTTP_API}` "
                "relations must be present together if any are provided.",
            ),
        ]

        for maas_tls, relations, expected_msg in cases:
            with self.subTest(maas_tls=maas_tls, relations=relations):
                harness = self._make_harness()
                harness.add_relation(MAAS_ROLLING_OPS_RELATION, "maas-region")
                harness.set_leader(True)
                if maas_tls:
                    harness.update_config(
                        {
                            "ssl_cert_content": "placeholder-cert",
                            "ssl_key_content": "placeholder-key",
                        }
                    )
                harness.begin()
                db_rel = harness.add_relation(MAAS_DB_NAME, "postgresql")
                harness.update_relation_data(
                    db_rel,
                    "postgresql",
                    {
                        "endpoints": "30.0.0.1:5432",
                        "username": "test_maas_db",
                        "password": "my_secret",
                    },
                )
                harness.charm.on.rollingops_lock_granted.emit()
                for rel_name in relations:
                    rel_id = harness.add_relation(rel_name, "haproxy")
                    harness.add_relation_unit(rel_id, "haproxy/0")
                harness.evaluate_status()
                self.assertEqual(harness.model.unit.status, ops.BlockedStatus(expected_msg))

    @patch("charm.MaasHelper", autospec=True)
    def test_haproxy_relation__valid_topology_reaches_active(self, mock_helper):
        mock_helper.get_installed_version.return_value = "mock-ver"
        mock_helper.get_installed_channel.return_value = MAAS_SNAP_CHANNEL
        mock_helper.is_maas_initialized.return_value = True
        self.harness.set_leader(True)
        self.harness.begin()
        db_rel = self.harness.add_relation(MAAS_DB_NAME, "postgresql")
        self.harness.update_relation_data(
            db_rel,
            "postgresql",
            {
                "endpoints": "30.0.0.1:5432",
                "username": "test_maas_db",
                "password": "my_secret",
            },
        )
        self.harness.charm.on.rollingops_lock_granted.emit()
        # No HAProxy relations and no TLS config, valid topology
        self.harness.evaluate_status()
        self.assertEqual(self.harness.model.unit.status, ops.ActiveStatus())


class TestCharmActions(unittest.TestCase):
    def setUp(self):
        self.harness = ops.testing.Harness(MaasRegionCharm)
        self.harness.add_network("10.0.0.10")
        self.addCleanup(self.harness.cleanup)
        self.harness.add_relation(MAAS_ROLLING_OPS_RELATION, "maas-region")

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

    @patch("charm.MaasHelper", autospec=True)
    def test_get_maas_secret_action(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        mock_helper.get_maas_secret.return_value = "0123456789ab0123456789"
        output = self.harness.run_action("get-maas-secret")
        self.assertEqual(output.results["maas-secret"], "0123456789ab0123456789")

    @patch("charm.MaasHelper", autospec=True)
    def test_stop_maas_action(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()

        output = self.harness.run_action("stop-maas")

        mock_helper.set_running.assert_called_once_with(False)
        self.assertEqual(output.results["status"], "stopped")

    @patch("charm.MaasHelper", autospec=True)
    def test_stop_maas_action_fail(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        mock_helper.set_running.side_effect = SnapError("snap stop failed")

        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("stop-maas")

        mock_helper.set_running.assert_called_once_with(False)
        self.assertEqual(e.exception.message, "Failed to stop MAAS: snap stop failed")

    @patch("charm.MaasHelper", autospec=True)
    def test_start_maas_action(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()

        output = self.harness.run_action("start-maas")

        mock_helper.set_running.assert_called_once_with(True)
        self.assertEqual(output.results["status"], "started")

    @patch("charm.MaasHelper", autospec=True)
    def test_start_maas_action_fail(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        mock_helper.set_running.side_effect = SnapError("snap start failed")

        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("start-maas")

        mock_helper.set_running.assert_called_once_with(True)
        self.assertEqual(e.exception.message, "Failed to start MAAS: snap start failed")

    def test_get_maas_secret_action_fail(self):
        self.harness.set_leader(True)
        self.harness.begin()
        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("get-maas-secret")
        err = e.exception
        self.assertEqual(err.message, "MAAS is not initialized yet")

    @patch("charm.MaasHelper", autospec=True)
    def test_get_maas_status_action(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        mock_status = {
            "agent": {
                "startup": "disabled",
                "current": "active",
                "since": "today at 07:48 UTC",
            },
            "regiond": {
                "startup": "enabled",
                "current": "active",
                "since": "today at 07:48 UTC",
            },
        }
        mock_helper.get_maas_status.return_value = mock_status
        output = self.harness.run_action("get-maas-status")
        self.assertEqual(output.results["services"], mock_status)

    @patch("charm.MaasHelper", autospec=True)
    def test_get_maas_status_action_fail(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        mock_helper.get_maas_status.return_value = {}
        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("get-maas-status")
        err = e.exception
        self.assertEqual(err.message, "MAAS is not initialized yet or failed to retrieve status")

    def _setup_pre_upgrade_check(
        self, mock_helper, inst_version, inst_revision, snap_info_map, host_base, installed_channel
    ):
        mock_helper.get_installed_version.return_value = inst_version
        mock_helper.get_installed_revision.return_value = inst_revision
        mock_helper.get_installed_channel.return_value = installed_channel
        mock_helper.get_host_base.return_value = host_base
        mock_helper.get_latest_channel_info.side_effect = lambda ch: snap_info_map.get(ch)

    CROSS_CHANNEL_MAP: ClassVar[dict[str, dict]] = {
        "3.8/stable": {
            "version": "3.8.0",
            "revision": "50000",
            "epoch": {"read": [2, 3], "write": [3]},
        },
        "3.9/stable": {
            "version": "3.9.0",
            "revision": "60000",
            "epoch": {"read": [3, 4], "write": [4]},
        },
    }

    @patch("charm.MaasHelper", autospec=True)
    def test_pre_upgrade_check_maas_not_installed(self, mock_helper):
        for missing in ("version", "revision", "channel"):
            with self.subTest(missing=missing):
                self.harness = ops.testing.Harness(MaasRegionCharm)
                self.harness.add_network("10.0.0.10")
                self.addCleanup(self.harness.cleanup)
                self.harness.set_leader(True)
                self.harness.begin()
                self._setup_pre_upgrade_check(
                    mock_helper,
                    inst_version=None if missing == "version" else "3.8.0",
                    inst_revision=None if missing == "revision" else "50000",
                    snap_info_map=self.CROSS_CHANNEL_MAP,
                    host_base="26.04",
                    installed_channel=None if missing == "channel" else "3.8/stable",
                )

                with self.assertRaises(ops.testing.ActionFailed) as e:
                    self.harness.run_action("pre-upgrade-check", {"track": "3.9"})

                self.assertEqual(
                    e.exception.message,
                    "Could not obtain installed MAAS version, revision, or channel."
                    " Is MAAS installed?",
                )
                mock_helper.get_latest_channel_info.assert_not_called()

    @patch("charm.MaasHelper", autospec=True)
    def test_pre_upgrade_check_channel_not_in_store(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        self._setup_pre_upgrade_check(
            mock_helper,
            inst_version="3.8.0",
            inst_revision="50000",
            snap_info_map={},  # No entry for 3.9/stable
            host_base="26.04",
            installed_channel="3.8/stable",
        )

        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("pre-upgrade-check", {"track": "3.9"})

        mock_helper.get_latest_channel_info.assert_called_once_with("3.9/stable")
        self.assertEqual(
            e.exception.message,
            "No MAAS version found in the snap store for channel 3.9/stable",
        )
        self.assertEqual(
            e.exception.output.results["installed-snap"],
            "3.8.0 (revision 50000) on channel 3.8/stable",
        )

    @patch("charm.MaasHelper", autospec=True)
    def test_pre_upgrade_check_store_missing_version_and_revision(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        self._setup_pre_upgrade_check(
            mock_helper,
            inst_version="3.8.0",
            inst_revision="50000",
            snap_info_map={"3.9/stable": {"epoch": {"read": [3], "write": [3]}}},
            host_base="26.04",
            installed_channel="3.8/stable",
        )

        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("pre-upgrade-check", {"track": "3.9"})

        self.assertEqual(
            e.exception.message,
            "The snap store did not report a version and revision for channel 3.9/stable,"
            " cannot determine if an upgrade is possible.",
        )
        self.assertEqual(
            e.exception.output.results["installed-snap"],
            "3.8.0 (revision 50000) on channel 3.8/stable",
        )

    @patch("charm.MaasHelper", autospec=True)
    def test_pre_upgrade_check_store_query_fails(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        self._setup_pre_upgrade_check(
            mock_helper,
            inst_version="3.8.0",
            inst_revision="50000",
            snap_info_map=self.CROSS_CHANNEL_MAP,
            host_base="26.04",
            installed_channel="3.8/stable",
        )
        mock_helper.get_latest_channel_info.side_effect = SnapError("store unreachable")

        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("pre-upgrade-check", {"track": "3.9"})

        self.assertEqual(
            e.exception.message,
            "Failed to query the snap store for the latest MAAS version on channel 3.9/stable,"
            " cannot determine if an upgrade is possible.",
        )
        self.assertEqual(
            e.exception.output.results["installed-snap"],
            "3.8.0 (revision 50000) on channel 3.8/stable",
        )

    INSTALLED_TRACK: ClassVar[str] = MAAS_SNAP_CHANNEL.split("/")[0]
    INSTALLED_VERSION: ClassVar[str] = f"{INSTALLED_TRACK}.0"
    POINT_VERSION: ClassVar[str] = f"{INSTALLED_TRACK}.1"

    POINT_CHANNEL_MAP: ClassVar[dict[str, dict]] = {
        MAAS_SNAP_CHANNEL: {
            "version": POINT_VERSION,
            "revision": "50100",
            "epoch": {"read": [2, 3], "write": [3]},
        },
    }

    @patch("charm.MaasHelper", autospec=True)
    def test_pre_upgrade_check_point_upgrade_skips_checks(self, mock_helper):
        """Staying on the installed channel cannot change the base, so it is not checked."""
        self.harness.set_leader(True)
        self.harness.begin()
        self._setup_pre_upgrade_check(
            mock_helper,
            inst_version=self.INSTALLED_VERSION,
            inst_revision="50000",
            snap_info_map=self.POINT_CHANNEL_MAP,
            host_base="26.04",
            installed_channel=MAAS_SNAP_CHANNEL,
        )

        output = self.harness.run_action("pre-upgrade-check")

        mock_helper.get_latest_channel_info.assert_called_once_with(MAAS_SNAP_CHANNEL)
        self.assertEqual(
            output.results["upgrade-target-snap"],
            f"{self.POINT_VERSION} (revision 50100) on channel {MAAS_SNAP_CHANNEL}",
        )
        self.assertEqual(
            output.results["info"],
            f"Point upgrade is possible from {self.INSTALLED_VERSION} to {self.POINT_VERSION}.",
        )
        # Not expecting base checks for point upgrades
        self.assertNotIn("host-base", output.results)
        self.assertNotIn("upgrade-target-charm-bases", output.results)
        mock_helper.get_host_base.assert_not_called()

    @patch("charm.MaasHelper", autospec=True)
    def test_pre_upgrade_check_already_latest_revision(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        self._setup_pre_upgrade_check(
            mock_helper,
            inst_version=self.POINT_VERSION,
            inst_revision="50100",
            snap_info_map=self.POINT_CHANNEL_MAP,
            host_base="26.04",
            installed_channel=MAAS_SNAP_CHANNEL,
        )

        output = self.harness.run_action("pre-upgrade-check")

        self.assertEqual(
            output.results["info"],
            "Current installed revision (50100) is the latest available on channel"
            f" {MAAS_SNAP_CHANNEL}. No upgrade is needed.",
        )
        self.assertNotIn("upgrade-target-snap", output.results)

    @patch.dict("charm.MAAS_TRACK_BASES", {"3.8": ["26.04"], "3.9": ["26.04"]}, clear=True)
    @patch("charm.MaasHelper", autospec=True)
    def test_pre_upgrade_check_compatible(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        self._setup_pre_upgrade_check(
            mock_helper,
            inst_version="3.8.0",
            inst_revision="50000",
            snap_info_map=self.CROSS_CHANNEL_MAP,
            host_base="26.04",
            installed_channel="3.8/stable",
        )

        output = self.harness.run_action("pre-upgrade-check", {"track": "3.9"})

        mock_helper.get_latest_channel_info.assert_called_once_with("3.9/stable")
        self.assertEqual(
            output.results["upgrade-target-snap"], "3.9.0 (revision 60000) on channel 3.9/stable"
        )
        self.assertEqual(output.results["host-base"], "26.04")
        self.assertEqual(output.results["upgrade-target-charm-bases"], "26.04")
        self.assertIn("Performing an upgrade inplace is possible", output.results["info"])

    @patch.dict("charm.MAAS_TRACK_BASES", {"3.8": ["26.04"], "3.9": ["28.04"]}, clear=True)
    @patch("charm.MaasHelper", autospec=True)
    def test_pre_upgrade_check_base_incompatible(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        self._setup_pre_upgrade_check(
            mock_helper,
            inst_version="3.8.0",
            inst_revision="50000",
            snap_info_map=self.CROSS_CHANNEL_MAP,
            host_base="26.04",
            installed_channel="3.8/stable",
        )

        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("pre-upgrade-check", {"track": "3.9"})

        err = e.exception
        self.assertIn("requires an Ubuntu base of 28.04", err.message)
        self.assertIn("this unit runs 26.04", err.message)
        self.assertIn("redeploying units", err.message)
        self.assertEqual(err.output.results["host-base"], "26.04")
        self.assertEqual(err.output.results["upgrade-target-charm-bases"], "28.04")

    @patch.dict("charm.MAAS_TRACK_BASES", {"3.8": ["26.04"]}, clear=True)
    @patch("charm.MaasHelper", autospec=True)
    def test_pre_upgrade_check_base_unknown_track(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        self._setup_pre_upgrade_check(
            mock_helper,
            inst_version="3.8.0",
            inst_revision="50000",
            snap_info_map=self.CROSS_CHANNEL_MAP,
            host_base="26.04",
            installed_channel="3.8/stable",
        )

        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("pre-upgrade-check", {"track": "3.9"})

        err = e.exception
        self.assertIn("Track 3.9 is not known to this charm", err.message)
        self.assertIn("one or both tracks are not known to this charm", err.message)
        self.assertNotIn("host-base", err.output.results)
        self.assertNotIn("upgrade-target-charm-bases", err.output.results)

    @patch.dict(
        "charm.MAAS_TRACK_BASES",
        {"3.7": ["24.04"], "3.8": ["26.04"], "3.9": ["26.04"]},
        clear=True,
    )
    @patch("charm.MaasHelper", autospec=True)
    def test_pre_upgrade_check_non_sequential_track(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        self._setup_pre_upgrade_check(
            mock_helper,
            inst_version="3.7.3",
            inst_revision="42000",
            snap_info_map=self.CROSS_CHANNEL_MAP,
            host_base="26.04",
            installed_channel="3.7/stable",
        )

        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("pre-upgrade-check", {"track": "3.9"})

        err = e.exception
        self.assertIn("non-sequential upgrade that skips one or more minor versions", err.message)
        self.assertNotIn("requires an Ubuntu base", err.message)

    @patch.dict("charm.MAAS_TRACK_BASES", {"3.8": ["26.04"], "3.9": ["26.04"]}, clear=True)
    @patch("charm.MaasHelper", autospec=True)
    def test_pre_upgrade_check_downgrade(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        self._setup_pre_upgrade_check(
            mock_helper,
            inst_version="3.9.5",
            inst_revision="61000",
            snap_info_map=self.CROSS_CHANNEL_MAP,
            host_base="26.04",
            installed_channel="3.9/stable",
        )

        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("pre-upgrade-check", {"track": "3.8"})

        self.assertIn(
            "The latest version (3.8.0) on channel 3.8/stable is a downgrade compared to the"
            " installed version (3.9.5).",
            e.exception.message,
        )

    @patch("charm.MaasHelper", autospec=True)
    def test_upgrade_action(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        mock_helper.get_installed_version.return_value = "3.8.0"
        mock_helper.get_installed_revision.return_value = "12345"

        output = self.harness.run_action("upgrade")

        mock_helper.upgrade.assert_not_called()
        self.harness.charm.on.rollingops_lock_granted.emit()

        mock_helper.upgrade.assert_called_once_with(MAAS_SNAP_CHANNEL)
        self.assertEqual(self.harness.get_workload_version(), "3.8.0")
        self.assertEqual(
            output.results["info"], f"Upgrade started for snap on channel {MAAS_SNAP_CHANNEL}"
        )

    @patch("charm.MaasHelper", autospec=True)
    def test_upgrade_action_fail(self, mock_helper):
        self.harness.set_leader(True)
        self.harness.begin()
        mock_helper.upgrade.side_effect = SnapError("snap refresh failed")

        self.harness.run_action("upgrade")
        self.harness.charm.on.rollingops_lock_granted.emit()

        # The first attempt plus max_retry=3 exhausts the operation.
        for _ in range(3):
            self.harness.charm.on.rollingops_lock_granted.emit()

        self.assertEqual(mock_helper.upgrade.call_count, 4)
        self.assertFalse(self.harness.charm.rolling_ops_manager.is_waiting_callback("upgrade"))

    @patch("charm.MaasRegionCharm._upgrade_precondition_error")
    @patch("charm.MaasHelper", autospec=True)
    def test_upgrade_action_force_skips_precondition(self, mock_helper, precondition_error):
        precondition_error.return_value = "MAAS is still running"
        self.harness.set_leader(True)
        self.harness.begin()

        output = self.harness.run_action("upgrade", {"force": True})

        precondition_error.assert_not_called()
        self.assertEqual(
            output.results["info"], f"Upgrade started for snap on channel {MAAS_SNAP_CHANNEL}"
        )

        self.harness.charm.on.rollingops_lock_granted.emit()
        mock_helper.upgrade.assert_called_once_with(MAAS_SNAP_CHANNEL)

    @patch("charm.MaasRegionCharm._upgrade_precondition_error")
    @patch("charm.MaasHelper", autospec=True)
    def test_upgrade_action_without_force_checks_precondition(
        self, mock_helper, precondition_error
    ):
        precondition_error.return_value = "MAAS is still running"
        self.harness.set_leader(True)
        self.harness.begin()

        with self.assertRaises(ops.testing.ActionFailed) as e:
            self.harness.run_action("upgrade")

        precondition_error.assert_called_once()
        self.assertEqual(e.exception.message, "MAAS is still running")

        self.harness.charm.on.rollingops_lock_granted.emit()
        mock_helper.upgrade.assert_not_called()

    @patch("charm.MaasHelper", autospec=True)
    def test_upgrade_precondition_error(self, mock_helper):
        self.harness.set_planned_units(2)
        self.harness.begin()
        mock_helper.is_running.return_value = True
        mock_helper.get_installed_channel.return_value = "3.7/stable"

        error = self.harness.charm._upgrade_precondition_error()

        assert error is not None
        self.assertIn("The MAAS snap is running", error)

    @patch("charm.MaasHelper", autospec=True)
    def test_upgrade_precondition_error_allowed(self, mock_helper):
        cases = {
            "single unit": (1, True, "3.7/stable"),
            "maas stopped": (2, False, "3.7/stable"),
            "same channel": (2, True, MAAS_SNAP_CHANNEL),
        }
        self.harness.begin()

        for name, (planned_units, is_running, channel) in cases.items():
            with self.subTest(name):
                self.harness.set_planned_units(planned_units)
                mock_helper.is_running.return_value = is_running
                mock_helper.get_installed_channel.return_value = channel

                self.assertIsNone(self.harness.charm._upgrade_precondition_error())

    @patch(
        "charm.MaasRegionCharm.bind_address",
        new_callable=PropertyMock(return_value="10.0.0.10"),
    )
    @patch("charm.MaasHelper.get_regions")
    @patch("charm.MaasRegionCharm._create_or_get_internal_admin")
    def test_get_region_system_ids(self, admin, get_regions, _mock_bind_address):
        admin.return_value = {"username": "admin"}
        get_regions.return_value = {"region-1", "region-2"}
        self.harness.begin()
        regions = self.harness.charm.get_region_system_ids()
        self.assertEqual(regions, {"region-1", "region-2"})

    @patch("charm.MaasRegionCharm._create_or_get_internal_admin")
    def test_get_region_system_ids_get_admin_fail(self, admin):
        admin.side_effect = subprocess.CalledProcessError(1, "maas")
        self.harness.begin()
        with self.assertRaises(subprocess.CalledProcessError):
            self.harness.charm.get_region_system_ids()

    @patch(
        "charm.MaasRegionCharm.bind_address",
        new_callable=PropertyMock(return_value="10.0.0.10"),
    )
    @patch("charm.MaasHelper.get_regions")
    @patch("charm.MaasRegionCharm._create_or_get_internal_admin")
    def test_get_region_system_ids_get_regions_fail(self, admin, get_regions, _mock_bind_address):
        admin.return_value = {"username": "admin"}
        get_regions.side_effect = subprocess.CalledProcessError(1, "maas")
        self.harness.begin()
        with self.assertRaises(subprocess.CalledProcessError):
            self.harness.charm.get_region_system_ids()

    @patch(
        "charm.MaasRegionCharm.bind_address",
        new_callable=PropertyMock(return_value="10.0.0.10"),
    )
    @patch("charm.MaasHelper.get_rack_versions")
    @patch("charm.MaasRegionCharm._create_or_get_internal_admin")
    def test_get_rack_versions(self, admin, get_rack_versions, _mock_bind_address):
        admin.return_value = {"username": "admin"}
        get_rack_versions.return_value = {"rack-1": "3.7.3", "rack-2": "3.8.0~alpha1"}
        self.harness.begin()
        versions = self.harness.charm.get_rack_versions()
        self.assertEqual(versions, {"rack-1": "3.7.3", "rack-2": "3.8.0~alpha1"})

    @patch(
        "charm.MaasRegionCharm.bind_address",
        new_callable=PropertyMock(return_value="10.0.0.10"),
    )
    @patch("charm.MaasHelper.get_rack_versions")
    @patch("charm.MaasRegionCharm._create_or_get_internal_admin")
    def test_get_rack_versions_standalone_only(self, admin, get_rack_versions, _mock_bind_address):
        admin.return_value = {"username": "admin"}
        get_rack_versions.return_value = {"rack-1": "3.7.3"}
        self.harness.begin()
        versions = self.harness.charm.get_rack_versions(standalone_only=True)
        self.assertEqual(versions, {"rack-1": "3.7.3"})
        self.assertTrue(get_rack_versions.call_args.kwargs["standalone_only"])

    @patch("charm.MaasRegionCharm._create_or_get_internal_admin")
    def test_get_rack_versions_get_admin_fail(self, admin):
        admin.side_effect = subprocess.CalledProcessError(1, "maas")
        self.harness.begin()
        with self.assertRaises(subprocess.CalledProcessError):
            self.harness.charm.get_rack_versions()

    @patch(
        "charm.MaasRegionCharm.bind_address",
        new_callable=PropertyMock(return_value="10.0.0.10"),
    )
    @patch("charm.MaasHelper.get_rack_versions")
    @patch("charm.MaasRegionCharm._create_or_get_internal_admin")
    def test_get_rack_versions_read_fail(self, admin, get_rack_versions, _mock_bind_address):
        admin.return_value = {"username": "admin"}
        get_rack_versions.side_effect = subprocess.CalledProcessError(1, "maas")
        self.harness.begin()
        with self.assertRaises(subprocess.CalledProcessError):
            self.harness.charm.get_rack_versions()

    @patch("charm.MaasRegionCharm.get_rack_versions")
    def test_check_rack_versions_point_release_ordering(self, get_rack_versions):
        self.harness.begin()
        get_rack_versions.return_value = {"rack-1": "3.7.10"}

        results: dict = {}
        self.harness.charm._check_rack_versions("3.7.3", results)

        self.assertIn("newer than the target version 3.7.3", results["rack-info"])

    @patch("charm.MaasRegionCharm.get_rack_versions")
    def test_check_rack_versions_behind(self, get_rack_versions):
        self.harness.begin()
        get_rack_versions.return_value = {"rack-1": "3.7.2", "rack-2": "3.7.1"}

        results: dict = {}
        self.harness.charm._check_rack_versions("3.7.3", results)

        self.assertIn("Some racks are behind the target MAAS version", results["rack-info"])
        self.assertIn("rack-1 (3.7.2), rack-2 (3.7.1) are older", results["rack-info"])
        self.assertIn("Consider upgrading your standalone racks first", results["rack-info"])

    @patch("charm.MaasRegionCharm.get_rack_versions")
    def test_check_rack_versions_at_target(self, get_rack_versions):
        self.harness.begin()
        get_rack_versions.return_value = {"rack-1": "3.7.3"}

        results: dict = {}
        self.harness.charm._check_rack_versions("3.7.3", results)

        self.assertEqual(
            results["rack-info"],
            "All standalone rack controllers are running target version 3.7.3.",
        )


class TestTrackBases(unittest.TestCase):
    """Guards keeping MAAS_TRACK_BASES in step with the charm it ships in."""

    def _charmcraft(self):
        with open(Path(__file__).parents[2] / "charmcraft.yaml") as f:
            return yaml.safe_load(f)

    def test_current_track_is_mapped(self):
        # Bumping MAAS_SNAP_CHANNEL to a new track must come with a base mapping.
        track = MAAS_SNAP_CHANNEL.split("/")[0]
        self.assertIn(
            track,
            MAAS_TRACK_BASES,
            f"MAAS_SNAP_CHANNEL is on track {track}, add it to MAAS_TRACK_BASES.",
        )

    def test_current_track_matches_declared_platforms(self):
        # The mapping for this track must match the bases the charm is actually built for.
        track = MAAS_SNAP_CHANNEL.split("/")[0]
        declared = {p.split(":")[0].split("@")[1] for p in self._charmcraft()["platforms"]}
        self.assertEqual(
            set(MAAS_TRACK_BASES[track]),
            declared,
            f"MAAS_TRACK_BASES[{track}] disagrees with `platforms` in charmcraft.yaml.",
        )


class TestNextTrack(unittest.TestCase):
    """Test _next_track, which drives the sequential upgrade path."""

    TRACKS: ClassVar[dict[str, list[str]]] = {
        "3.7": ["24.04"],
        "3.8": ["26.04"],
        "4.0": ["26.04"],
    }

    @patch.dict("charm.MAAS_TRACK_BASES", TRACKS, clear=True)
    def test_first_track_returns_successor(self):
        self.assertEqual(_next_track("3.7"), "3.8")

    @patch.dict("charm.MAAS_TRACK_BASES", TRACKS, clear=True)
    def test_middle_track_returns_successor(self):
        self.assertEqual(_next_track("3.8"), "4.0")

    @patch.dict("charm.MAAS_TRACK_BASES", TRACKS, clear=True)
    def test_latest_track_returns_none(self):
        self.assertIsNone(_next_track("4.0"))

    @patch.dict("charm.MAAS_TRACK_BASES", TRACKS, clear=True)
    def test_unmapped_track_returns_none(self):
        self.assertIsNone(_next_track("2.9"))
        self.assertIsNone(_next_track("7.0"))

    @patch.dict("charm.MAAS_TRACK_BASES", TRACKS, clear=True)
    def test_non_track_input_returns_none(self):
        self.assertIsNone(_next_track(""))
        self.assertIsNone(_next_track("3.8/stable"))
        self.assertIsNone(_next_track("3.80"))

    @patch.dict("charm.MAAS_TRACK_BASES", {}, clear=True)
    def test_empty_mapping_returns_none(self):
        # Shouldn't ever be the case in a real charm but it's here anyway
        self.assertIsNone(_next_track("3.8"))

    @patch.dict(
        "charm.MAAS_TRACK_BASES",
        {"3.8": ["26.04"], "4.0": ["26.04"], "3.7": ["24.04"]},
        clear=True,
    )
    def test_any_order_track(self):
        self.assertEqual(_next_track("3.8"), "4.0")
        self.assertEqual(_next_track("3.7"), "3.8")
        self.assertIsNone(_next_track("4.0"))


class TestMAASURLs(unittest.TestCase):
    """Test maas_cli_url and maas_api_url properties."""

    def setUp(self):
        self.harness = ops.testing.Harness(MaasRegionCharm)
        self.harness.add_network("10.0.0.10")
        self.addCleanup(self.harness.cleanup)

    def test_maas_cli_url_with_config(self):
        """Test maas_cli_url returns configured URL when maas_url is set."""
        self.harness.update_config({"maas_url": "https://custom.maas.example.com/MAAS"})
        self.harness.begin()
        self.assertEqual(self.harness.charm.maas_cli_url, "https://custom.maas.example.com/MAAS")

    def test_maas_cli_url_with_haproxy_non_tls(self):
        """Test maas_cli_url uses HAProxy non-TLS endpoint when TLS is disabled."""
        self.harness.begin()
        rel_id = self.harness.add_relation(HAPROXY_NON_TLS, "haproxy")
        self.harness.update_relation_data(rel_id, "haproxy", {"endpoints": '["10.226.71.86:80"]'})
        self.assertEqual(self.harness.charm.maas_cli_url, "http://10.226.71.86:80/MAAS")

    def test_maas_cli_url_with_haproxy_tls(self):
        """Test maas_cli_url uses HAProxy TLS endpoint when TLS is enabled."""
        self.harness.update_config(
            {
                "ssl_cert_content": "cert-content",
                "ssl_key_content": "key-content",
            }
        )
        self.harness.begin()
        rel_id = self.harness.add_relation(HAPROXY_TLS, "haproxy")
        self.harness.update_relation_data(rel_id, "haproxy", {"endpoints": '["10.226.71.86:443"]'})
        self.assertEqual(self.harness.charm.maas_cli_url, "https://10.226.71.86:443/MAAS")

    def test_maas_cli_url_fallback_to_bind_address_non_tls(self):
        """Test maas_cli_url falls back to bind address when no HAProxy (non-TLS)."""
        self.harness.begin()
        self.assertEqual(
            self.harness.charm.maas_cli_url, f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS"
        )

    def test_maas_cli_url_fallback_to_bind_address_tls(self):
        """Test maas_cli_url falls back to bind address when no HAProxy (TLS)."""
        self.harness.update_config(
            {
                "ssl_cert_content": "cert-content",
                "ssl_key_content": "key-content",
            }
        )
        self.harness.begin()
        self.assertEqual(
            self.harness.charm.maas_cli_url, f"https://10.0.0.10:{MAAS_HTTPS_PORT}/MAAS"
        )

    def test_maas_cli_url_handles_malformed_endpoints(self):
        """Test maas_cli_url handles malformed HAProxy endpoints and falls back to bind address."""
        cases = [
            ("invalid_json", "invalid-json"),
            ("empty_list", "[]"),
            ("non_list_json", '{"key": "value"}'),
        ]
        for name, endpoints_value in cases:
            with self.subTest(case=name):
                self.harness.begin()
                rel_id = self.harness.add_relation(HAPROXY_NON_TLS, "haproxy")
                self.harness.update_relation_data(
                    rel_id, "haproxy", {"endpoints": endpoints_value}
                )
                self.assertEqual(
                    self.harness.charm.maas_cli_url, f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS"
                )
                self.harness.cleanup()
                self.harness = ops.testing.Harness(MaasRegionCharm)
                self.harness.add_network("10.0.0.10")

    def test_maas_api_url_with_config(self):
        """Test maas_api_url converts https to http from configured URL."""
        self.harness.update_config({"maas_url": "https://custom.maas.example.com/MAAS"})
        self.harness.begin()
        self.assertEqual(self.harness.charm.maas_api_url, "http://custom.maas.example.com/MAAS")

    def test_maas_api_url_with_http_config(self):
        """Test maas_api_url keeps http scheme from configured URL."""
        self.harness.update_config({"maas_url": "http://custom.maas.example.com/MAAS"})
        self.harness.begin()
        self.assertEqual(self.harness.charm.maas_api_url, "http://custom.maas.example.com/MAAS")

    def test_maas_api_url_with_haproxy(self):
        """Test maas_api_url uses HAProxy non-TLS endpoint."""
        self.harness.begin()
        rel_id = self.harness.add_relation(HAPROXY_NON_TLS, "haproxy")
        self.harness.update_relation_data(rel_id, "haproxy", {"endpoints": '["10.226.71.86:80"]'})
        self.assertEqual(self.harness.charm.maas_api_url, "http://10.226.71.86:80/MAAS")

    def test_maas_api_url_fallback_to_bind_address(self):
        """Test maas_api_url falls back to bind address when no HAProxy."""
        self.harness.begin()
        self.assertEqual(
            self.harness.charm.maas_api_url, f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS"
        )

    def test_maas_api_url_handles_malformed_endpoints(self):
        """Test maas_api_url handles malformed HAProxy endpoints and falls back to bind address."""
        cases = [
            ("invalid_json", "invalid-json"),
            ("empty_list", "[]"),
            ("non_list_json", '{"key": "value"}'),
        ]
        for name, endpoints_value in cases:
            with self.subTest(case=name):
                self.harness.begin()
                rel_id = self.harness.add_relation(HAPROXY_NON_TLS, "haproxy")
                self.harness.update_relation_data(
                    rel_id, "haproxy", {"endpoints": endpoints_value}
                )
                self.assertEqual(
                    self.harness.charm.maas_api_url, f"http://10.0.0.10:{MAAS_HTTP_PORT}/MAAS"
                )
                self.harness.cleanup()
                self.harness = ops.testing.Harness(MaasRegionCharm)
                self.harness.add_network("10.0.0.10")


class TestScrapeConfigs(unittest.TestCase):
    def setUp(self):
        self.harness = ops.testing.Harness(MaasRegionCharm)
        self.harness.add_network("10.0.0.10")
        self.addCleanup(self.harness.cleanup)

    @staticmethod
    def _by_path(scrape_configs):
        return {cfg["metrics_path"]: cfg for cfg in scrape_configs}

    def test_non_tls_mode(self):
        """All MAAS endpoints are scraped over http when TLS is disabled."""
        self.harness.update_config({"enable_rack_mode": False})
        self.harness.begin()

        configs = self.harness.charm._generate_scrape_configs()
        by_path = self._by_path(configs)

        self.assertEqual(set(by_path), {"/metrics", "/MAAS/metrics", "/metrics/temporal"})
        # No https scheme nor tls_config in non-TLS mode.
        for cfg in configs:
            self.assertNotIn("scheme", cfg)
            self.assertNotIn("tls_config", cfg)
        self.assertEqual(
            by_path["/metrics"]["static_configs"][0]["targets"],
            [f"localhost:{MAAS_REGION_METRICS_PORT}"],
        )
        self.assertEqual(
            by_path["/MAAS/metrics"]["static_configs"][0]["targets"],
            [f"localhost:{MAAS_CLUSTER_METRICS_PORT}"],
        )
        self.assertEqual(
            by_path["/metrics/temporal"]["static_configs"][0]["targets"],
            [f"localhost:{MAAS_HTTP_PORT}"],
        )

    def test_tls_mode(self):
        """/MAAS/metrics and /metrics/temporal move to https:5443 when TLS is enabled."""
        self.harness.update_config(
            {
                "enable_rack_mode": False,
                "ssl_cert_content": "placeholder-cert",
                "ssl_key_content": "placeholder-key",
            }
        )
        self.harness.begin()

        configs = self.harness.charm._generate_scrape_configs()
        by_path = self._by_path(configs)

        self.assertEqual(set(by_path), {"/metrics", "/MAAS/metrics", "/metrics/temporal"})
        # The region metrics endpoint stays plain http.
        self.assertNotIn("scheme", by_path["/metrics"])
        self.assertEqual(
            by_path["/metrics"]["static_configs"][0]["targets"],
            [f"localhost:{MAAS_REGION_METRICS_PORT}"],
        )
        for path in ("/MAAS/metrics", "/metrics/temporal"):
            cfg = by_path[path]
            self.assertEqual(cfg["scheme"], "https")
            self.assertEqual(cfg["tls_config"], {"insecure_skip_verify": True})
            self.assertEqual(cfg["static_configs"][0]["targets"], [f"localhost:{MAAS_HTTPS_PORT}"])

    def test_rack_mode_adds_agent_endpoint(self):
        """Rack mode adds the http agent metrics endpoint."""
        self.harness.update_config({"enable_rack_mode": True})
        self.harness.begin()

        configs = self.harness.charm._generate_scrape_configs()
        by_path = self._by_path(configs)

        self.assertIn(MAAS_AGENT_METRICS_ENDPOINT, by_path)
        agent_cfg = by_path[MAAS_AGENT_METRICS_ENDPOINT]
        self.assertNotIn("scheme", agent_cfg)
        self.assertEqual(
            agent_cfg["static_configs"][0]["targets"],
            [f"localhost:{MAAS_AGENT_METRICS_PORT}"],
        )

    def test_rack_mode_disabled_omits_agent_endpoint(self):
        """Without rack mode the agent metrics endpoint is not scraped."""
        self.harness.update_config({"enable_rack_mode": False})
        self.harness.begin()

        by_path = self._by_path(self.harness.charm._generate_scrape_configs())

        self.assertNotIn(MAAS_AGENT_METRICS_ENDPOINT, by_path)
