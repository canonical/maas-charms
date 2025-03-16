# Copyright 2024 Canonical
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest
from unittest.mock import MagicMock, PropertyMock, call, mock_open, patch

from charms.operator_libs_linux.v2.snap import SnapState

from helper import MAAS_SERVICE, MaasHelper


class TestHelperSnapCache(unittest.TestCase):
    def _setup_snap(
        self,
        mock_snap,
        present=False,
        revision="1234",
        channel="latest/stable",
        cohort="maas",
    ):
        maas = MagicMock()
        type(maas).present = PropertyMock(return_value=present)
        type(maas).revision = PropertyMock(return_value=revision)
        type(maas).channel = PropertyMock(return_value=channel)
        type(maas).cohort = PropertyMock(return_value=cohort)
        instance = mock_snap.return_value
        instance.__getitem__.return_value = maas
        return maas

    @patch("helper.SnapCache", autospec=True)
    def test_install(self, mock_snap):
        mock_maas = self._setup_snap(mock_snap)
        MaasHelper.install("test/channel", "maas")
        mock_maas.ensure.assert_has_calls(
            [
                call(SnapState.Latest, channel="test/channel", cohort="maas"),
            ]
        )
        mock_maas.hold.assert_called_once()

    @patch("helper.SnapCache", autospec=True)
    def test_install_already_present(self, mock_snap):
        mock_maas = self._setup_snap(mock_snap, present=True)
        MaasHelper.install("test/channel", "maas")
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
    def test_refresh(self, mock_snap):
        mock_service = MagicMock()

        # return False the first time, then True every subsequent time
        def side_effect(key, default=None):
            if side_effect.call_count == 0:
                side_effect.call_count += 1
                return False
            return True

        side_effect.call_count = 0
        mock_service.get.side_effect = side_effect

        mock_maas = self._setup_snap(mock_snap, present=True)
        mock_maas.services.get.return_value = mock_service

        MaasHelper.refresh("test/upgrade", "maas")
        mock_maas.stop.assert_called_once()
        mock_maas.ensure.assert_has_calls(
            [
                call(SnapState.Present, channel="test/upgrade", cohort="maas"),
            ]
        )
        mock_maas.start.assert_called_once()

    @patch("helper.SnapCache", autospec=True)
    def test_get_installed_version(self, mock_snap):
        self._setup_snap(mock_snap, present=True, revision="12345")
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

    @patch("pathlib.Path.open", new_callable=lambda: mock_open(read_data="secret\n"))
    def test_get_maas_secret(self, _):
        self.assertEqual(MaasHelper.get_maas_secret(), "secret")

    @patch("pathlib.Path.open", side_effect=OSError)
    def test_get_maas_secret_not_initialised(self, _):
        self.assertIsNone(MaasHelper.get_maas_secret())

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
