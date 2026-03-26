# Copyright 2024 Canonical
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import subprocess
import unittest
from unittest.mock import MagicMock, PropertyMock, mock_open, patch

from charms.operator_libs_linux.v2.snap import SnapState

from helper import MAAS_SERVICE, MaasHelper


class TestHelperSnapCache(unittest.TestCase):
    def _setup_snap(self, mock_snap, present=False, version="1234", channel="latest/stable"):
        maas = MagicMock()
        type(maas).present = PropertyMock(return_value=present)
        type(maas).version = PropertyMock(return_value=version)
        type(maas).channel = PropertyMock(return_value=channel)
        instance = mock_snap.return_value
        instance.__getitem__.return_value = maas
        return maas

    @patch("helper.SnapCache", autospec=True)
    def test_install(self, mock_snap):
        mock_maas = self._setup_snap(mock_snap)
        MaasHelper.install("test/channel")
        mock_maas.ensure.assert_called_once_with(SnapState.Latest, channel="test/channel")
        mock_maas.hold.assert_called_once()

    @patch("helper.SnapCache", autospec=True)
    def test_install_already_present(self, mock_snap):
        mock_maas = self._setup_snap(mock_snap, present=True)
        MaasHelper.install("test/channel")
        mock_maas.ensure.assert_not_called()

    @patch("helper.SnapCache", autospec=True)
    def test_uninstall(self, mock_snap):
        mock_maas = self._setup_snap(mock_snap, present=True)
        MaasHelper.uninstall()
        mock_maas.ensure.assert_called_once_with(SnapState.Absent)

    @patch("helper.SnapCache", autospec=True)
    def test_uninstall_not_present(self, mock_snap):
        mock_maas = self._setup_snap(mock_snap, present=False)
        MaasHelper.uninstall()
        mock_maas.ensure.assert_not_called()

    @patch("helper.SnapCache", autospec=True)
    def test_stop(self, mock_snap):
        mock_maas = self._setup_snap(mock_snap, present=True)
        MaasHelper.stop()
        mock_maas.stop.assert_called_once()

    @patch("helper.SnapCache", autospec=True)
    def test_stop_not_present(self, mock_snap):
        mock_maas = self._setup_snap(mock_snap, present=False)
        MaasHelper.stop()
        mock_maas.stop.assert_not_called()

    @patch("helper.SnapCache", autospec=True)
    def test_get_installed_version(self, mock_snap):
        self._setup_snap(mock_snap, present=True, version="12345")
        self.assertEqual(MaasHelper.get_installed_version(), "12345")

    @patch("helper.SnapCache", autospec=True)
    def test_get_installed_channel(self, mock_snap):
        self._setup_snap(mock_snap, present=True, channel="latest/edge")
        self.assertEqual(MaasHelper.get_installed_channel(), "latest/edge")

    @patch("helper.SnapCache", autospec=True)
    def test_is_running(self, mock_snap):
        maas = self._setup_snap(mock_snap, present=True)
        maas.services.return_value = {MAAS_SERVICE: {"activate": True}}
        self.assertTrue(MaasHelper.is_running())

    @patch("helper.SnapCache", autospec=True)
    def test_set_running(self, mock_snap):
        maas = self._setup_snap(mock_snap, present=True)
        maas.start.return_value = None
        MaasHelper.set_running(True)
        maas.start.assert_called_once()

    @patch("helper.SnapCache", autospec=True)
    def test_set_not_running(self, mock_snap):
        maas = self._setup_snap(mock_snap, present=True)
        maas.stop.return_value = None
        MaasHelper.set_running(False)
        maas.stop.assert_called_once()


class TestHelperFiles(unittest.TestCase):
    @patch("pathlib.Path.open", new_callable=lambda: mock_open(read_data="maas-id\n"))
    def test_get_maas_id(self, _):
        self.assertEqual(MaasHelper.get_maas_id(), "maas-id")

    @patch("pathlib.Path.open", side_effect=OSError)
    def test_get_maas_id_not_initialised(self, _):
        self.assertIsNone(MaasHelper.get_maas_id())

    @patch("pathlib.Path.open", new_callable=lambda: mock_open(read_data="maas-uuid\n"))
    def test_get_maas_uuid(self, _):
        self.assertEqual(MaasHelper.get_maas_uuid(), "maas-uuid")

    @patch("pathlib.Path.open", side_effect=OSError)
    def test_get_maas_uuid_not_initialised(self, _):
        self.assertIsNone(MaasHelper.get_maas_uuid())

    @patch("pathlib.Path.open", new_callable=lambda: mock_open(read_data="region+rack\n"))
    def test_get_maas_mode(self, _):
        self.assertEqual(MaasHelper.get_maas_mode(), "region+rack")

    @patch("pathlib.Path.open", side_effect=OSError)
    def test_get_maas_mode_not_initialised(self, _):
        self.assertIsNone(MaasHelper.get_maas_mode())

    @patch("pathlib.Path.read_text")
    def test_is_tls_enabled_no(self, mock_read_text):
        mock_read_text.return_value = "listen 80"
        self.assertEqual(MaasHelper.is_tls_enabled(), False)

    @patch("pathlib.Path.read_text")
    def test_is_tls_enabled_yes(self, mock_read_text):
        mock_read_text.return_value = "listen 5443"
        self.assertEqual(MaasHelper.is_tls_enabled(), True)

    @patch("pathlib.Path.read_text", side_effect=FileNotFoundError)
    def test_is_tls_enabled_not_initalized(self, _):
        self.assertEqual(MaasHelper.is_tls_enabled(), None)

    @patch(
        "pathlib.Path.open", new_callable=lambda: mock_open(read_data="0123456789ab0123456789\n")
    )
    def test_get_maas_secret(self, _):
        self.assertEqual(MaasHelper.get_maas_secret(), "0123456789ab0123456789")

    @patch("pathlib.Path.open", side_effect=OSError)
    def test_get_maas_secret_not_initialised(self, _):
        self.assertIsNone(MaasHelper.get_maas_secret())

    @patch("helper.subprocess.check_output")
    def test_get_maas_status(self, mock_check_output):
        mock_output = """Service          Startup   Current   Since
agent            disabled  active    today at 07:48 UTC
apiserver        enabled   active    today at 07:48 UTC
bind9            disabled  inactive  -
rackd            enabled   error     today at 07:50 UTC
regiond          enabled   backoff   today at 07:49 UTC
"""
        mock_check_output.return_value = mock_output.encode()
        result = MaasHelper.get_maas_status()
        self.assertEqual(len(result), 5)
        self.assertIn("agent", result)
        self.assertEqual(result["agent"]["startup"], "disabled")
        self.assertEqual(result["agent"]["current"], "active")
        self.assertEqual(result["agent"]["since"], "today at 07:48 UTC")
        self.assertIn("apiserver", result)
        self.assertIn("bind9", result)
        self.assertEqual(result["bind9"]["current"], "inactive")
        self.assertEqual(result["rackd"]["current"], "error")
        self.assertEqual(result["regiond"]["current"], "backoff")
        mock_check_output.assert_called_once_with(
            ["/snap/bin/maas", "status"], stderr=subprocess.DEVNULL
        )

    @patch("helper.subprocess.check_output", side_effect=subprocess.CalledProcessError(1, "cmd"))
    def test_get_maas_status_command_failed(self, mock_check_output):
        result = MaasHelper.get_maas_status()
        self.assertEqual(result, {})

    @patch("helper.subprocess.check_output")
    def test_get_maas_status_empty_output(self, mock_check_output):
        mock_check_output.return_value = b""
        result = MaasHelper.get_maas_status()
        self.assertEqual(result, {})

    @patch("helper.subprocess.check_output")
    def test_get_maas_status_invalid_headers(self, mock_check_output):
        mock_output = """Service  Status  Other
agent    enabled  active
"""
        mock_check_output.return_value = mock_output.encode()
        result = MaasHelper.get_maas_status()
        self.assertEqual(result, {})

    @patch("helper.subprocess.check_output")
    def test_get_maas_status_invalid_startup(self, mock_check_output):
        mock_output = """Service  Startup  Current  Since
agent    unknown  active   today at 07:48 UTC
"""
        mock_check_output.return_value = mock_output.encode()
        result = MaasHelper.get_maas_status()
        self.assertEqual(result, {})

    @patch("helper.subprocess.check_output")
    def test_get_maas_status_invalid_status(self, mock_check_output):
        mock_output = """Service  Startup   Current  Since
agent    enabled   unknown  today at 07:48 UTC
"""
        mock_check_output.return_value = mock_output.encode()
        result = MaasHelper.get_maas_status()
        self.assertEqual(result, {})

    @patch("helper.subprocess.check_output")
    def test_get_maas_status_malformed_line(self, mock_check_output):
        mock_output = """Service  Startup  Current  Since
agent    enabled  active   today at 07:48 UTC
apiserver
regiond  enabled  active   today at 07:48 UTC
"""
        mock_check_output.return_value = mock_output.encode()
        result = MaasHelper.get_maas_status()
        # Should skip the malformed line but keep the valid ones
        self.assertEqual(len(result), 2)
        self.assertIn("agent", result)
        self.assertIn("regiond", result)
        self.assertNotIn("apiserver", result)

    @patch("helper.subprocess.check_output")
    def test_get_maas_status_mixed_valid_invalid(self, mock_check_output):
        mock_output = """Service    Startup   Current   Since
agent      disabled  active    today at 07:48 UTC
apiserver  invalid   active    today at 07:48 UTC
regiond    enabled   unknown   today at 07:48 UTC
rackd      enabled   active    today at 07:48 UTC
"""
        mock_check_output.return_value = mock_output.encode()
        result = MaasHelper.get_maas_status()
        # Should only include valid entries
        self.assertEqual(len(result), 2)
        self.assertIn("agent", result)
        self.assertIn("rackd", result)
        self.assertNotIn("apiserver", result)
        self.assertNotIn("regiond", result)


class TestHelperSetup(unittest.TestCase):
    @patch("helper.subprocess.check_call")
    def test_setup_region(self, mock_run):
        MaasHelper.setup_region(
            "http://1.1.1.1:5240", "postgresql://user:pass@2.2.2.2:5432/db", "region"
        )
        mock_run.assert_called_once_with(
            [
                "/snap/bin/maas",
                "init",
                "region",
                "--maas-url",
                "http://1.1.1.1:5240",
                "--database-uri",
                "postgresql://user:pass@2.2.2.2:5432/db",
                "--force",
            ]
        )

    @patch("helper.subprocess.check_call")
    def test_create_admin_user(self, mock_run):
        MaasHelper.create_admin_user("user", "passwd", "email", None)
        mock_run.assert_called_once_with(
            [
                "/snap/bin/maas",
                "createadmin",
                "--username",
                "user",
                "--password",
                "passwd",
                "--email",
                "email",
            ]
        )

    @patch("helper.subprocess.check_call")
    def test_create_admin_user_with_sshkey(self, mock_run):
        MaasHelper.create_admin_user("user", "passwd", "email", "lp_user")
        mock_run.assert_called_once_with(
            [
                "/snap/bin/maas",
                "createadmin",
                "--username",
                "user",
                "--password",
                "passwd",
                "--email",
                "email",
                "--ssh-import",
                "lp_user",
            ]
        )

    @patch("helper.subprocess.check_output")
    def test_get_api_key(self, mock_run):
        mock_run.return_value = b"a-key"
        key = MaasHelper.get_api_key("user")
        self.assertEqual(key, "a-key")
        mock_run.assert_called_once_with(
            [
                "/snap/bin/maas",
                "apikey",
                "--username",
                "user",
            ]
        )

    @patch("helper.subprocess.check_call")
    def test_msm_enroll(self, mock_run):
        token = "my-jwt-token"
        MaasHelper.msm_enroll(token)
        mock_run.assert_called_once_with(
            [
                "/snap/bin/maas",
                "msm",
                "enrol",
                "--yes",
                token,
            ]
        )
