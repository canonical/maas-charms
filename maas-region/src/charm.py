#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""Charm the application."""

import json
import logging
import secrets
import socket
import subprocess
from pathlib import Path
from typing import Any, List, Union, Optional

import ops
import yaml
from charms.data_platform_libs.v0 import data_interfaces as db
from charms.grafana_agent.v0 import cos_agent
from charms.maas_region.v0 import maas
from charms.vault_k8s.v0.vault_kv import (
    VaultKvConnectedEvent,
    VaultKvReadyEvent,
    VaultKvRequires,
)
from helper import MaasHelper
from vault_client import Vault

logger = logging.getLogger(__name__)

MAAS_PEER_NAME = "maas-cluster"
MAAS_API_RELATION = "api"
MAAS_DB_NAME = "maas-db"

MAAS_SNAP_CHANNEL = "3.4/stable"

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

NONCE_SECRET_LABEL = "vault-kv-nonce"
VAULT_KV_SECRET_LABEL = "vault-kv"
VAULT_KV_SECRET_PATH = "test"
VAULT_CA_CERT_FILENAME = "ca.pem"


class MaasRegionCharm(ops.CharmBase):
    """Charm the application."""

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
            # dashboard_dirs=["./src/grafana_dashboards"],
        )

        # Vault
        self.vault_kv = VaultKvRequires(self, "vault-kv", mount_suffix="kv")
        self.framework.observe(self.vault_kv.on.connected, self._on_kv_connected)
        self.framework.observe(self.vault_kv.on.ready, self._on_kv_ready)
        self.framework.observe(self.on.create_secret_action, self._on_create_secret_action)
        self.framework.observe(self.on.get_secret_action, self._on_get_secret_action)

        # Charm actions
        self.framework.observe(self.on.create_admin_action, self._on_create_admin_action)
        self.framework.observe(self.on.get_api_key_action, self._on_get_api_key_action)
        self.framework.observe(self.on.list_controllers_action, self._on_list_controllers_action)
        self.framework.observe(self.on.get_api_endpoint_action, self._on_get_api_endpoint_action)

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
            str: the token, or None if not available
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

    def set_peer_data(
        self, app_or_unit: Union[ops.Application, ops.Unit], key: str, data: Any
    ) -> None:
        """Put information into the peer data bucket."""
        if not self.peers:
            return
        self.peers.data[app_or_unit][key] = json.dumps(data or {})

    def get_peer_data(self, app_or_unit: Union[ops.Application, ops.Unit], key: str) -> Any:
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

    def _initialize_maas(self) -> bool:
        try:
            MaasHelper.setup_region(
                self.maas_api_url, self.connection_string, self.get_operational_mode()
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

    def _get_regions(self) -> List[str]:
        eps = [socket.getfqdn()]
        if peers := self.peers:
            for u in peers.units:
                if addr := self.get_peer_data(u, "system-name"):
                    eps += [addr]
        return list(set(eps))

    def _update_ha_proxy(self) -> None:
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
                            MAAS_HTTP_PORT,
                            [],
                        )
                    ],
                }
            ]
            relation.data[self.unit]["services"] = yaml.safe_dump(data)

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

        try:
            self.model.get_secret(label=NONCE_SECRET_LABEL)
        except ops.model.SecretNotFoundError:
            self.unit.add_secret(
                {"nonce": secrets.token_hex(16)},
                label=NONCE_SECRET_LABEL,
                description="Nonce for vault-kv relation",
            )

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
        if conn := self.connection_string:
            self.unit.status = ops.MaintenanceStatus("Initialising the MAAS database")
            logger.info(f"DSN: {conn}")
            self._initialize_maas()

    def _on_maasdb_endpoints_changed(self, event: db.DatabaseEndpointsChangedEvent) -> None:
        """Update database DSN.

        Args:
            event (DatabaseEndpointsChangedEvent): event from DatabaseRequires
        """
        logger.info(f"MAAS database endpoints have been changed to: {event.endpoints}")
        if conn := self.connection_string:
            self.unit.status = ops.MaintenanceStatus("Updating database connection")
            logger.info(f"DSN: {conn}")
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

    def _on_kv_connected(self, event: VaultKvConnectedEvent):
        """Request credentials from Vault KV."""
        relation = self.model.get_relation(event.relation_name, event.relation_id)
        if not relation:
            return
        binding = self.model.get_binding(relation)
        if not binding:
            logger.error("Binding not found")
            return
        egress_subnet = str(binding.network.interfaces[0].subnet)
        self.vault_kv.request_credentials(relation, egress_subnet, self.get_nonce())

    def _on_kv_ready(self, event: VaultKvReadyEvent):
        """Store the Vault KV credentials in a secret."""
        if (relation := self.model.get_relation(event.relation_name, event.relation_id)) is None:
            return
        if not (ca_certificate := self.vault_kv.get_ca_certificate(relation)):
            logger.error("CA certificate not found")
            return
        if not (vault_url := self.vault_kv.get_vault_url(relation)):
            logger.error("Vault URL not found")
            return
        if not (mount := self.vault_kv.get_mount(relation)):
            logger.error("Mount not found")
            return
        unit_credentials = self.vault_kv.get_unit_credentials(relation)
        secret = self.model.get_secret(id=unit_credentials)
        secret_content = secret.get_content(refresh=True)
        juju_secret_content = {
            "vault-url": vault_url,
            "mount": mount,
            "role-id": secret_content["role-id"],
            "wrapping-token": secret_content["wrapping-token"],
        }
        try:
            vault_kv_secret = self.model.get_secret(label=VAULT_KV_SECRET_LABEL)
            vault_kv_secret.set_content(content=juju_secret_content)
            logger.info("Vault KV secret updated")
        except ops.model.SecretNotFoundError:
            self.app.add_secret(juju_secret_content, label=VAULT_KV_SECRET_LABEL)
            logger.info("Vault KV secret created")
        self._store_ca_certificate(cert=ca_certificate)

    def _store_ca_certificate(self, cert: str) -> None:
        """Store the CA certificate in the charm storage."""
        certs_path = self._get_ca_cert_location_in_charm()
        with open(f"{certs_path}/{VAULT_CA_CERT_FILENAME}", "w") as fd:
            fd.write(cert)

    def _on_create_secret_action(self, event: ops.charm.ActionEvent):
        """Create a secret in Vault KV."""
        try:
            secret = self.model.get_secret(label=VAULT_KV_SECRET_LABEL)
        except ops.model.SecretNotFoundError:
            event.fail("Vault KV secret not found")
            return
        secret_content = secret.get_content(refresh=True)
        mount = secret_content["mount"]
        ca_certificate_path = self._get_ca_cert_location_in_charm()
        if ca_certificate_path is None:
            event.fail("CA certificate not found")
            return
        secret_key = event.params.get("key")
        secret_value = event.params.get("value")
        if not secret_key or not secret_value:
            event.fail("Missing key or value")
            return
        vault = Vault(
            url=secret_content["vault-url"],
            approle_role_id=secret_content["role-id"],
            ca_certificate=f"{ca_certificate_path}/{VAULT_CA_CERT_FILENAME}",
            wrapping_token=secret_content["wrapping-token"],
        )
        vault.create_secret_in_kv(
            path=VAULT_KV_SECRET_PATH, mount=mount, key=secret_key, value=secret_value
        )

    def _on_get_secret_action(self, event: ops.charm.ActionEvent) -> None:
        try:
            secret = self.model.get_secret(label=VAULT_KV_SECRET_LABEL)
        except ops.model.SecretNotFoundError:
            event.fail("Vault KV secret not found")
            return
        secret_content = secret.get_content(refresh=True)
        mount = secret_content["mount"]
        ca_certificate_path = self._get_ca_cert_location_in_charm()
        if ca_certificate_path is None:
            event.fail("CA certificate not found")
            return
        secret_key = event.params.get("key")
        if not secret_key:
            event.fail("Missing key or value")
            return
        vault = Vault(
            url=secret_content["vault-url"],
            approle_role_id=secret_content["role-id"],
            ca_certificate=f"{ca_certificate_path}/{VAULT_CA_CERT_FILENAME}",
            wrapping_token=secret_content["wrapping-token"],
        )
        vault_secret = vault.get_secret_in_kv(path=VAULT_KV_SECRET_PATH, mount=mount)
        if secret_key not in vault_secret:
            event.fail("Secret not found")
            return
        event.set_results({"value": vault_secret[secret_key]})

    def get_nonce(self) -> str:
        """Get the nonce from the secret."""
        secret = self.model.get_secret(label=NONCE_SECRET_LABEL)
        return secret.get_content(refresh=True)["nonce"]

    def _get_ca_cert_location_in_charm(self) -> Optional[Path]:
        """Return the CA certificate location in the charm (not in the workload).

        This path would typically be: /var/lib/juju/storage/certs/0/ca.pem

        Returns:
            Path: The CA certificate location

        Raises:
            VaultCertsError: If the CA certificate is not found
        """
        storage = self.model.storages
        if "certs" not in storage:
            return None
        if len(storage["certs"]) == 0:
            return None
        cert_storage = storage["certs"][0]
        return cert_storage.location

if __name__ == "__main__":  # pragma: nocover
    ops.main(MaasRegionCharm)  # type: ignore
