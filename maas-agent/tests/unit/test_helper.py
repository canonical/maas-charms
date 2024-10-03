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
        cohort="maas-agent",
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
        MaasHelper.install("test/channel")
        mock_maas.ensure.assert_has_calls(
            [
                call(SnapState.Latest, channel="test/channel"),
                call(SnapState.Present, cohort="maas"),
            ]
        )
        mock_maas.hold.assert_called_once()

    @patch("helper.SnapCache", autospec=True)
    def test_install_already_present(self, mock_snap):
        mock_maas = self._setup_snap(mock_snap, present=True)
        MaasHelper.install("test/channel")
        mock_maas.ensure.assert_called_once_with(SnapState.Present, cohort="maas")

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

        MaasHelper.refresh("test/upgrade")
        mock_maas.stop.assert_called_once()
        mock_maas.ensure.assert_called_once_with(
            SnapState.Present, channel="test/upgrade", cohort="maas"
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

    @patch("pathlib.Path.open", new_callable=lambda: mock_open(read_data="region+rack\n"))
    def test_get_maas_mode(self, _):
        self.assertEqual(MaasHelper.get_maas_mode(), "region+rack")

    @patch("pathlib.Path.open", side_effect=OSError)
    def test_get_maas_mode_not_initialised(self, _):
        self.assertIsNone(MaasHelper.get_maas_mode())


class TestHelperSetup(unittest.TestCase):
    @patch("helper.subprocess.check_call")
    def test_setup_rack(self, mock_run):
        MaasHelper.setup_rack("http://1.1.1.1:5240", "my_secret")
        mock_run.assert_called_once_with(
            [
                "/snap/bin/maas",
                "init",
                "rack",
                "--maas-url",
                "http://1.1.1.1:5240",
                "--secret",
                "my_secret",
                "--force",
            ]
        )
