# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""Helper functions for MAAS management."""

import subprocess
from pathlib import Path

from charms.operator_libs_linux.v2.snap import SnapCache, SnapState

MAAS_SNAP_NAME = "maas"
MAAS_MODE = Path("/var/snap/maas/common/snap_mode")
MAAS_SECRET = Path("/var/snap/maas/common/maas/secret")
MAAS_ID = Path("/var/snap/maas/common/maas/maas_id")
MAAS_SERVICE = "pebble"


class MaasHelper:
    """MAAS helper."""

    @staticmethod
    def install(channel: str) -> None:
        """Install snap.

        Args:
            channel (str): snapstore channel
        """
        maas = SnapCache()[MAAS_SNAP_NAME]
        if not maas.present:
            maas.ensure(SnapState.Latest, channel=channel)
            maas.hold()

    @staticmethod
    def uninstall() -> None:
        """Uninstall snap."""
        maas = SnapCache()[MAAS_SNAP_NAME]
        if maas.present:
            maas.ensure(SnapState.Absent)

    @staticmethod
    def get_installed_version() -> str | None:
        """Get installed version.

        Returns:
            Union[str, None]: version if installed
        """
        maas = SnapCache()[MAAS_SNAP_NAME]
        return maas.version.split("-")[0] if maas.version is not None and maas.present else None

    @staticmethod
    def get_installed_channel() -> str | None:
        """Get installed channel.

        Returns:
            Union[str, None]: channel if installed
        """
        maas = SnapCache()[MAAS_SNAP_NAME]
        return maas.channel if maas.present else None

    @staticmethod
    def get_maas_id() -> str | None:
        """Get MAAS system ID.

        Returns:
            Union[str, None]: system_id, or None if not present
        """
        try:
            with MAAS_ID.open() as file:
                return file.readline().strip()
        except OSError:
            return None

    @staticmethod
    def get_maas_mode() -> str | None:
        """Get MAAS operation mode.

        Returns:
            Union[str, None]: mode, or None if not initialised
        """
        try:
            with MAAS_MODE.open() as file:
                return file.readline().strip()
        except OSError:
            return None

    @staticmethod
    def is_running() -> bool:
        """Check if MAAS is running.

        Returns:
            boot: whether the service is running
        """
        maas = SnapCache()[MAAS_SNAP_NAME]
        service = maas.services.get(MAAS_SERVICE, {})
        return service.get("activate", False)

    @staticmethod
    def set_running(enable: bool) -> None:
        """Set service status.

        Args:
            enable (bool): enable service
        """
        maas = SnapCache()[MAAS_SNAP_NAME]
        if enable:
            maas.start()
        else:
            maas.stop()

    @staticmethod
    def setup_rack(maas_url: str, secret: str) -> None:
        """Initialize a Rack/Agent controller.

        Args:
            maas_url (str):  URL that MAAS should use for communicate from the
                nodes to MAAS and other controllers of MAAS.
            secret (str): Enrollement token

        Raises:
            CalledProcessError: failed to initialize MAAS
        """
        cmd = [
            "/snap/bin/maas",
            "init",
            "rack",
            "--maas-url",
            maas_url,
            "--secret",
            secret,
            "--force",
        ]
        subprocess.check_call(cmd)
