#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""Charm the application."""

import json
import logging
import socket
import subprocess
from typing import Any, List, Union

import ops
import yaml
from charms.data_platform_libs.v0 import data_interfaces as db
from charms.grafana_agent.v0 import cos_agent
from charms.maas_region.v0 import maas
from charms.operator_libs_linux.v2.snap import SnapError
from charms.tempo_coordinator_k8s.v0.charm_tracing import trace_charm
from charms.tempo_coordinator_k8s.v0.tracing import TracingEndpointRequirer, charm_tracing_config

from helper import MaasHelper

logger = logging.getLogger(__name__)

MAAS_PEER_NAME = "maas-cluster"
MAAS_API_RELATION = "api"
MAAS_DB_NAME = "maas-db"

MAAS_SNAP_CHANNEL = "3.5/stable"

MAAS_PROXY_PORT = 80

MAAS_HTTP_PORT = 5240
MAAS_HTTPS_PORT = 5443
MAAS_REGION_METRICS_PORT = 5239
MAAS_CLUSTER_METRICS_PORT = MAAS_HTTP_PORT

MAAS_REGION_PORTS = [
    ops.Port("udp", 53),  # named
    ops.Port("udp", 67),  # dhcpd
    ops.Port("udp", 69),  # tftp
    ops.Port("udp", 123),  # chrony
    ops.Port("udp", 323),  # chrony
    *[ops.Port("udp", p) for p in range(5241, 5247 + 1)],  # Internal services
    ops.Port("tcp", 53),  # named
    ops.Port("tcp", 3128),  # squid
    ops.Port("tcp", 8000),  # squid
    ops.Port("tcp", MAAS_HTTP_PORT),  # API
    ops.Port("tcp", MAAS_HTTPS_PORT),  # API
    ops.Port("tcp", MAAS_REGION_METRICS_PORT),
    *[ops.Port("tcp", p) for p in range(5241, 5247 + 1)],  # Internal services
    *[ops.Port("tcp", p) for p in range(5250, 5270 + 1)],  # RPC Workers
    *[ops.Port("tcp", p) for p in range(5270, 5274 + 1)],  # Temporal
    *[ops.Port("tcp", p) for p in range(5280, 5284 + 1)],  # Temporal
]


@trace_charm(
    tracing_endpoint="charm_tracing_endpoint",
    extra_types=[
        cos_agent.COSAgentProvider,
        maas.MaasRegionProvider,
        db.DatabaseRequires,
        MaasHelper,
    ],
)
class MaasRegionCharm(ops.CharmBase):
    """Charm the application."""

    _TLS_MODES = [
        "disabled",
        "termination",
        "passthrough",
    ]  # no TLS, termination at HA Proxy, passthrough to MAAS

    def __init__(self, *args):
        super().__init__(*args)

        # Charm lifecycle
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.remove, self._on_remove)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)

        # MAAS Region
        self.maas_region = maas.MaasRegionProvider(self)
        maas_region_events = self.on[maas.DEFAULT_ENDPOINT_NAME]
        self.framework.observe(maas_region_events.relation_joined, self._on_maas_cluster_changed)
        self.framework.observe(maas_region_events.relation_changed, self._on_maas_cluster_changed)
        self.framework.observe(maas_region_events.relation_departed, self._on_maas_cluster_changed)
        self.framework.observe(maas_region_events.relation_broken, self._on_maas_cluster_changed)
        self.framework.observe(
            maas_region_events.relation_changed, self._on_maas_cluster_data_changed
        )

        maas_peer_events = self.on[MAAS_PEER_NAME]
        self.framework.observe(maas_peer_events.relation_joined, self._on_maas_peer_changed)
        self.framework.observe(maas_peer_events.relation_changed, self._on_maas_peer_changed)
        self.framework.observe(maas_peer_events.relation_departed, self._on_maas_peer_changed)
        self.framework.observe(maas_peer_events.relation_broken, self._on_maas_peer_changed)

        # MAAS DB
        self.maasdb_name = f'{self.app.name.replace("-", "_")}_db'
        self.maasdb = db.DatabaseRequires(self, MAAS_DB_NAME, self.maasdb_name)
        self.framework.observe(self.maasdb.on.database_created, self._on_maasdb_created)
        self.framework.observe(self.maasdb.on.endpoints_changed, self._on_maasdb_endpoints_changed)

        # HAProxy
        api_events = self.on[MAAS_API_RELATION]
        self.framework.observe(api_events.relation_changed, self._on_api_endpoint_changed)
        self.framework.observe(api_events.relation_departed, self._on_api_endpoint_changed)
        self.framework.observe(api_events.relation_broken, self._on_api_endpoint_changed)

        # COS
        self._grafana_agent = cos_agent.COSAgentProvider(
            self,
            metrics_endpoints=[
                {"path": "/metrics", "port": MAAS_REGION_METRICS_PORT},
                {"path": "/MAAS/metrics", "port": MAAS_CLUSTER_METRICS_PORT},
            ],
            metrics_rules_dir="./src/prometheus",
            logs_rules_dir="./src/loki",
            dashboard_dirs=["./src/grafana_dashboards"],
        )
        self.tracing = TracingEndpointRequirer(self, protocols=["otlp_http"])
        self.charm_tracing_endpoint, _ = charm_tracing_config(self.tracing, None)

        # Charm actions
        self.framework.observe(self.on.create_admin_action, self._on_create_admin_action)
        self.framework.observe(self.on.get_api_key_action, self._on_get_api_key_action)
        self.framework.observe(self.on.list_controllers_action, self._on_list_controllers_action)
        self.framework.observe(self.on.get_api_endpoint_action, self._on_get_api_endpoint_action)

        # Charm configuration
        self.framework.observe(self.on.config_changed, self._on_config_changed)

    @property
    def peers(self) -> Union[ops.Relation, None]:
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
    def version(self) -> Union[str, None]:
        """Reports the current workload version.

        Returns:
            str: the version, or None if not installed
        """
        return MaasHelper.get_installed_version()

    @property
    def enrollment_token(self) -> Union[str, None]:
        """Reports the enrollment token.

        Returns:
            str: the otken, or None if not available
        """
        return MaasHelper.get_maas_secret()

    @property
    def bind_address(self) -> Union[str, None]:
        """Get Unit bind address.

        Returns:
            str: A single address that the charm's application should bind() to.
        """
        if bind := self.model.get_binding("juju-info"):
            return str(bind.network.bind_address)
        return None

    @property
    def maas_api_url(self) -> str:
        """Get MAAS API URL.

        Returns:
            str: The API URL
        """
        if relation := self.model.get_relation(MAAS_API_RELATION):
            unit = next(iter(relation.units), None)
            if unit and (addr := relation.data[unit].get("public-address")):
                return f"http://{addr}:{MAAS_PROXY_PORT}/MAAS"
        if bind := self.bind_address:
            return f"http://{bind}:{MAAS_HTTP_PORT}/MAAS"
        return ""

    @property
    def maas_id(self) -> Union[str, None]:
        """Reports the MAAS ID.

        Returns:
            str: the ID, or None if not initialized
        """
        return MaasHelper.get_maas_id()

    def get_operational_mode(self) -> str:
        """Get expected MAAS mode.

        Returns:
            str: either `region` of `region+rack`
        """
        has_agent = self.maas_region.gather_rack_units().get(socket.getfqdn())
        return "region+rack" if has_agent else "region"

    def _set_peer_data_(
        self,
        peer: Union[ops.Relation, None],
        app_or_unit: Union[ops.Application, ops.Unit, None],
        key: str,
        data: Any,
    ) -> None:
        if not peer:
            return
        peer.data[app_or_unit or peer.app][key] = json.dumps(data or {})

    def _get_peer_data_(
        self,
        peer: Union[ops.Relation, None],
        app_or_unit: Union[ops.Application, ops.Unit, None],
        key: str,
    ) -> Any:
        if not peer:
            return {}
        return json.loads(data) if (data := peer.data[app_or_unit or peer.app].get(key)) else {}

    def set_peer_data(
        self, app_or_unit: Union[ops.Application, ops.Unit], key: str, data: Any
    ) -> None:
        """Put information into the peer data bucket."""
        self._set_peer_data_(self.peers, app_or_unit, key, data)

    def get_peer_data(self, app_or_unit: Union[ops.Application, ops.Unit], key: str) -> Any:
        """Retrieve information from the peer data bucket."""
        return self._get_peer_data_(self.peers, app_or_unit, key)

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
        try:
            MaasHelper.setup_region(
                self.maas_api_url, self.connection_string, self.get_operational_mode()
            )
            # check maas_api_url existence in case MAAS isn't ready yet
            if self.maas_api_url and self.unit.is_leader():
                self._update_tls_config()
            return True
        except subprocess.CalledProcessError:
            return False

    def _publish_tokens(self) -> bool:
        if self.maas_api_url and self.enrollment_token:
            self.maas_region.publish_enroll_token(
                self.maas_api_url,
                self._get_regions(),
                self.enrollment_token,
            )
            return True
        return False

    def _get_regions(self) -> List[str]:
        eps = [socket.getfqdn()]
        if peers := self.peers:
            for u in peers.units:
                if addr := self.get_peer_data(u, "system-name"):
                    eps += [addr]
        return list(set(eps))

    def _update_ha_proxy(self) -> None:
        region_port = (
            MAAS_HTTPS_PORT if self.config["tls_mode"] == "passthrough" else MAAS_HTTP_PORT
        )
        if relation := self.model.get_relation(MAAS_API_RELATION):
            app_name = f"api-{self.app.name}"
            data = [
                {
                    "service_name": "haproxy_service" if MAAS_PROXY_PORT == 80 else app_name,
                    "service_host": "0.0.0.0",
                    "service_port": MAAS_PROXY_PORT,
                    "service_options": ["mode http", "balance leastconn"],
                    "servers": [
                        (
                            f"{app_name}-{self.unit.name.replace('/', '-')}",
                            self.bind_address,
                            region_port,
                            [],
                        )
                    ],
                },
            ]
            if self.config["tls_mode"] != "disabled":
                data.append(
                    {
                        "service_name": "agent_service",
                        "service_host": "0.0.0.0",
                        "service_port": MAAS_PROXY_PORT,
                        "servers": [
                            (
                                f"{app_name}-{self.unit.name.replace('/', '-')}",
                                self.bind_address,
                                MAAS_HTTP_PORT,
                                [],
                            )
                        ],
                    }
                )
            relation.data[self.unit]["services"] = yaml.safe_dump(data)

    def _update_tls_config(self) -> None:
        """Enable or disable TLS in MAAS."""
        if (tls_enabled := MaasHelper.is_tls_enabled()) is not None:
            if not tls_enabled and self.config["tls_mode"] == "passthrough":
                MaasHelper.create_tls_files(
                    self.config["ssl_cert_content"],  # type: ignore
                    self.config["ssl_key_content"],  # type: ignore
                    self.config["ssl_cacert_content"],  # type: ignore
                )
                MaasHelper.enable_tls()
                MaasHelper.delete_tls_files()
            elif tls_enabled and self.config["tls_mode"] in ["disabled", "termination"]:
                MaasHelper.disable_tls()

    def _on_start(self, _event: ops.StartEvent) -> None:
        """Handle the MAAS controller startup.

        Args:
            event (ops.StartEvent): Event from ops framework
        """
        self.unit.status = ops.MaintenanceStatus("starting...")
        self._setup_network()
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
        self._write_app_type_(self.unit, "region")
        self._ensure_maas_cohort(_event)

        _cohort = self.get_cohort()
        if not _cohort:
            logger.exception("Snap cohort not found")
            return

        try:
            MaasHelper.install(MAAS_SNAP_CHANNEL, cohort_key=_cohort)
        except Exception as ex:
            logger.error(str(ex))

    def _on_remove(self, _event: ops.RemoveEvent) -> None:
        """Remove MAAS from the machine.

        Args:
            event (ops.RemoveEvent): Event from ops framework
        """
        self.unit.status = ops.MaintenanceStatus("removing...")
        try:
            MaasHelper.uninstall()
        except Exception as ex:
            logger.error(str(ex))

    def _on_upgrade_charm(self, _event: ops.UpgradeCharmEvent) -> None:
        """Upgrade MAAS installation on the machine.

        Args:
            event (ops.UpgradeCharmEvent): Event from ops framework
        """
        self.unit.status = ops.MaintenanceStatus(f"upgrading to {MAAS_SNAP_CHANNEL}...")

        if self.unit.is_leader():
            self._set_regions_updating_(True)

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
            # write this so the leader can coordinate
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
        elif not self.unit.opened_ports().issuperset(MAAS_REGION_PORTS):
            e.add_status(ops.WaitingStatus("Waiting for service ports"))
        elif not self.connection_string:
            e.add_status(ops.WaitingStatus("Waiting for database DSN"))
        elif not self.maas_api_url:
            ops.WaitingStatus("Waiting for MAAS initialization")
        else:
            self.unit.status = ops.ActiveStatus()

    def _on_maasdb_created(self, event: db.DatabaseCreatedEvent) -> None:
        """Database is ready.

        Args:
            event (DatabaseCreatedEvent): event from DatabaseRequires
        """
        logger.info(f"MAAS database credentials received for user '{event.username}'")
        if self.connection_string:
            self.unit.status = ops.MaintenanceStatus("Initialising the MAAS database")
            self._initialize_maas()

    def _on_maasdb_endpoints_changed(self, event: db.DatabaseEndpointsChangedEvent) -> None:
        """Update database DSN.

        Args:
            event (DatabaseEndpointsChangedEvent): event from DatabaseRequires
        """
        logger.info(f"MAAS database endpoints have been changed to: {event.endpoints}")
        if self.connection_string:
            self.unit.status = ops.MaintenanceStatus("Updating database connection")
            self._initialize_maas()

    def _on_api_endpoint_changed(self, event: ops.RelationEvent) -> None:
        logger.info(event)
        self._update_ha_proxy()
        self._initialize_maas()
        if self.unit.is_leader():
            self._publish_tokens()

    def _on_maas_peer_changed(self, event: ops.RelationEvent) -> None:
        logger.info(event)
        self.set_peer_data(self.unit, "system-name", socket.getfqdn())
        if self.unit.is_leader():
            self._publish_tokens()

    def _on_maas_cluster_changed(self, event: ops.RelationEvent) -> None:
        logger.info(event)
        if self.unit.is_leader() and not self._publish_tokens():
            event.defer()
            return
        if cur_mode := MaasHelper.get_maas_mode():
            if self.get_operational_mode() != cur_mode:
                self._initialize_maas()

    @property
    def maas_units(self) -> Union[ops.Relation, None]:
        """Fetch the provides/requires relation between region/agent."""
        return self.model.get_relation(maas.DEFAULT_ENDPOINT_NAME)

    def get_cohort(self) -> Union[str, None]:
        """Read the snap cohort from the region/agent relation."""
        return self._get_peer_data_(self.maas_units, app_or_unit=None, key="cohort")

    def set_cohort(self, cohort: str) -> None:
        """Write the snap cohort to the region/agent relation."""
        self._set_peer_data_(self.maas_units, app_or_unit=None, key="cohort", data=cohort)

    def _write_snap_version_(self) -> None:
        # write the snap version to the relation databag
        self._set_peer_data_(self.maas_units, self.unit, "snap-channel", MAAS_SNAP_CHANNEL)

    def _get_snap_version(self, unit: Union[ops.Unit, None]) -> str:
        # read the snap version from the relation databag
        return self._get_peer_data_(self.maas_units, unit or self.unit, "snap-channel")

    def _write_app_type_(self, unit: Union[ops.Unit, None], app: str) -> None:
        # Write the app type (region/agent) to the relation databag
        return self._set_peer_data_(self.maas_units, unit or self.unit, "app", app)

    def _get_app_type_(self, unit: Union[ops.Unit, None]) -> str:
        # read the app type (region/agent) from the relation databag
        return self._get_peer_data_(self.maas_units, unit or self.unit, "app")

    def _set_regions_updating_(self, updating: bool = False) -> None:
        # set the updating flag to true for regions
        self._set_peer_data_(
            self.maas_units,
            app_or_unit=None,
            key="region-update",
            data="true" if updating else "false",
        )

    def _set_agents_updating_(self, updating: bool = False) -> None:
        # set the updating flag to true for agents
        self._set_peer_data_(
            self.maas_units,
            app_or_unit=None,
            key="agent-update",
            data="true" if updating else "false",
        )

    @property
    def _regions_updating_(self) -> bool:
        # Check if the updating flag is true for regions
        return (
            self._get_peer_data_(self.maas_units, app_or_unit=None, key="region-update") == "true"
        )

    @property
    def _agents_updating_(self) -> bool:
        # Check if the updating flag is true for agents
        return (
            self._get_peer_data_(self.maas_units, app_or_unit=None, key="agent-update") == "true"
        )

    def _on_maas_cluster_data_changed(self, event: ops.RelationChangedEvent) -> None:
        logger.info(event)

        # The leader needs to handle information flow
        if self.unit.is_leader():

            if peers := self.maas_units:
                # wait for regions
                if any(
                    self._get_snap_version(unit) != MAAS_SNAP_CHANNEL
                    for unit in peers.units
                    if self._get_app_type_(unit) == "region"
                ):
                    self.unit.status = ops.MaintenanceStatus("Waiting for regions to refresh")
                    event.defer()
                    return

                # upgrade agents if the regions are done
                if self._regions_updating_:
                    self._set_regions_updating_(False)
                    self._set_agents_updating_(True)

                # wait for agents
                if any(
                    self._get_snap_version(unit) != MAAS_SNAP_CHANNEL
                    for unit in peers.units
                    if self._get_app_type_(unit) == "agent"
                ):
                    self.unit.status = ops.MaintenanceStatus("Waiting for agents to refresh")
                    event.defer()
                    return

                if self._agents_updating_:
                    self._set_agents_updating_(False)

        # regions should block until upgraded
        if MaasHelper.get_installed_channel() != MAAS_SNAP_CHANNEL:
            self.unit.status = ops.BlockedStatus("Awaiting unit refresh")
            logger.exception("Awaiting unit refresh")
            return

    def _ensure_maas_cohort(self, event: ops.InstallEvent) -> None:
        logger.info(event)
        _cohort = self.get_cohort()

        if self.unit.is_leader():
            if not _cohort:
                logger.debug("Cohort not found in databag")
                _cohort = MaasHelper.get_or_create_snap_cohort()

            if not _cohort:
                msg = "Could not find or create MAAS snap cohort"
                logger.debug(msg)
                self.unit.status = ops.BlockedStatus(msg)
                return

            logger.debug(f"Cohort found: {_cohort}")

            self.set_cohort(_cohort)
            logger.debug(_cohort)
            return

        if not _cohort:
            event.defer()
            return

    def _on_create_admin_action(self, event: ops.ActionEvent):
        """Handle the create-admin action.

        Args:
            event (ops.ActionEvent): Event from the framework
        """
        username = event.params["username"]
        password = event.params["password"]
        email = event.params["email"]
        ssh_import = event.params.get("ssh-import")

        try:
            MaasHelper.create_admin_user(username, password, email, ssh_import)
            event.set_results({"info": f"user {username} successfully created"})
        except subprocess.CalledProcessError:
            event.fail(f"Failed to create user {username}")

    def _on_get_api_key_action(self, event: ops.ActionEvent):
        """Handle the get-api-key action.

        Args:
            event (ops.ActionEvent): Event from the framework
        """
        username = event.params["username"]
        try:
            key = MaasHelper.get_api_key(username)
            event.set_results({"api-key": key})
        except subprocess.CalledProcessError:
            event.fail(f"Failed to get key for user {username}")

    def _on_list_controllers_action(self, event: ops.ActionEvent):
        """Handle the list-controllers action."""
        event.set_results(
            {
                "regions": json.dumps(self._get_regions()),
                "agents": json.dumps(list(self.maas_region.gather_rack_units().keys())),
            }
        )

    def _on_get_api_endpoint_action(self, event: ops.ActionEvent):
        """Handle the get-api-endpoint action."""
        if url := self.maas_api_url:
            event.set_results({"api-url": url})
        else:
            event.fail("MAAS is not initialized yet")

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        # validate tls_mode
        tls_mode = self.config["tls_mode"]
        if tls_mode not in self._TLS_MODES:
            msg = f"Invalid tls_mode configuration: '{tls_mode}'. Valid options are: {self._TLS_MODES}"
            self.unit.status = ops.BlockedStatus(msg)
            raise ValueError(msg)
        # validate certificate and key
        if tls_mode == "passthrough":
            cert = self.config["ssl_cert_content"]
            key = self.config["ssl_key_content"]
            if not cert or not key:
                raise ValueError(
                    "Both ssl_cert_content and ssl_key_content must be defined when using tls_mode=passthrough"
                )
        self._update_ha_proxy()
        if self.unit.is_leader():
            self._update_tls_config()


if __name__ == "__main__":  # pragma: nocover
    ops.main(MaasRegionCharm)  # type: ignore
