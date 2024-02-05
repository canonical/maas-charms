#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""Charm the application."""

import logging

import ops
from charms.maas_region_charm.v0 import maas
from helper import MaasHelper

logger = logging.getLogger(__name__)

MAAS_SNAP_NAME = "maas"
MAAS_RELATION_NAME = "maas-region"

# FIXME must include external services also (e.g. DHCP)
MAAS_RACK_PORTS = [ops.Port("tcp", 5248)]

MAAS_SNAP_CHANNEL = "3.4/stable"


class MaasRackCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, *args):
        super().__init__(*args)

        # Charm lifecycle
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._on_start)

        # MAAS relation
        self.maas_region = maas.MaasRegionRequires(self)
        self.framework.observe(
            self.maas_region.on.config_received, self._on_maas_config_received
        )
        self.framework.observe(self.maas_region.on.created, self._on_maas_created)
        # self.framework.observe(self.maas_region.on.removed, self._on_maas_removed)

    @property
    def version(self) -> str:
        """Reports the current workload version.

        Returns:
            str: the version, or empty if not installed
        """
        if ver := MaasHelper.get_installed_version():
            return ver
        return ""

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
        if config := self.maas_region.get_enroll_data():
            return MaasHelper.setup_rack(
                config.api_url,
                config.maas_secret,
            )
        return False

    def _on_start(self, _event: ops.StartEvent) -> None:
        """Handle the MAAS controller startup.

        Args:
            event (ops.StartEvent): Event from ops framework
        """
        if not self.maas_region.get_enroll_data():
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

    def _on_install(self, _event: ops.InstallEvent) -> None:
        """Install MAAS in the machine.

        Args:
            event (ops.InstallEvent): Event from ops framework
        """
        self.unit.status = ops.MaintenanceStatus("installing...")
        try:
            MaasHelper.install(MAAS_SNAP_CHANNEL)
        except Exception as ex:
            logger.error(str(ex))
            self.unit.status = ops.ErrorStatus(
                f"Failed to install MAAS snap from channel '{MAAS_SNAP_CHANNEL}'"
            )
            return
        self.unit.status = ops.MaintenanceStatus("initializing...")

    def _on_collect_status(self, e: ops.CollectStatusEvent) -> None:
        # FIXME

    def _on_maas_config_received(self, _event: maas.MaasConfigReceivedEvent) -> None:
        self.unit.status = ops.MaintenanceStatus("enrolling...")
        if not self._initialize_maas():
            self.unit.status = ops.ErrorStatus("Failed to initialize MAAS")
            return
        self.unit.status = ops.ActiveStatus()

    def _on_maas_created(self, event: ops.RelationCreatedEvent):
        if id := MaasHelper.get_maas_id():
            self.maas_region.publish_unit_system_id(id)
        else:
            event.defer()



if __name__ == "__main__":  # pragma: nocover
    ops.main(MaasRackCharm)  # type: ignore
