#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""Charm the application."""

import json
import logging
import socket
from typing import Any, Union

import ops
from charms.grafana_agent.v0 import cos_agent
from charms.maas_region.v0 import maas
from charms.operator_libs_linux.v2.snap import SnapError
from charms.tempo_coordinator_k8s.v0.charm_tracing import trace_charm
from charms.tempo_coordinator_k8s.v0.tracing import TracingEndpointRequirer, charm_tracing_config

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
    ops.Port("tcp", 5240),  # nginx primary
    *[ops.Port("tcp", p) for p in range(5241, 5247 + 1)],  # Internal services
    ops.Port("tcp", 5248),
    ops.Port("tcp", MAAS_RACK_METRICS_PORT),
]
MAAS_SNAP_CHANNEL = "3.5/stable"


@trace_charm(
    tracing_endpoint="charm_tracing_endpoint",
    extra_types=[cos_agent.COSAgentProvider, maas.MaasRegionRequirer, MaasHelper],
)
class MaasRackCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, *args):
        super().__init__(*args)

        # Charm lifecycle
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.remove, self._on_remove)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)

        # COS
        self._grafana_agent = cos_agent.COSAgentProvider(
            self,
            metrics_endpoints=[
                {"path": "/metrics", "port": MAAS_RACK_METRICS_PORT},
            ],
        )
        self.tracing = TracingEndpointRequirer(self, protocols=["otlp_http"])
        self.charm_tracing_endpoint, _ = charm_tracing_config(self.tracing, None)

        # MAAS relation
        self.maas_region = maas.MaasRegionRequirer(self)
        maas_region_events = self.on[maas.DEFAULT_ENDPOINT_NAME]
        self.framework.observe(self.maas_region.on.config_received, self._on_maas_config_received)
        self.framework.observe(self.maas_region.on.created, self._on_maas_created)
        # self.framework.observe(self.maas_region.on.removed, self._on_maas_removed)
        self.framework.observe(
            maas_region_events.relation_changed, self._on_maas_cluster_data_changed
        )

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

        self._write_snap_version_()
        self._write_app_type_(self.unit, "agent")

        _cohort = self.get_cohort()
        if not _cohort:
            logger.exception("Snap cohort not found")
            return

        try:
            MaasHelper.install(MAAS_SNAP_CHANNEL, cohort_key=_cohort)
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

    def _on_upgrade(self, _event: ops.UpgradeCharmEvent) -> None:
        """Upgrade MAAS install on the machine.

        Args:
            event (ops.UpgradeCharmEvent): Event from ops framework
        """
        self.unit.status = ops.MaintenanceStatus(f"upgrading to {MAAS_SNAP_CHANNEL}...")
        if current := MaasHelper.get_installed_channel():
            if current > MAAS_SNAP_CHANNEL:
                msg = f"Cannot downgrade {current} to {MAAS_SNAP_CHANNEL}"
                self.unit.status = ops.BlockedStatus(msg)
                logger.exception(msg)
                return
            elif current == MAAS_SNAP_CHANNEL:
                logger.info("Cannot upgrade across revisions")
                return

        _cohort = self.get_cohort()
        if not _cohort:
            logger.exception("Snap cohort not found")
            return

        try:
            MaasHelper.refresh(MAAS_SNAP_CHANNEL, cohort_key=_cohort)
            self._write_snap_version_()
        except SnapError:
            logger.exception(f"failed to upgrade MAAS snap to channel '{MAAS_SNAP_CHANNEL}'")
        except Exception as ex:
            logger.error(str(ex))

    def _on_collect_status(self, e: ops.CollectStatusEvent) -> None:
        if MaasHelper.get_installed_channel() != MAAS_SNAP_CHANNEL:
            # skip if we've already set blocked due to attempting a downgrade
            if not isinstance(self.unit.status, ops.BlockedStatus):
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

    def _set_peer_data_(
        self,
        peer: Union[ops.Relation, None],
        app_or_unit: Union[ops.Application, ops.Unit, None],
        key: str,
        data: Any,
    ) -> None:
        if not peer:
            return
        peer.data[app_or_unit or peer.app][key] = data

    def _get_peer_data_(
        self,
        peer: Union[ops.Relation, None],
        app_or_unit: Union[ops.Application, ops.Unit, None],
        key: str,
    ) -> Any:
        if not peer:
            return {}
        if (data := peer.data[app_or_unit or peer.app].get(key)):
            return data
        return {}

    @property
    def maas_units(self) -> Union[ops.Relation, None]:
        """Fetch the provides/requires relation between region/agent."""
        return self.model.get_relation(maas.DEFAULT_ENDPOINT_NAME)

    def get_cohort(self) -> Union[str, None]:
        """Read the snap cohort from the region/agent relation."""
        return self._get_peer_data_(self.maas_units, app_or_unit=None, key="cohort")

    def _write_snap_version_(self) -> None:
        # write the snap version to the relation databag
        self._set_peer_data_(self.maas_units, self.unit, "snap-channel", MAAS_SNAP_CHANNEL)

    def _write_app_type_(self, unit: Union[ops.Unit, None], app: str) -> None:
        # Write the app type (region/agent) to the relation databag
        return self._set_peer_data_(self.maas_units, unit or self.unit, "app", app)

    @property
    def _agents_updating_(self) -> bool:
        # Check if the updating flag is true for agents
        return (
            self._get_peer_data_(self.maas_units, app_or_unit=None, key="agent-update") == "true"
        )

    def _on_maas_cluster_data_changed(self, event: ops.RelationChangedEvent) -> None:
        logger.info(event)

        # ensure we update our cohort if needed
        if self._agents_updating_:
            if MaasHelper.get_installed_channel() != MAAS_SNAP_CHANNEL:
                self.unit.status = ops.BlockedStatus("Awaiting unit refresh")
                logger.exception("Awaiting unit refresh")
                return


if __name__ == "__main__":  # pragma: nocover
    ops.main(MaasRackCharm)  # type: ignore
