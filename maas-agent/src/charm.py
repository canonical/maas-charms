#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""Charm the application."""

import logging
import socket
from typing import Union

import ops
from charms.grafana_agent.v0 import cos_agent
from charms.maas_region.v0 import maas
from charms.operator_libs_linux.v2.snap import SnapError
from helper import MaasHelper

logger = logging.getLogger(__name__)

MAAS_SNAP_NAME = "maas"
MAAS_RELATION_NAME = "maas-region"

MAAS_RACK_METRICS_PORT = 5249
MAAS_RACK_PORTS = [
    ops.Port("udp", 53),  # named
    ops.Port("udp", 67),  # dhcpd
    ops.Port("udp", 69),  # tftp
    ops.Port("udp", 123),  # chrony
    ops.Port("udp", 323),  # chrony
    ops.Port("tcp", 53),  # named
    ops.Port("tcp", 5240),  # nginx master
    *[ops.Port("tcp", p) for p in range(5241, 5247 + 1)],  # Internal services
    ops.Port("tcp", 5248),
    ops.Port("tcp", MAAS_RACK_METRICS_PORT),
]
MAAS_SNAP_CHANNEL = "3.4/stable"


class MaasRackCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, *args):
        super().__init__(*args)

        # Charm lifecycle
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.remove, self._on_remove)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)

        # COS
        self._grafana_agent = cos_agent.COSAgentProvider(
            self,
            metrics_endpoints=[
                {"path": "/metrics", "port": MAAS_RACK_METRICS_PORT},
            ],
        )

        # MAAS relation
        self.maas_region = maas.MaasRegionRequirer(self)
        self.framework.observe(self.maas_region.on.config_received, self._on_maas_config_received)
        self.framework.observe(self.maas_region.on.created, self._on_maas_created)
        # self.framework.observe(self.maas_region.on.removed, self._on_maas_removed)

    @property
    def version(self) -> Union[str, None]:
        """Reports the current workload version.

        Returns:
            str: the version, or None if not installed
        """
        return MaasHelper.get_installed_version()

    @property
    def maas_id(self) -> Union[str, None]:
        """Reports the MAAS ID.

        Returns:
            str: the ID, or None if not initialized
        """
        return MaasHelper.get_maas_id()

    def _setup_network(self, enable: bool) -> bool:
        """Open the network ports.

        Returns:
            bool: True if successful
        """
        try:
            if enable:
                self.unit.set_ports(*MAAS_RACK_PORTS)
            else:
                self.unit.set_ports()
        except ops.model.ModelError:
            logger.exception("failed to set service ports")
            return False
        return True

    def _initialize_maas(self) -> None:
        if config := self.maas_region.get_enroll_data():
            MaasHelper.setup_rack(
                config.api_url,
                config.get_secret(self.model),
            )

    def _on_start(self, _event: ops.StartEvent) -> None:
        """Handle the MAAS controller startup.

        Args:
            _event (ops.StartEvent): Event from ops framework
        """
        self.unit.status = ops.MaintenanceStatus("starting...")
        MaasHelper.set_running(True)
        if workload_version := self.version:
            self.unit.set_workload_version(workload_version)

    def _on_install(self, _event: ops.InstallEvent) -> None:
        """Install MAAS in the machine.

        Args:
            event (ops.InstallEvent): Event from ops framework
        """
        self.unit.status = ops.MaintenanceStatus("installing...")
        try:
            MaasHelper.install(MAAS_SNAP_CHANNEL)
        except SnapError:
            logger.exception(f"failed to install MAAS snap from channel '{MAAS_SNAP_CHANNEL}'")

    def _on_remove(self, _event: ops.RemoveEvent) -> None:
        """Remove MAAS from the machine.

        Args:
            event (ops.RemoveEvent): Event from ops framework
        """
        self.unit.status = ops.MaintenanceStatus("removing...")
        try:
            if MaasHelper.get_maas_mode() == "rack":
                MaasHelper.uninstall()
        except Exception as ex:
            logger.error(str(ex))

    def _on_collect_status(self, e: ops.CollectStatusEvent) -> None:
        if MaasHelper.get_installed_channel() != MAAS_SNAP_CHANNEL:
            e.add_status(ops.BlockedStatus("Failed to install MAAS snap"))
        elif not self.maas_region.get_enroll_data():
            e.add_status(ops.WaitingStatus("Waiting for enrollment token"))
        elif MaasHelper.get_maas_mode() == "rack" and not self.unit.opened_ports().issuperset(
            MAAS_RACK_PORTS
        ):
            e.add_status(ops.WaitingStatus("Waiting for service ports"))
        else:
            self.unit.status = ops.ActiveStatus()

    def _on_maas_config_received(self, event: maas.MaasConfigReceivedEvent) -> None:
        self.unit.status = ops.MaintenanceStatus("enrolling...")
        if socket.getfqdn() not in event.config["regions"]:
            self._initialize_maas()
            self._setup_network(True)
        else:
            self._setup_network(False)

    def _on_maas_created(self, event: ops.RelationCreatedEvent):
        self.maas_region.publish_unit_url(socket.getfqdn())


if __name__ == "__main__":  # pragma: nocover
    ops.main(MaasRackCharm)  # type: ignore
