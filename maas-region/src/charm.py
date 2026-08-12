#!/usr/bin/env python3
# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the application."""

import json
import logging
import random
import socket
import string
import subprocess
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse, urlunparse

import ops
from charmlibs.rollingops import OperationResult, RollingOpsManager
from charms.data_platform_libs.v0 import data_interfaces as db
from charms.grafana_agent.v0 import cos_agent
from charms.haproxy.v1.haproxy_route_tcp import HaproxyRouteTcpRequirer, LoadBalancingAlgorithm
from charms.maas_site_manager_k8s.v0 import enroll
from charms.operator_libs_linux.v2.snap import SnapError
from charms.tempo_coordinator_k8s.v0.charm_tracing import trace_charm
from charms.tempo_coordinator_k8s.v0.tracing import TracingEndpointRequirer, charm_tracing_config
from ops.model import SecretNotFoundError
from pydantic import IPvAnyAddress

from backups import S3_CONFIGURATION_BLOCKED_KEY, MAASBackups
from helper import MaasHelper

logger = logging.getLogger(__name__)

MAAS_PEER_NAME = "maas-cluster"
MAAS_DB_NAME = "maas-db"
MAAS_ROLLING_OPS_RELATION = "rollingops-peers"
MAAS_UPGRADE_RELATION = "upgrade"
HAPROXY_NON_TLS = "ingress-tcp"
HAPROXY_TLS = "ingress-tcp-tls"
HAPROXY_TEMPORAL = "ingress-tcp-temporal"
HAPROXY_INTERNAL_HTTP_API = "ingress-tcp-internal-http-api"

MAAS_SNAP_CHANNEL = "3.8/edge"

# Ubuntu bases each MAAS track's charm is published for. Must be updated when a new
# track ships, used for upgrade compatibility checks.
MAAS_TRACK_BASES: dict[str, list[str]] = {
    "3.7": ["24.04"],
    "3.8": ["26.04"],
}

MAAS_PROXY_PORT = 80
MAAS_TLS_PROXY_PORT = 443

MAAS_HTTP_PORT = 5240
MAAS_HTTPS_PORT = 5443
MAAS_REGION_METRICS_PORT = 5239
MAAS_AGENT_METRICS_PORT = 5248
MAAS_RACK_METRICS_PORT = 5249
MAAS_CLUSTER_METRICS_PORT = MAAS_HTTP_PORT
MAAS_TEMPORAL_PORT = 5271
MAAS_INTERNAL_HTTP_API_PORT = 5242

MAAS_AGENT_METRICS_ENDPOINT = "/metrics/agent"

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

MAAS_RACK_PORTS = [
    ops.Port("udp", 53),  # named
    ops.Port("udp", 67),  # dhcpd
    ops.Port("udp", 69),  # tftp
    ops.Port("udp", 123),  # chrony
    ops.Port("udp", 323),  # chrony
    ops.Port("tcp", 53),  # named
    ops.Port("tcp", 5240),  # nginx primary
    *[ops.Port("tcp", p) for p in range(5241, 5247 + 1)],  # Internal services
    ops.Port("tcp", MAAS_RACK_METRICS_PORT),
    ops.Port("tcp", MAAS_AGENT_METRICS_PORT),
]
MAAS_REGION_RACK_PORTS = list(set(MAAS_REGION_PORTS).union(MAAS_RACK_PORTS))

MAAS_ADMIN_SECRET_LABEL = "maas-admin"
MAAS_ADMIN_SECRET_KEY = "maas-admin-secret-uri"

MAAS_BACKUP_TYPES = ["full", "differential", "incremental"]

COMMON_DEFAULT_HAPROXY_ARGS = {
    "enforce_tls": False,
    "tls_terminate": False,
    "retry_count": 3,
    "retry_redispatch": True,
    "load_balancing_consistent_hashing": True,
    "load_balancing_algorithm": LoadBalancingAlgorithm.SRCIP,
    "check_rise": 2,
    "check_fall": 3,
    "check_interval": 2,
    "server_timeout": 900,
}


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a MAAS version like "3.7.2" into a comparable tuple.

    Pre-release suffixes such as "~alpha1" are dropped.
    """
    return tuple(int(part) for part in version.split("~")[0].split(".") if part.isdigit())


def _epoch_compatible(current: dict[str, list[int]], target: dict[str, list[int]]) -> bool:
    """Whether a refresh from `current` to `target` is allowed by snap epoch rules.

    snapd only permits a refresh when the target revision can read the data format
    written by the current revision.
    """
    return bool(set(current["write"]) & set(target["read"]))


def _format_epoch(epoch: dict[str, list[int]]) -> str:
    """Render an epoch for display in action results."""
    return f"read={epoch['read']} write={epoch['write']}"


@trace_charm(
    tracing_endpoint="charm_tracing_endpoint",
    extra_types=[
        cos_agent.COSAgentProvider,
        db.DatabaseRequires,
        MaasHelper,
        MAASBackups,
    ],
)
class MaasRegionCharm(ops.CharmBase):
    """Charm the application."""

    _INTERNAL_ADMIN_USER = "maas-admin-internal"

    def __init__(self, *args):
        super().__init__(*args)

        # Charm lifecycle
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.remove, self._on_remove)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)

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
        self.haproxy_non_tls_route = HaproxyRouteTcpRequirer(
            self,
            HAPROXY_NON_TLS,
            port=80,
            backend_port=MAAS_HTTP_PORT,
            **COMMON_DEFAULT_HAPROXY_ARGS,
        )
        self.framework.observe(
            self.haproxy_non_tls_route.on.ready, self._reconcile_ha_proxy_and_initialise
        )
        self.framework.observe(
            self.haproxy_non_tls_route.on.removed, self._reconcile_ha_proxy_and_initialise
        )

        self.haproxy_tls_route = HaproxyRouteTcpRequirer(
            self,
            HAPROXY_TLS,
            port=443,
            backend_port=MAAS_HTTPS_PORT,
            **COMMON_DEFAULT_HAPROXY_ARGS,
        )
        self.framework.observe(self.haproxy_tls_route.on.ready, self._reconcile_ha_proxy)
        self.framework.observe(self.haproxy_tls_route.on.removed, self._reconcile_ha_proxy)

        # Temporal
        self.haproxy_temporal_route = HaproxyRouteTcpRequirer(
            self,
            HAPROXY_TEMPORAL,
            port=MAAS_TEMPORAL_PORT,
            backend_port=MAAS_TEMPORAL_PORT,
            **COMMON_DEFAULT_HAPROXY_ARGS,
        )
        self.framework.observe(self.haproxy_temporal_route.on.ready, self._reconcile_ha_proxy)
        self.framework.observe(self.haproxy_temporal_route.on.removed, self._reconcile_ha_proxy)

        # Internal HTTP API
        self.haproxy_internal_http_api_route = HaproxyRouteTcpRequirer(
            self,
            HAPROXY_INTERNAL_HTTP_API,
            port=MAAS_INTERNAL_HTTP_API_PORT,
            backend_port=MAAS_INTERNAL_HTTP_API_PORT,
            **COMMON_DEFAULT_HAPROXY_ARGS,
        )
        self.framework.observe(
            self.haproxy_internal_http_api_route.on.ready, self._reconcile_ha_proxy
        )
        self.framework.observe(
            self.haproxy_internal_http_api_route.on.removed, self._reconcile_ha_proxy
        )

        # COS
        self._grafana_agent = cos_agent.COSAgentProvider(
            self,
            metrics_rules_dir="./src/prometheus",
            logs_rules_dir="./src/loki",
            dashboard_dirs=["./src/grafana_dashboards"],
            scrape_configs=self._generate_scrape_configs,
        )
        self.tracing = TracingEndpointRequirer(self, protocols=["otlp_http"])
        self.charm_tracing_endpoint, _ = charm_tracing_config(self.tracing, None)

        # S3
        self.backup = MAASBackups(self, "s3-parameters")

        # Charm actions
        self.framework.observe(self.on.create_admin_action, self._on_create_admin_action)
        self.framework.observe(self.on.get_api_key_action, self._on_get_api_key_action)
        self.framework.observe(self.on.get_api_endpoint_action, self._on_get_api_endpoint_action)
        self.framework.observe(self.on.get_maas_secret_action, self._on_get_maas_secret_action)
        self.framework.observe(self.on.get_maas_status_action, self._on_get_maas_status_action)
        self.framework.observe(self.on.stop_maas_action, self._on_stop_maas_action)
        self.framework.observe(self.on.start_maas_action, self._on_start_maas_action)
        self.framework.observe(self.on.pre_upgrade_check_action, self._on_pre_upgrade_check_action)
        self.framework.observe(self.on.upgrade_action, self._on_upgrade_action)

        # Charm configuration
        self.framework.observe(self.on.config_changed, self._on_config_changed)

        # Rolling ops manager
        self.rolling_ops_manager = RollingOpsManager(
            charm=self,
            peer_relation_name=MAAS_ROLLING_OPS_RELATION,
            callback_targets={
                "initialize": self._on_rolling_maas_init,
                "upgrade": self._upgrade,
            },
        )

    def _upgrade_precondition_error(self) -> str | None:
        """Check whether this unit is safe to upgrade.

        Returns:
            str | None: a message explaining why the upgrade must not proceed, or
                None if it may go ahead
        """
        if (
            self.app.planned_units() > 1
            and MaasHelper.is_running()
            and MaasHelper.get_installed_channel() != MAAS_SNAP_CHANNEL
        ):
            return (
                "The MAAS snap is running while attempting to upgrade this unit to a new "
                "channel. This likely means MAAS has not been stopped on all units first, "
                "and running different minor versions of MAAS simultaneously is not "
                "supported. Run `juju run maas-region/<n> stop-maas` on every unit before "
                "upgrading to a new channel, or pass force=true to skip this check."
            )
        return None

    def _upgrade(self) -> None:
        """Upgrade the MAAS snap.

        Raises:
            Exception: if the snap upgrade fails
        """
        self.unit.status = ops.MaintenanceStatus("upgrading...")
        MaasHelper.upgrade(MAAS_SNAP_CHANNEL)
        if workload_version := self.version:
            self.unit.set_workload_version(workload_version)

    def _on_upgrade_action(self, event: ops.ActionEvent) -> None:
        """Handle the upgrade action.

        Args:
            event (ops.ActionEvent): Event from the framework
        """
        if not event.params.get("force", False) and (error := self._upgrade_precondition_error()):
            logger.warning("Refusing to upgrade MAAS: %s", error)
            event.fail(error)
            return

        try:
            self._upgrade()
        except Exception as ex:
            logger.exception("Failed to upgrade MAAS")
            event.fail(f"Upgrade failed: {ex}")
            return
        event.set_results(
            {
                "version": MaasHelper.get_installed_version(),
                "revision": MaasHelper.get_installed_revision(),
            }
        )

    def _on_pre_upgrade_check_action(self, event: ops.ActionEvent) -> None:
        target_channel = event.params.get("channel")
        if not target_channel:
            target_channel = MAAS_SNAP_CHANNEL

        installed_version = MaasHelper.get_installed_version()
        installed_revision = MaasHelper.get_installed_revision()
        installed_channel = MaasHelper.get_installed_channel()
        if not installed_version or not installed_revision or not installed_channel:
            event.fail("Could not obtain installed MAAS version, revision, or channel. Is MAAS installed?")
            return

        # Due to the installation with cohorts, and the fact that charm revisions are specific to an architecture,
        # the snap revision installed will be the same across all units in the application, therefore the report
        # for this unit is representative for all units in the cluster.
        results = {
            "installed-snap": f"{installed_version} (revision {installed_revision}) on channel {installed_channel}",
        }

        try:
            target_channel_info = MaasHelper.get_latest_channel_info(target_channel)
        except Exception:
            logger.exception("Failed to query the snap store for the latest MAAS version")
            event.set_results(results)
            event.fail(
                f"Failed to query the snap store for the latest MAAS version on channel {target_channel}, cannot determine if an upgrade is possible."
            )
            return
        if not target_channel_info:
            event.set_results(results)
            event.fail(f"No MAAS version found in the snap store for channel {target_channel}")
            return

        target_version = target_channel_info.get("version", "")
        target_revision = target_channel_info.get("revision", "")

        if target_revision == installed_revision:
            results["info"] = (
                f"Current installed revision ({installed_revision}) is the latest available on channel {target_channel}. No upgrade is needed."
            )
            event.set_results(results)
            return

        results["upgrade-target-snap"] = (
            f"{target_version} (revision {target_revision}) on channel {target_channel}"
        )
        if _version_tuple(target_version) < _version_tuple(installed_version):
            event.set_results(results)
            event.fail(
                f"The latest version ({target_version}) on channel {target_channel} is a downgrade compared to the installed version ({installed_version})."
                f" MAAS does not support downgrades. Please use a channel with a newer version.\n"
            )
            return

        if target_channel == installed_channel:
            # Point upgrade, no need for base compatibility check
            results["info"] = f"Point upgrade is possible from {installed_version} to {target_version}."
            event.set_results(results)
            return

        # A move between tracks may also require a base change
        if base_error := self._check_base_compatibility(target_channel, results):
            event.set_results(results)
            event.fail(base_error)
            return

        event.set_results(results)

    def _check_base_compatibility(self, target_channel: str, results: dict[str, Any]) -> str | None:
        """Record base compatibility for a target channel, in place, in `results`.

        Args:
            target_channel (str): the channel being checked, e.g. "3.9/edge"
            results (dict[str, Any]): action results to annotate

        Returns:
            str | None: a failure message if the target track needs a different base
        """
        host_base = MaasHelper.get_host_base()
        target_track = target_channel.split("/")[0]
        target_bases = MAAS_TRACK_BASES.get(target_track)

        # An unmapped track is reported as undetermined: a stale map must not block
        # an otherwise legitimate upgrade.
        if target_bases is None:
            results["info"] = (
                f"Track {target_track} is not known to this charm, so base compatibility "
                "could not be determined. Check the charm's supported bases on Charmhub "
                "to determine if the base is the same as the current host, and report "
                "this message to the charm maintainers."
            )
            return None

        results["host-base"] = host_base
        results["upgrade-target-charm-bases"] = ", ".join(target_bases)
        if host_base in target_bases:
            results["info"] = f"The current host base {host_base} is compatible with the {target_channel} charm's supported bases. Performing an upgrade inplace is possible."
            return None
        return (
            f"Channel {target_channel} requires an Ubuntu base of "
            f"{', '.join(target_bases)}, but this unit runs {host_base or 'unknown'}. "
            "Changing base requires redeploying units on the new base."
        )

    @property
    def is_tls_config_enabled(self) -> bool:
        """If MAAS is meant to run in TLS mode."""
        ssl_cfg_keys = ["ssl_cert_content", "ssl_key_content", "ssl_cacert_content"]
        for key in ssl_cfg_keys:
            if self.config[key]:
                return True
        return False

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
    def maas_cli_url(self) -> str:
        """Get MAAS CLI URL.

        Returns:
            str: The CLI URL
        """
        if maas_url := self.config["maas_url"]:
            return str(maas_url)

        scheme, port, relation_name = (
            ("https", MAAS_HTTPS_PORT, HAPROXY_TLS)
            if self.is_tls_config_enabled
            else ("http", MAAS_HTTP_PORT, HAPROXY_NON_TLS)
        )

        # TODO: Read the vip from HAProxy, if the relation exists, once
        # https://github.com/canonical/haproxy-operator/issues/365 or similar is implemented
        if relation := self.model.get_relation(relation_name):
            if endpoints := relation.data[relation.app].get("endpoints"):
                try:
                    endpoint_list = json.loads(endpoints)
                    if isinstance(endpoint_list, list) and endpoint_list:
                        return f"{scheme}://{endpoint_list[0]}/MAAS"
                except json.JSONDecodeError:
                    logger.warning(f"Invalid endpoints format from HAProxy: {endpoints}")
        return f"{scheme}://{self.bind_address}:{port}/MAAS"

    @property
    def maas_api_url(self) -> str:
        """Get MAAS API URL.

        Returns:
            str: The API URL
        """
        if maas_url := self.config["maas_url"]:
            parsed = urlparse(str(maas_url))
            # Force http scheme for internal API initialization
            if parsed.scheme == "https":
                parsed = parsed._replace(scheme="http")
            return urlunparse(parsed)

        # TODO: Read the vip from HAProxy, if the relation exists, once
        # https://github.com/canonical/haproxy-operator/issues/365 or similar is implemented
        if relation := self.model.get_relation(HAPROXY_NON_TLS):
            if endpoints := relation.data[relation.app].get("endpoints"):
                try:
                    endpoint_list = json.loads(endpoints)
                    if isinstance(endpoint_list, list) and endpoint_list:
                        return f"http://{endpoint_list[0]}/MAAS"
                except json.JSONDecodeError:
                    logger.warning(f"Invalid endpoints format from HAProxy: {endpoints}")
        return f"http://{self.bind_address}:{MAAS_HTTP_PORT}/MAAS"

    @property
    def maas_ips(self) -> list[IPvAnyAddress]:
        """Get the IP addresses of MAAS Regions in the cluster.

        Return:
            list[IPvAnyAddress]: The list of connected MAAS IPs
        """
        region_ips = {self.bind_address}
        if self.peers:
            region_ips.update(
                addr
                for unit in self.peers.units
                if isinstance(addr := self.get_peer_data(unit, "bind-address"), str)
            )
        return list(map(ip_address, region_ips))

    def get_operational_mode(self) -> str:
        """Get expected MAAS mode.

        Returns:
            str: either `region` of `region+rack`
        """
        has_agent = self.config["enable_rack_mode"]
        return "region+rack" if has_agent else "region"

    def get_required_ports(self) -> list[ops.Port]:
        """Get expected MAAS ports based on operational mode.

        Returns:
            list[ops.Port]
        """
        has_agent = self.config["enable_rack_mode"]
        return MAAS_REGION_RACK_PORTS if has_agent else MAAS_REGION_PORTS

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

    def _generate_scrape_configs(self) -> list[dict]:
        """Build Prometheus scrape_configs for the cos-agent relation.

        The scheme/port of some MAAS metrics endpoints depend on whether TLS is
        enabled, so they are generated dynamically. The COSAgentProvider invokes
        this callable on each of its refresh events, which by default include this
        (maas-region) charm's ``config_changed`` plus cos-agent relation changes.
        So toggling the ``ssl_*`` config options regenerates the scrape jobs.

        Returns:
            list[dict]: standard Prometheus scrape_config dicts
        """
        # The region metrics endpoint is plain http and identical in both modes.
        scrape_configs: list[dict] = [
            {
                "metrics_path": "/metrics",
                "static_configs": [{"targets": [f"localhost:{MAAS_REGION_METRICS_PORT}"]}],
            },
        ]

        # /MAAS/metrics and /metrics/temporal move from http:5240 to https:5443
        # when TLS is enabled.
        if self.is_tls_config_enabled:
            # We include insecure_skip_verify because we are always scraping localhost.
            # Even if we have the certs for the scrape targets, we'd rather specify the scrape
            # jobs with localhost rather than the SAN (region/HAProxy IP) the cert was issued for.
            tls_config = {"insecure_skip_verify": True}
            scrape_configs.append(
                {
                    "scheme": "https",
                    "metrics_path": "/MAAS/metrics",
                    "static_configs": [{"targets": [f"localhost:{MAAS_HTTPS_PORT}"]}],
                    "tls_config": tls_config,
                }
            )
            scrape_configs.append(
                {
                    "scheme": "https",
                    "metrics_path": "/metrics/temporal",
                    "static_configs": [{"targets": [f"localhost:{MAAS_HTTPS_PORT}"]}],
                    "tls_config": tls_config,
                }
            )
        else:
            scrape_configs.append(
                {
                    "metrics_path": "/MAAS/metrics",
                    "static_configs": [{"targets": [f"localhost:{MAAS_CLUSTER_METRICS_PORT}"]}],
                }
            )
            scrape_configs.append(
                {
                    "metrics_path": "/metrics/temporal",
                    "static_configs": [{"targets": [f"localhost:{MAAS_HTTP_PORT}"]}],
                }
            )

        # Agent metrics are plain http and only present in rack mode.
        if self.config["enable_rack_mode"]:
            scrape_configs.append(
                {
                    "metrics_path": MAAS_AGENT_METRICS_ENDPOINT,
                    "static_configs": [{"targets": [f"localhost:{MAAS_AGENT_METRICS_PORT}"]}],
                }
            )

        return scrape_configs

    def _setup_network(self) -> bool:
        """Open the network ports.

        Returns:
            bool: True if successful
        """
        try:
            self.unit.set_ports(*self.get_required_ports())
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
            self._setup_network()
            MaasHelper.stop()
            MaasHelper.setup_region(
                self.maas_api_url,
                self.connection_string,
                self.get_operational_mode(),
            )
            # check maas_cli_url existence in case MAAS isn't ready yet
            if self.maas_cli_url and self.unit.is_leader():
                self._update_tls_config()
                credentials = self._create_or_get_internal_admin()
                MaasHelper.set_prometheus_metrics(
                    credentials["username"],
                    self.maas_cli_url,
                    self.config["enable_prometheus_metrics"],  # type: ignore
                    str(self.config["ssl_cacert_content"]),
                )
            return True
        except subprocess.CalledProcessError:
            return False

    def _on_rolling_maas_init(self) -> OperationResult:
        """Run MAAS initialization.

        Required for RollingOpsManager, which expects a callback that
        takes a CharmBase object and EventBase object as arguments.
        """
        success = self._initialize_maas()
        return OperationResult.RELEASE if success else OperationResult.RETRY_RELEASE

    def get_region_system_ids(self) -> set[str]:
        """Get the system IDs of all regions in the MAAS cluster.

        Returns:
            set[str]: set of system IDs

        Raises:
            CalledProcessError: failed to get the regions
        """
        credentials = self._create_or_get_internal_admin()
        return MaasHelper.get_regions(
            admin_username=credentials["username"],
            maas_url=self.maas_cli_url,
            cacert=str(self.config["ssl_cacert_content"]),
        )

    def _reconcile_ha_proxy(self, event: ops.EventBase) -> None:
        """Configure the two HAProxy relations.

        Provides the MAAS Region IP addresses to each HAProxy relation.
        Status setting is left to `_on_collect_status`, which evaluates the
        relation/configuration topology.

        Returns:
            None
        """
        haproxy_non_tls_enabled = self.model.get_relation(HAPROXY_NON_TLS) is not None
        haproxy_tls_enabled = self.model.get_relation(HAPROXY_TLS) is not None

        haproxy_temporal_route_enabled = self.model.get_relation(HAPROXY_TEMPORAL) is not None
        haproxy_internal_http_api_route_enabled = (
            self.model.get_relation(HAPROXY_INTERNAL_HTTP_API) is not None
        )

        # Check if all required HAProxy relations are present (TLS is optional)
        has_required_haproxy_relations = (
            haproxy_non_tls_enabled
            and haproxy_temporal_route_enabled
            and haproxy_internal_http_api_route_enabled
        )
        has_any_haproxy_relation = (
            haproxy_non_tls_enabled
            or haproxy_tls_enabled
            or haproxy_temporal_route_enabled
            or haproxy_internal_http_api_route_enabled
        )
        # Valid scenarios:
        # 1. No HAProxy relations at all (standalone MAAS)
        # 2. All required HAProxy relations present without TLS (MAAS also without TLS config)
        # 3. All required HAProxy relations present with TLS (MAAS also with TLS config)
        unit_valid = not has_any_haproxy_relation or (
            has_required_haproxy_relations and self.is_tls_config_enabled == haproxy_tls_enabled
        )
        logger.info(
            f"Reconciling HAProxy with haproxy_non_tls_enabled: {haproxy_non_tls_enabled}"
            f", haproxy_tls_enabled: {haproxy_tls_enabled}"
            f", maas_tls_enabled: {self.is_tls_config_enabled}"
            f", and computed validity as: {unit_valid}"
        )

        if not self.unit.is_leader():
            return

        haproxy_relations = [
            (haproxy_non_tls_enabled, self.haproxy_non_tls_route),
            (haproxy_temporal_route_enabled, self.haproxy_temporal_route),
            (haproxy_internal_http_api_route_enabled, self.haproxy_internal_http_api_route),
            (haproxy_tls_enabled, self.haproxy_tls_route),
        ]
        for enabled, rel in haproxy_relations:
            if enabled:
                if unit_valid:
                    rel.configure_hosts(self.maas_ips)
                else:
                    rel.configure_hosts()
                rel.update_relation_data()

    def _reconcile_ha_proxy_and_initialise(self, event: ops.EventBase) -> None:
        self._reconcile_ha_proxy(event)
        if self.connection_string and (
            MaasHelper.get_maas_details().get("maas_url") != self.maas_api_url
        ):
            self._initialize_maas()

    def _update_tls_config(self) -> None:
        """Enable or disable TLS in MAAS."""
        if (tls_enabled := MaasHelper.is_tls_enabled()) is not None:
            if not tls_enabled and self.is_tls_config_enabled:
                MaasHelper.create_tls_files(
                    self.config["ssl_cert_content"],  # type: ignore
                    self.config["ssl_key_content"],  # type: ignore
                    self.config["ssl_cacert_content"],  # type: ignore
                )
                MaasHelper.enable_tls()
                MaasHelper.delete_tls_files()
            elif tls_enabled and not self.is_tls_config_enabled:
                MaasHelper.disable_tls()

    def _update_prometheus_config(self, enable: bool) -> None:
        if not MaasHelper.is_maas_initialized():
            logger.warning("MAAS Not ready for Prometheus config yet")
            return

        if secret_uri := self.get_peer_data(self.app, MAAS_ADMIN_SECRET_KEY):
            secret = self.model.get_secret(id=secret_uri)
            username = secret.get_content()["username"]
            MaasHelper.set_prometheus_metrics(
                username, self.maas_cli_url, enable, str(self.config["ssl_cacert_content"])
            )

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
        if not MaasHelper.get_present():
            e.add_status(ops.BlockedStatus("Failed to install MAAS snap"))
        elif (
            # If the S3 configuration is marked as blocked in the application data bag,
            # mark the leader as blocked.
            blocked_msg := self.get_peer_data(self.app, S3_CONFIGURATION_BLOCKED_KEY)
        ) and self.unit.is_leader():
            e.add_status(ops.BlockedStatus(blocked_msg))
        elif MaasHelper.get_installed_channel() != MAAS_SNAP_CHANNEL:
            e.add_status(ops.BlockedStatus("MAAS snap channel does not match the charm channel"))
        elif not self.unit.opened_ports().issuperset(MAAS_REGION_PORTS):
            e.add_status(ops.WaitingStatus("Waiting for service ports"))
        elif not self.connection_string:
            e.add_status(ops.WaitingStatus("Waiting for database DSN"))
        elif not self.maas_api_url:
            e.add_status(ops.WaitingStatus("Waiting for MAAS initialization"))
        elif not MaasHelper.is_running():
            e.add_status(ops.BlockedStatus("The MAAS snap service is not active"))
        else:
            # Check HAProxy configuration validity
            haproxy_non_tls = self.model.get_relation(HAPROXY_NON_TLS) is not None
            haproxy_tls = self.model.get_relation(HAPROXY_TLS) is not None
            haproxy_temporal = self.model.get_relation(HAPROXY_TEMPORAL) is not None
            haproxy_internal_http_api = (
                self.model.get_relation(HAPROXY_INTERNAL_HTTP_API) is not None
            )

            has_required_relations = (
                haproxy_non_tls and haproxy_temporal and haproxy_internal_http_api
            )
            has_any_haproxy_relation = (
                haproxy_non_tls or haproxy_tls or haproxy_temporal or haproxy_internal_http_api
            )

            # Invalid: HAProxy TLS relation present but MAAS TLS not enabled
            if not self.is_tls_config_enabled and haproxy_tls:
                e.add_status(
                    ops.BlockedStatus(
                        "Invalid HAProxy configuration: "
                        f"Cannot have `{HAPROXY_TLS}` relation when MAAS TLS is not enabled; "
                        "Set the `ssl_cert_content` and `ssl_key_content` configuration options."
                    )
                )
            # Invalid: MAAS TLS enabled with required relations but missing HAProxy TLS
            elif self.is_tls_config_enabled and has_required_relations and not haproxy_tls:
                e.add_status(
                    ops.BlockedStatus(
                        f"Invalid HAProxy configuration: Missing `{HAPROXY_TLS}` relation "
                        "when MAAS TLS is enabled."
                    )
                )
            # Invalid: HAProxy TLS relation present without all required base relations
            elif haproxy_tls and not has_required_relations:
                e.add_status(
                    ops.BlockedStatus(
                        "Invalid HAProxy configuration: "
                        f"`{HAPROXY_TLS}` relation requires all base relations: "
                        f"`{HAPROXY_NON_TLS}`, `{HAPROXY_TEMPORAL}`, and `{HAPROXY_INTERNAL_HTTP_API}`."
                    )
                )
            # Invalid: Partial HAProxy relations (not all required together)
            elif has_any_haproxy_relation and not has_required_relations:
                e.add_status(
                    ops.BlockedStatus(
                        "Invalid HAProxy configuration: "
                        f"All of `{HAPROXY_NON_TLS}`, `{HAPROXY_TEMPORAL}`, and `{HAPROXY_INTERNAL_HTTP_API}` "
                        "relations must be present together if any are provided."
                    )
                )
            else:
                e.add_status(ops.ActiveStatus())

    def _on_maasdb_created(self, event: db.DatabaseCreatedEvent) -> None:
        """Database is ready.

        Args:
            event (DatabaseCreatedEvent): event from DatabaseRequires
        """
        logger.info(f"MAAS database credentials received for user '{event.username}'")
        if self.connection_string:
            self.unit.status = ops.MaintenanceStatus("Initializing the MAAS database")
            self.rolling_ops_manager.request_async_lock(
                callback_id="initialize", max_retry=1
            )

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

    def _on_maas_peer_changed(self, event: ops.RelationEvent) -> None:
        logger.info(event)
        self.set_peer_data(self.unit, "system-name", socket.gethostname())
        self.set_peer_data(self.unit, "bind-address", self.bind_address)
        self._reconcile_ha_proxy_and_initialise(event)

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

    def _on_get_api_endpoint_action(self, event: ops.ActionEvent):
        """Handle the get-api-endpoint action."""
        if url := self.maas_cli_url:
            event.set_results({"api-url": url})
        else:
            event.fail("MAAS is not initialized yet")

    def _on_get_maas_secret_action(self, event: ops.ActionEvent):
        """Handle the get-maas-secret action."""
        if secret := MaasHelper.get_maas_secret():
            event.set_results({"maas-secret": secret})
        else:
            event.fail("MAAS is not initialized yet")

    def _on_get_maas_status_action(self, event: ops.ActionEvent):
        """Handle the get-maas-status action."""
        if status := MaasHelper.get_maas_status():
            event.set_results({"services": status})
        else:
            event.fail("MAAS is not initialized yet or failed to retrieve status")

    def _on_stop_maas_action(self, event: ops.ActionEvent):
        """Handle the stop-maas action.

        This is intended to be called by the user as part of the MAAS upgrade process.
        When the snap is stopped, other charm actions may behave unexpectedly.
        """
        try:
            MaasHelper.set_running(False)
            event.set_results({"status": "stopped"})
        except SnapError as e:
            event.fail(f"Failed to stop MAAS: {e}")

    def _on_start_maas_action(self, event: ops.ActionEvent):
        """Handle the start-maas action."""
        try:
            MaasHelper.set_running(True)
            event.set_results({"status": "started"})
        except SnapError as e:
            event.fail(f"Failed to start MAAS: {e}")

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        # validate TLS certificate and key
        if self.is_tls_config_enabled:
            cert = self.config["ssl_cert_content"]
            key = self.config["ssl_key_content"]
            if not cert or not key:
                raise ValueError(
                    "Both ssl_cert_content and ssl_key_content must be defined when using configuring TLS"
                )

        # validate maas_url if provided
        if maas_url := self.config["maas_url"]:
            parsed = urlparse(str(maas_url))
            if not parsed.scheme or not parsed.netloc:
                raise ValueError(
                    f"Invalid maas_url: {maas_url}. Must be a valid URL with scheme and host."
                )
        self._reconcile_ha_proxy(event)
        maas_details = MaasHelper.get_maas_details()
        # the MAAS initialization details have changed
        init_details = {
            "API URL": maas_details.get("maas_url") != self.maas_api_url,
            f"Mode ({self.get_operational_mode()})": MaasHelper.get_maas_mode()
            != self.get_operational_mode(),
        }
        if self.connection_string and any(init_details.values()):
            changes = [k for k, v in init_details.items() if v]
            self.unit.status = ops.MaintenanceStatus(
                f"re-initializing MAAS with new: {', '.join(changes)}..."
            )
            self._initialize_maas()

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
