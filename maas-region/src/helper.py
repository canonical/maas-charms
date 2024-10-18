# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""Helper functions for MAAS management."""

import re
import subprocess
from logging import getLogger
from pathlib import Path
from time import sleep
from typing import Optional, Union

from charms.operator_libs_linux.v2.snap import SnapCache, SnapState

MAAS_SNAP_NAME = "maas"
MAAS_MODE = Path("/var/snap/maas/common/snap_mode")
MAAS_SECRET = Path("/var/snap/maas/common/maas/secret")
MAAS_ID = Path("/var/snap/maas/common/maas/maas_id")
MAAS_SERVICE = "pebble"


class MaasHelper:
    """MAAS helper."""

    @staticmethod
    def install(channel: str, cohort_key: Optional[str] = None) -> None:
        """Install snap.

        Args:
            channel (str): snapstore channel
            cohort_key (str): cohort to join when installing snap
        """
        maas = SnapCache()[MAAS_SNAP_NAME]
        if not maas.present:
            maas.ensure(SnapState.Latest, channel=channel)
            maas.hold()
        if cohort_key:
            maas.ensure(SnapState.Present, cohort=cohort_key)
            maas._cohort = cohort_key

    @staticmethod
    def uninstall() -> None:
        """Uninstall snap."""
        maas = SnapCache()[MAAS_SNAP_NAME]
        if maas.present:
            maas.ensure(SnapState.Absent)

    @staticmethod
    def refresh(channel: str, cohort_key: Optional[str] = None) -> None:
        """Refresh snap."""
        maas = SnapCache()[MAAS_SNAP_NAME]
        service = maas.services.get(MAAS_SERVICE, {})
        maas.stop()
        while service.get("activate", False):
            sleep(1)
        maas.ensure(SnapState.Present, channel=channel)
        if cohort_key:
            maas.ensure(SnapState.Present, cohort=cohort_key)
            maas._cohort = cohort_key
        maas.start()
        while not service.get("activate", True):
            sleep(1)
        maas.hold()

    @staticmethod
    def get_installed_version() -> Union[str, None]:
        """Get installed version.

        Returns:
            Union[str, None]: version if installed
        """
        maas = SnapCache()[MAAS_SNAP_NAME]
        return maas.revision if maas.present else None

    @staticmethod
    def get_installed_channel() -> Union[str, None]:
        """Get installed channel.

        Returns:
            Union[str, None]: channel if installed
        """
        maas = SnapCache()[MAAS_SNAP_NAME]
        return maas.channel if maas.present else None

    @staticmethod
    def get_maas_id() -> Union[str, None]:
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
    def get_maas_mode() -> Union[str, None]:
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
    def create_admin_user(
        username: str, password: str, email: str, ssh_import: Union[str, None]
    ) -> None:
        """Create an Admin user.

        Args:
            username (str): username
            password (str): new password
            email (str): user e-mail address
            ssh_import (Union[str, None]): optional ssh to import

        Raises:
            CalledProcessError: failed to create user
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

        subprocess.check_call(cmd)

    @staticmethod
    def get_api_key(username: str) -> str:
        """Get API key for a user.

        Args:
            username (str): username

        Returns:
            str: the API key

        Raises:
            CalledProcessError: failed to fetch key
        """
        cmd = [
            "/snap/bin/maas",
            "apikey",
            "--username",
            username,
        ]
        return subprocess.check_output(cmd).decode()

    @staticmethod
    def setup_region(maas_url: str, dsn: str, mode: str) -> None:
        """Initialize a Region controller.

        TODO add Vault support

        Args:
            maas_url (str):  URL that MAAS should use for communicate from the
                nodes to MAAS and other controllers of MAAS.
            dsn (str): URI for the MAAS Postgres database
            mode (str): MAAS operational mode

        Raises:
            CalledProcessError: failed to fetch key
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
        subprocess.check_call(cmd)

    @staticmethod
    def get_maas_secret() -> Union[str, None]:
        """Get MAAS enrollment secret token.

        Returns:
            Union[str, None]: token, or None if not present
        """
        try:
            with MAAS_SECRET.open() as file:
                return file.readline().strip()
        except OSError:
            return None

    @staticmethod
    def get_or_create_snap_cohort() -> Union[str, None]:
        """Return the maas snap cohort, or create a new one."""
        logger = getLogger(__name__)
        maas = SnapCache()[MAAS_SNAP_NAME]

        verbose_info = maas._snap("info", ["--verbose"])
        if _found_cohort := re.match(r"cohort:\s*([^\n]+)", verbose_info):
            return str(_found_cohort.group(1))
        logger.debug("Could not find cohort key in snap info")

        cohort_creation = maas._snap("create-cohort")
        if _created_cohort := re.match(r"cohort-key:\s+([^\n]+)", cohort_creation):
            return str(_created_cohort.group(1))

        logger.debug(f"Could not find cohort key in snap create-cohort: {_created_cohort}")
        return None
