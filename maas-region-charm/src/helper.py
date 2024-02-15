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


def _run_local(*args, **kwargs) -> int:
    """Run process in the unit environment.

    Returns:
        int: process exit status
    """
    return subprocess.Popen(*args, **kwargs).wait()


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
            str | None: version if installed
        """
        maas = SnapCache()[MAAS_SNAP_NAME]
        return maas.revision if maas.present else None

    @staticmethod
    def get_installed_channel() -> str | None:
        """Get installed channel.

        Returns:
            str | None: channel if installed
        """
        maas = SnapCache()[MAAS_SNAP_NAME]
        return maas.channel if maas.present else None

    @staticmethod
    def get_maas_id() -> str | None:
        """Get MAAS system ID.

        Returns:
            str | None: system_id, or None if not present
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
            str | None: mode, or None if not initialised
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
    def create_admin_user(
        username: str, password: str, email: str, ssh_import: str | None
    ) -> bool:
        """Create an Admin user.

        Args:
            username (str): username
            password (str): new password
            email (str): user e-mail address
            ssh_import (str | None): optional ssh to import

        Returns:
            bool: whether the user was created
        """
        cmd = [
            "/snap/bin/maas",
            "createadmin",
            "--username",
            username,
            "--password",
            password,
            "--email",
            email,
        ]
        if ssh_import is not None:
            cmd += ["--ssh-import", ssh_import]

        return _run_local(cmd) == 0

    @staticmethod
    def setup_region(maas_url: str, dsn: str, mode: str) -> bool:
        """Initialize a Region controller.

        TODO add Vault support

        Args:
            maas_url (str):  URL that MAAS should use for communicate from the
                nodes to MAAS and other controllers of MAAS.
            dsn (str): URI for the MAAS Postgres database
            mode (str): MAAS operational mode

        Returns:
            bool: whether the initialisation succeeded
        """
        cmd = [
            "/snap/bin/maas",
            "init",
            mode,
            "--maas-url",
            maas_url,
            "--database-uri",
            dsn,
            "--force",
        ]
        return _run_local(cmd) == 0

    @staticmethod
    def get_maas_secret() -> str | None:
        """Get MAAS enrollment secret token.

        Returns:
            str | None: token, or None if not present
        """
        try:
            with MAAS_SECRET.open() as file:
                return file.readline().strip()
        except OSError:
            return None
