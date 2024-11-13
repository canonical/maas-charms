# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""Helper functions for MAAS management."""

import re
import subprocess
from logging import getLogger
from os import remove
from pathlib import Path
from time import sleep
from typing import Union

from charms.operator_libs_linux.v2.snap import SnapCache, SnapState

MAAS_SNAP_NAME = "maas"
MAAS_MODE = Path("/var/snap/maas/common/snap_mode")
MAAS_SECRET = Path("/var/snap/maas/common/maas/secret")
MAAS_ID = Path("/var/snap/maas/common/maas/maas_id")
MAAS_SERVICE = "pebble"
MAAS_SSL_CERT_FILEPATH = Path("/var/snap/maas/common/cert.pem")
MAAS_SSL_KEY_FILEPATH = Path("/var/snap/maas/common/key.pem")
MAAS_CACERT_FILEPATH = Path("/var/snap/maas/common/cacert.pem")
NGINX_CFG_FILEPATH = Path("/var/snap/maas/current/http/regiond.nginx.conf")
MAAS_HTTPS_PORT = 5443

logger = getLogger(__name__)


class MaasHelper:
    """MAAS helper."""

    @staticmethod
    def install(channel: str, cohort_key: str) -> None:
        """Install snap.

        Args:
            channel (str): snapstore channel
            cohort_key (str): cohort to join when installing snap
        """
        maas = SnapCache()[MAAS_SNAP_NAME]
        if not maas.present:
            maas.ensure(SnapState.Latest, channel=channel, cohort=cohort_key)

    @staticmethod
    def uninstall() -> None:
        """Uninstall snap."""
        maas = SnapCache()[MAAS_SNAP_NAME]
        if maas.present:
            maas.ensure(SnapState.Absent)

    @staticmethod
    def refresh(channel: str, cohort_key: str) -> None:
        """Refresh snap."""
        maas = SnapCache()[MAAS_SNAP_NAME]
        service = maas.services.get(MAAS_SERVICE, {})
        maas.stop()
        while service.get("activate", False):
            sleep(1)
        maas.ensure(SnapState.Present, channel=channel, cohort=cohort_key)
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
    def is_tls_enabled() -> Union[bool, None]:
        """Check whether MAAS currently has TLS enabled.

        Returns:
            bool | None: True if MAAS has TLS enabled, False if not, None if MAAS is not initialized
        """
        try:
            return f"listen {MAAS_HTTPS_PORT}" in NGINX_CFG_FILEPATH.read_text()
        except FileNotFoundError:
            # MAAS is not initialized yet, don't give false hope
            return None

    @staticmethod
    def delete_tls_files() -> None:
        """Delete the TLS files used for setting configuring tls."""
        if MAAS_SSL_CERT_FILEPATH.exists():
            remove(MAAS_SSL_CERT_FILEPATH)
        if MAAS_SSL_KEY_FILEPATH.exists():
            remove(MAAS_SSL_KEY_FILEPATH)
        if MAAS_CACERT_FILEPATH.exists():
            remove(MAAS_CACERT_FILEPATH)

    @staticmethod
    def create_tls_files(
        ssl_certificate: str, ssl_key: str, cacert: str = "", overwrite: bool = False
    ) -> None:
        """Ensure that the SSL certificate and private key exist.

        Args:
            ssl_certificate (str): contents of the certificate file
            ssl_key (str): contents of the private key file
            cacert (str): optionally, contents of cacert chain for a self-signed ssl_certificate
            overwrite (bool): Whether to overwrite the files if they exist already
        """
        if not MAAS_SSL_CERT_FILEPATH.exists() or overwrite:
            MAAS_SSL_CERT_FILEPATH.write_text(ssl_certificate)
        if not MAAS_SSL_KEY_FILEPATH.exists() or overwrite:
            MAAS_SSL_KEY_FILEPATH.write_text(ssl_key)
        if cacert and (not MAAS_CACERT_FILEPATH.exists() or overwrite):
            MAAS_CACERT_FILEPATH.write_text(cacert)

    @staticmethod
    def enable_tls(cacert: bool = False) -> None:
        """Set up TLS for the Region controller.

        Raises:
            CalledProcessError: if "maas config-tls enable" command failed for any reason
        """
        cmd = [
            "/snap/bin/maas",
            "config-tls",
            "enable",
            "--yes",
        ]
        if cacert:
            cmd.extend(["--cacert", str(MAAS_CACERT_FILEPATH)])
        cmd.extend([str(MAAS_SSL_KEY_FILEPATH), str(MAAS_SSL_CERT_FILEPATH)])
        subprocess.check_call(cmd)

    @staticmethod
    def disable_tls() -> None:
        """Disable TLS for the Region controller.

        Raises:
            CalledProcessError: if "maas config-tls disable" command failed for any reason
        """
        cmd = [
            "/snap/bin/maas",
            "config-tls",
            "disable",
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
        if _found_cohort := re.search(r"cohort:\s*([^\n]+)", verbose_info):
            return str(_found_cohort.group(1))
        logger.debug("Could not find cohort key in snap info")

        cohort_creation = subprocess.check_output(
            ["sudo", "snap", "create-cohort", maas._name], universal_newlines=True
        )
        if _created_cohort := re.search(r"cohort-key:\s+([^\n]+)", cohort_creation):
            return str(_created_cohort.group(1))

        logger.debug(f"Could not find cohort key in snap create-cohort: {_created_cohort}")
        return None
