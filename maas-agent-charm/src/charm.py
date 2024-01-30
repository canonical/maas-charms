#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""Charm the application."""

import logging

import ops
from charms.maas_region_charm.v0.maas import MaasRegionRequires, MaasRequiresEvent
from helper import MaasHelper

logger = logging.getLogger(__name__)

MAAS_SNAP_NAME = "maas"
MAAS_RELATION_NAME = "maas-region"

# FIXME must include external services also (e.g. DHCP)
MAAS_RACK_PORTS = [ops.Port("tcp", 5248)]

MAAS_SNAP_CHANNEL = "stable"


class MaasRackCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, *args):
        super().__init__(*args)

        # Charm lifecycle
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.config_changed, self._on_config_changed)

        # MAAS relation
        self.maas_region = MaasRegionRequires(self, MAAS_RELATION_NAME)
        self.framework.observe(
            self.maas_region.on.maas_secret_changed, self._on_maas_enroll_changed
        )
        self.framework.observe(self.maas_region.on.api_url_changed, self._on_maas_enroll_changed)

    @property
    def version(self) -> str:
        """Reports the current workload version.

        Returns:
            str: the version, or empty if not installed
        """
        if ver := MaasHelper.get_installed_version():
            return ver
        return ""

    @property
    def maas_api_url(self) -> str:
        """Get MAAS API URL.

        Returns:
            str: The API URL
        """
        data = self.maas_region.fetch_relation_data()
        return data.get("api_url", "")

    @property
    def maas_secret(self) -> str:
        """Get MAAS enrollment secret.

        Returns:
            str: The token
        """
        data = self.maas_region.fetch_relation_data()
        return data.get("maas_secret", "")

    def _setup_network(self) -> bool:
        """Open the network ports.

        Returns:
            bool: True if successful
        """
        try:
            self.unit.set_ports(*MAAS_RACK_PORTS)
        except ops.model.ModelError:
            logger.exception("failed to open service ports")
            return False
        return True

    def _initialize_maas(self) -> bool:
        return MaasHelper.setup_rack(
            self.maas_api_url,
            self.maas_secret,
        )

    def _on_start(self, event: ops.StartEvent) -> None:
        """Handle the MAAS controller startup.

        Args:
            event (ops.StartEvent): Event from ops framework
        """
        if not self.maas_secret or not self.maas_api_url:
            self.unit.status = ops.WaitingStatus("Waiting for enrollment token")
            return

        if not self._setup_network():
            self.unit.status = ops.ErrorStatus("Failed to open service ports")
            return

        MaasHelper.set_running(True)

        if workload_version := self.version:
            self.unit.set_workload_version(workload_version)
        else:
            self.unit.status = ops.ErrorStatus("MAAS not installed")
            return

        if not self._initialize_maas():
            self.unit.status = ops.ErrorStatus("Failed to initialize MAAS")
            return

        self.unit.status = ops.ActiveStatus()

    def _on_install(self, event: ops.InstallEvent) -> None:
        """Install MAAS in the machine.

        Args:
            event (ops.InstallEvent): Event from ops framework
        """
        self.unit.status = ops.MaintenanceStatus("installing...")
        channel = self.config.get("channel", MAAS_SNAP_CHANNEL)
        try:
            MaasHelper.install(channel)
        except Exception as ex:
            logger.error(str(ex))
            self.unit.status = ops.ErrorStatus(
                f"Failed to install MAAS snap from channel '{channel}'"
            )
            return
        self.unit.status = ops.MaintenanceStatus("initializing...")

    def _on_config_changed(self, event: ops.ConfigChangedEvent) -> None:
        channel = self.config.get("channel", MAAS_SNAP_CHANNEL)
        if channel != MaasHelper.get_installed_channel():
            self.unit.status = ops.MaintenanceStatus("refreshing...")
            try:
                MaasHelper.install(channel)
            except Exception as ex:
                logger.error(str(ex))
                self.unit.status = ops.ErrorStatus(
                    f"Failed to refresh MAAS snap to channel '{channel}'"
                )
                return

            if ver := MaasHelper.get_installed_version():
                self.unit.set_workload_version(ver)
            self.unit.status = ops.ActiveStatus()

    def _on_maas_enroll_changed(self, event: MaasRequiresEvent) -> None:
        self.unit.status = ops.MaintenanceStatus("enrolling...")
        if not self._initialize_maas():
            self.unit.status = ops.ErrorStatus("Failed to initialize MAAS")
            return
        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":  # pragma: nocover
    ops.main(MaasRackCharm)  # type: ignore
