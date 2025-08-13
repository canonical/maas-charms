#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""Charm the application."""

import json
import logging
import random
import socket
import string
import subprocess
from pathlib import Path
from typing import Any

import ops
import yaml
from charms.data_platform_libs.v0 import data_interfaces as db
from charms.grafana_agent.v0 import cos_agent
from charms.maas_region.v0 import maas
from charms.maas_site_manager_k8s.v0 import enroll
from charms.operator_libs_linux.v2.snap import SnapError
from charms.tempo_coordinator_k8s.v0.charm_tracing import trace_charm
from charms.tempo_coordinator_k8s.v0.tracing import TracingEndpointRequirer, charm_tracing_config
from ops.model import SecretNotFoundError

from backups import MAASBackups
from helper import MaasHelper

logger = logging.getLogger(__name__)

MAAS_PEER_NAME = "maas-cluster"
MAAS_API_RELATION = "api"
MAAS_DB_NAME = "maas-db"

MAAS_SNAP_CHANNEL = "3.6/stable"

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

MAAS_ADMIN_SECRET_LABEL = "maas-admin"
MAAS_ADMIN_SECRET_KEY = "maas-admin-secret-uri"

MAAS_SNAP_COMMON = "/var/snap/maas/common/maas"

MAAS_BACKUP_TYPES = ["full", "differential", "incremental"]


@trace_charm(
    tracing_endpoint="charm_tracing_endpoint",
    extra_types=[
        cos_agent.COSAgentProvider,
        maas.MaasRegionProvider,
        db.DatabaseRequires,
        MaasHelper,
        MAASBackups,
    ],
)
class MaasRegionCharm(ops.CharmBase):
    """Charm the application."""

    _TLS_MODES = frozenset(
        [
            "disabled",
            "termination",
            "passthrough",
        ]
    )  # no TLS, termination at HA Proxy, passthrough to MAAS
    _INTERNAL_ADMIN_USER = "maas-admin-internal"

    def __init__(self, *args):
        super().__init__(*args)

        # Charm lifecycle
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.remove, self._on_remove)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)

        # MAAS Region
        self.maas_region = maas.MaasRegionProvider(self)
        maas_region_events = self.on[maas.DEFAULT_ENDPOINT_NAME]
        self.framework.observe(maas_region_events.relation_joined, self._on_maas_cluster_changed)
        self.framework.observe(maas_region_events.relation_changed, self._on_maas_cluster_changed)
        self.framework.observe(maas_region_events.relation_departed, self._on_maas_cluster_changed)
        self.framework.observe(maas_region_events.relation_broken, self._on_maas_cluster_changed)

        maas_peer_events = self.on[MAAS_PEER_NAME]
        self.framework.observe(maas_peer_events.relation_joined, self._on_maas_peer_changed)
        self.framework.observe(maas_peer_events.relation_changed, self._on_maas_peer_changed)
        self.framework.observe(maas_peer_events.relation_departed, self._on_maas_peer_changed)
        self.framework.observe(maas_peer_events.relation_broken, self._on_maas_peer_changed)

        # MAAS DB
        self.maasdb_name = f"{self.app.name.replace('-', '_')}_db"
        self.maasdb = db.DatabaseRequires(self, MAAS_DB_NAME, self.maasdb_name)
        self.framework.observe(self.maasdb.on.database_created, self._on_maasdb_created)
        self.framework.observe(self.maasdb.on.endpoints_changed, self._on_maasdb_endpoints_changed)
        self.framework.observe(
            self.on[MAAS_DB_NAME].relation_broken, self._on_maasdb_relation_broken
        )

        # MAAS Site Manager relation
        self.msm = enroll.EnrollRequirer(self)
        self.framework.observe(self.msm.on.token_issued, self._on_msm_token_issued)
        self.framework.observe(self.msm.on.created, self._on_msm_created)
        self.framework.observe(self.msm.on.removed, self._on_msm_removed)

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

        # S3
        self.backup = MAASBackups(self, "s3-parameters")

        # Charm actions
        self.framework.observe(self.on.create_admin_action, self._on_create_admin_action)
        self.framework.observe(self.on.get_api_key_action, self._on_get_api_key_action)
        self.framework.observe(self.on.list_controllers_action, self._on_list_controllers_action)
        self.framework.observe(self.on.get_api_endpoint_action, self._on_get_api_endpoint_action)

        # Charm configuration
        self.framework.observe(self.on.config_changed, self._on_config_changed)

    @property
    def is_blocked(self) -> bool:
        """If the unit is in a blocked state."""
        return isinstance(self.unit.status, ops.BlockedStatus)

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
    def version(self) -> str | None:
        """Reports the current workload version.

        Returns:
            str: the version, or None if not installed
        """
        return MaasHelper.get_installed_version()

    @property
    def enrollment_token(self) -> str | None:
        """Reports the enrollment token.

        Returns:
            str: the token, or None if not available
        """
        return MaasHelper.get_maas_secret()

    @property
    def bind_address(self) -> str:
        """Get Unit bind address.

        Returns:
            str: A single address that the charm's application should bind() to.
        """
        if bind := self.model.get_binding("juju-info"):
            return str(bind.network.bind_address)
        else:
            raise ops.model.ModelError("Bind address not set in the model")

    @property
    def maas_api_url(self) -> str:
        """Get MAAS API URL.

        Returns:
            str: The API URL
        """
        if maas_url := self.config["maas_url"]:
            return str(maas_url)
        if relation := self.model.get_relation(MAAS_API_RELATION):
            unit = next(iter(relation.units), None)
            if unit and (addr := relation.data[unit].get("public-address")):
                return f"http://{addr}:{MAAS_PROXY_PORT}/MAAS"
        return f"http://{self.bind_address}:{MAAS_HTTP_PORT}/MAAS"

    @property
    def maas_id(self) -> str | None:
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
        has_agent = self.maas_region.gather_rack_units().get(socket.gethostname())
        return "region+rack" if has_agent else "region"

    def set_peer_data(self, app_or_unit: ops.Application | ops.Unit, key: str, data: Any) -> None:
        """Put information into the peer data bucket."""
        if not self.peers:
            return
        self.peers.data[app_or_unit][key] = json.dumps(data or {})

    def get_peer_data(self, app_or_unit: ops.Application | ops.Unit, key: str) -> Any:
        """Retrieve information from the peer data bucket."""
        if not self.peers:
            return {}
        data = self.peers.data[app_or_unit].get(key, "")
        return json.loads(data) if data else {}

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

    def _create_or_get_internal_admin(self) -> dict[str, str]:
        """Create an internal admin user if one does not already exist.

        Store the credentials in a secret, and return the credentials.
        If one exists, just return the credentials for the account.

        Returns:
            dict[str, str]: username and password of the admin user

        Raises:
            CalledProcessError: failed to create the user
        """
        try:
            secret = self.model.get_secret(label=MAAS_ADMIN_SECRET_LABEL)
            return secret.get_content()
        except SecretNotFoundError:
            password = "".join(
                random.SystemRandom().choice(string.ascii_letters + string.digits)
                for _ in range(15)
            )
            content = {"username": self._INTERNAL_ADMIN_USER, "password": password}

            MaasHelper.create_admin_user(content["username"], password, "", None)
            secret = self.app.add_secret(content, label=MAAS_ADMIN_SECRET_LABEL)
            self.set_peer_data(self.app, MAAS_ADMIN_SECRET_KEY, secret.id)
            return content

    def _initialize_maas(self) -> bool:
        try:
            MaasHelper.setup_region(
                self.maas_api_url, self.connection_string, self.get_operational_mode()
            )
            # check maas_api_url existence in case MAAS isn't ready yet
            if self.maas_api_url and self.unit.is_leader():
                self._update_tls_config()
                credentials = self._create_or_get_internal_admin()
                MaasHelper.set_prometheus_metrics(
                    credentials["username"],
                    self.bind_address,
                    self.config["enable_prometheus_metrics"],  # type: ignore
                )
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

    def _get_region_system_ids(self) -> set[str]:
        credentials = self._create_or_get_internal_admin()
        return MaasHelper.get_regions(
            admin_username=credentials["username"], maas_ip=self.bind_address
        )

    def _get_regions(self) -> list[str]:
        eps = [socket.gethostname()]
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

    def _update_prometheus_config(self, enable: bool) -> None:
        if secret_uri := self.get_peer_data(self.app, MAAS_ADMIN_SECRET_KEY):
            secret = self.model.get_secret(id=secret_uri)
            username = secret.get_content()["username"]
            MaasHelper.set_prometheus_metrics(username, self.bind_address, enable)

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
        channel = str(self.config.get("channel", MAAS_SNAP_CHANNEL))
        try:
            MaasHelper.install(channel)
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

    def _on_collect_status(self, e: ops.CollectStatusEvent) -> None:
        if MaasHelper.get_installed_channel() != MAAS_SNAP_CHANNEL:
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
            self.unit.status = ops.MaintenanceStatus("Initializing the MAAS database")
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

    def _on_maasdb_relation_broken(self, event: ops.RelationBrokenEvent):
        """Stop MAAS snap when database is no longer available.

        Args:
            event (ops.RelationBrokenEvent): Event from ops framework
        """
        logger.info("Stopping MAAS because database is no longer available")
        try:
            MaasHelper.stop()
        except SnapError as e:
            logger.exception("An exception occurred when stopping maas. Reason: %s", e.message)

    def _on_api_endpoint_changed(self, event: ops.RelationEvent) -> None:
        logger.info(event)
        self._update_ha_proxy()
        self._initialize_maas()
        if self.unit.is_leader():
            self._publish_tokens()

    def _on_maas_peer_changed(self, event: ops.RelationEvent) -> None:
        logger.info(event)
        self.set_peer_data(self.unit, "system-name", socket.gethostname())
        if self.unit.is_leader():
            self._publish_tokens()

    def _on_maas_cluster_changed(self, event: ops.RelationEvent) -> None:
        logger.info(event)
        # Handle data updates
        if region_id := self.get_peer_data(self.app, f"{self.unit.name}_restore_id"):
            (Path(MAAS_SNAP_COMMON) / "maas_id").write_text(f"{region_id}\n")
            self.set_peer_data(self.app, f"{self.unit.name}_restore_id", "")

        if restore_data := self.get_peer_data(self.app, f"{self.unit.name}_restore_data"):
            self.unit.status = ops.MaintenanceStatus("Restoring from backup...")
            self.set_peer_data(self.app, f"{self.unit.name}_restore_data", "")
            self.backup._run_restore_unit(
                event=ops.ActionEvent(event.handle),
                s3_parameters=restore_data["s3_parameters"],
                backup_id=restore_data["backup_id"],
            )
            self.unit.status = ops.ActiveStatus()
            return

        # Handle cluster joining
        if self.unit.is_leader() and not self._publish_tokens():
            event.defer()
            return
        try:
            creds = self._create_or_get_internal_admin()
            MaasHelper.set_prometheus_metrics(
                creds["username"],
                self.bind_address,
                self.config["enable_prometheus_metrics"],  # type: ignore
            )
        except subprocess.CalledProcessError:
            # If above failed, it's likely because things aren't ready yet.
            # we will try again
            pass
        if cur_mode := MaasHelper.get_maas_mode():
            if self.get_operational_mode() != cur_mode:
                self._initialize_maas()

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
            event.set_results({"api-key": key.strip()})
        except subprocess.CalledProcessError:
            event.fail(f"Failed to get key for user {username}")

    def _on_list_controllers_action(self, event: ops.ActionEvent):
        """Handle the list-controllers action."""
        event.set_results(
            {
                "controllers": json.dumps(
                    {
                        "regions": sorted(self._get_regions()),
                        "agents": sorted(self.maas_region.gather_rack_units().keys()),
                    }
                ),
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
        maas_details = MaasHelper.get_maas_details()
        if self.connection_string and maas_details.get("maas_url") != self.maas_api_url:
            self._initialize_maas()
            if self.unit.is_leader():
                self._publish_tokens()
        if self.unit.is_leader():
            self._update_tls_config()
            self._update_prometheus_config(self.config["enable_prometheus_metrics"])  # type: ignore

    def _on_msm_created(self, event: ops.RelationCreatedEvent) -> None:
        """MAAS Site Manager relation established.

        request enrollment token.
        """
        logger.info(event)
        if self.unit.is_leader():
            if cluster_uuid := MaasHelper.get_maas_uuid():
                self.msm.request_enroll(cluster_uuid)
            else:
                event.defer()

    def _on_msm_removed(self, event: enroll.TokenWithdrawEvent) -> None:
        """MAAS Site Manager relation removed.

        withdraw is handled by the remote end, nothing to do here.
        """
        logger.info(event)

    def _on_msm_token_issued(self, event: enroll.TokenIssuedEvent) -> None:
        """Enroll MAAS.

        use token to start the enrollment process.
        """
        logger.info(event)
        try:
            logger.debug("got enrollment token from MAAS Site Manager, enrolling")
            MaasHelper.msm_enroll(event._token)
            logger.info("enrolled to MAAS Site Manager")
        except subprocess.CalledProcessError as e:
            logger.error(f"failed to enroll: {e}")


if __name__ == "__main__":  # pragma: nocover
    ops.main(MaasRegionCharm)  # type: ignore
