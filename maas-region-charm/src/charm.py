#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""Charm the application."""

import json
import logging
from typing import Any

import ops
from charms.data_platform_libs.v0.data_interfaces import (
    DatabaseCreatedEvent,
    DatabaseEndpointsChangedEvent,
    DatabaseRequires,
)
from charms.maas_region_charm.v0.maas import MaasAgentEnrollEvent, MaasRegionProvides
from helper import MaasHelper

logger = logging.getLogger(__name__)

MAAS_RELATION_NAME = "maas-controller"
MAAS_PEER_NAME = "maas-region"
MAAS_DB_NAME = "maas-db"
MAAS_HTTP_PORT = 5240
MAAS_HTTPS_PORT = 5443

MAAS_SNAP_CHANNEL = "stable"

MAAS_REGION_PORTS = [
    ops.Port("udp", 53),  # named
    ops.Port("udp", 67),  # dhcpd
    ops.Port("udp", 69),  # tftp
    ops.Port("udp", 123),  # chrony
    ops.Port("udp", 3128),  # squid
    ops.Port("tcp", 53),  # named
    ops.Port("tcp", 3128),  # squid
    ops.Port("tcp", 8000),  # squid
    ops.Port("tcp", MAAS_HTTP_PORT),  # API
    ops.Port("tcp", MAAS_HTTPS_PORT),  # API
    *[ops.Port("tcp", p) for p in range(5241, 5247 + 1)],  # Internal services
    *[ops.Port("tcp", p) for p in range(5250, 5270 + 1)],  # RPC Workers
    *[ops.Port("tcp", p) for p in range(5270, 5274 + 1)],  # Temporal
    *[ops.Port("tcp", p) for p in range(5280, 5284 + 1)],  # Temporal
]


class MaasRegionCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, *args):
        super().__init__(*args)

        # Charm lifecycle
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.config_changed, self._on_config_changed)

        # MAAS relation
        self.maas_region = MaasRegionProvides(self, MAAS_RELATION_NAME)
        self.framework.observe(self.maas_region.on.agent_enroll, self._on_agent_enroll)

        # MAAS DB
        self.maasdb_name = f'{self.app.name.replace("-", "_")}_db'
        self.maasdb = DatabaseRequires(self, MAAS_DB_NAME, self.maasdb_name)
        self.framework.observe(self.maasdb.on.database_created, self._on_maasdb_created)
        self.framework.observe(self.maasdb.on.endpoints_changed, self._on_maasdb_endpoints_changed)

        # Charm actions
        self.framework.observe(self.on.create_admin_action, self._on_create_admin_action)

    @property
    def peers(self) -> ops.Relation | None:
        """Fetch the peer relation."""
        return self.model.get_relation(MAAS_PEER_NAME)

    @property
    def connection_string(self) -> str:
        """Returns the database connection string.

        Returns:
            str: the PostgreSQL connection string, if defined
        """
        data = list(self.maasdb.fetch_relation_data().values())
        if not data:
            return ""
        username = data[0].get("username")
        password = data[0].get("password")
        endpoints = data[0].get("endpoints")
        if None in [username, password, endpoints]:
            return ""
        return f"postgres://{username}:{password}@{endpoints}/{self.maasdb_name}"

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
    def enrollment_token(self) -> str:
        """Reports the enrollment token.

        Returns:
            str: the otken, or empty if not available
        """
        if token := MaasHelper.get_maas_secret():
            return token
        return ""

    @property
    def maas_api_url(self) -> str:
        """Get MAAS API URL.

        Returns:
            str: The API URL
        """
        # FIXME use VIP when HAProxy is used
        if bind := self.model.get_binding("juju-info"):
            unit_ip = bind.network.bind_address
            return f"http://{unit_ip}:{MAAS_HTTP_PORT}/MAAS"
        else:
            return ""

    def _setup_network(self) -> bool:
        """Open the network ports.

        Returns:
            bool: True if successful
        """
        try:
            self.unit.set_ports(*MAAS_REGION_PORTS)
        except ops.model.ModelError:
            logger.exception("failed to open service ports")
            return False
        return True

    def _initialize_maas(self) -> bool:
        return MaasHelper.setup_region(
            self.maas_api_url,
            self.connection_string,
        )

    def _on_start(self, event: ops.StartEvent) -> None:
        """Handle the MAAS controller startup.

        Args:
            event (ops.StartEvent): Event from ops framework
        """
        if not self.connection_string:
            self.unit.status = ops.WaitingStatus("Waiting for database DSN")
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
        """Update configuration.

        Currently only the snap channel can be configured

        Args:
            event (ops.ConfigChangedEvent): event from the ops framework
        """
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

    def _on_maasdb_created(self, event: DatabaseCreatedEvent) -> None:
        """Database is ready.

        Args:
            event (DatabaseCreatedEvent): event from DatabaseRequires
        """
        logger.info(f"MAAS database credentials received for user '{event.username}'")
        if conn := self.connection_string:
            self.unit.status = ops.MaintenanceStatus(
                "Received database credentials of the MAAS database"
            )
            logger.info(f"DSN: {conn}")
            if self._initialize_maas():
                self.unit.status = ops.ActiveStatus()
            else:
                self.unit.status = ops.ErrorStatus("Failed to initialize MAAS")

    def _on_maasdb_endpoints_changed(self, event: DatabaseEndpointsChangedEvent) -> None:
        """Update database DSN.

        Args:
            event (DatabaseEndpointsChangedEvent): event from DatabaseRequires
        """
        logger.info(f"MAAS database endpoints have been changed to: {event.endpoints}")
        if conn := self.connection_string:
            self.unit.status = ops.MaintenanceStatus("updating database connection...")
            logger.info(f"DSN: {conn}")
            MaasHelper.set_running(True)
            if self._initialize_maas():
                self.unit.status = ops.ActiveStatus()
            else:
                self.unit.status = ops.ErrorStatus("Failed to initialize MAAS")

        else:
            MaasHelper.set_running(False)
            self.unit.status = ops.WaitingStatus("Waiting for a database connection")

    def _on_agent_enroll(self, event: MaasAgentEnrollEvent) -> None:
        logger.info(f"got enrollment request {event.relation.id}")
        if self.maas_api_url and self.enrollment_token:
            self.maas_region.update_relation_data(
                event.relation.id,
                {"api_url": self.maas_api_url, "maas_secret": self.enrollment_token},
            )
        else:
            event.defer()

    def set_peer_data(self, key: str, data: Any) -> None:
        """Put information into the peer data bucket.

        Args:
            key (str): data key
            data (Any): stored value, musts be JSON-serializable
        """
        if p := self.peers:
            p.data[self.app][key] = json.dumps(data)

    def get_peer_data(self, key: str) -> Any:
        """Retrieve information from the peer data bucket.

        Args:
            key (str): data key

        Returns:
            Any: stored value, or None if the key is not present
        """
        if not self.peers:
            return None
        data = self.peers.data[self.app].get(key, "")
        return json.loads(data) if data else None

    def _on_create_admin_action(self, event: ops.ActionEvent):
        """Handle the create-admin action.

        Args:
            event (ops.ActionEvent): Event from the framework
        """
        username = event.params["username"]
        password = event.params["password"]
        email = event.params["email"]
        ssh_import = event.params.get("ssh-import")

        if MaasHelper.create_admin_user(username, password, email, ssh_import):
            event.set_results({"info": f"user {username} successfully created"})
        else:
            event.fail(f"Failed to create user {username}")


if __name__ == "__main__":  # pragma: nocover
    ops.main(MaasRegionCharm)  # type: ignore
