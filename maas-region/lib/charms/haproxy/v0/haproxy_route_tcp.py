# pylint: disable=too-many-lines,duplicate-code
"""Haproxy-route interface library.

## Getting Started

To get started using the library, you just need to fetch the library using `charmcraft`.

```shell
cd some-charm
charmcraft fetch-lib charms.haproxy.v1.haproxy_route_tcp
```

In the `metadata.yaml` of the charm, add the following:

```yaml
requires:
    backend-tcp:
        interface: haproxy-route-tcp
        limit: 1
```

Then, to initialise the library:

```python
from charms.haproxy.v0.haproxy_route_tcp import HaproxyRouteTcpRequirer

class SomeCharm(CharmBase):
  def __init__(self, *args):
    # ...

    # There are 2 ways you can use the requirer implementation:
    # 1. To initialize the requirer with parameters:
    self.haproxy_route_tcp_requirer = HaproxyRouteTcpRequirer(
        self,
        relation_name="haproxy-route-tcp"
        port=<optional>  # The port exposed on the provider.
        backend_port=<optional>  # The port where the backend service is listening.
        hosts=<optional>  # List of backend server addresses. Currently only support IP addresses.
        sni=<optional>  # Server name identification. Used to route traffic to the service.
        check_interval=<optional>  # Interval between health checks in seconds.
        check_rise=<optional>  # Number of successful health checks
            before server is considered up.
        check_fall=<optional>  # Number of failed health checks before server is considered down.
        check_type=<optional>  # Can be 'generic', 'mysql', 'postgres', 'redis' or 'smtp'ßß.
        check_send=<optional>  # Only used in generic health checks,
            specify a string to send in the health check request.
        check_expect=<optional>  # Only used in generic health checks,
            specify the expected response from a health check request.
        check_db_user=<optional>  # Only used if type is postgres or mysql,
            specify the user name to enable HAproxy to send a Client Authentication packet.
        load_balancing_algorithm=<optional>  # Algorithm to use for load balancing.
        load_balancing_consistent_hashing=<optional>  # Whether to use consistent hashing.
        rate_limit_connections_per_minute=<optional>  # Maximum connections allowed per minute.
        rate_limit_policy=<optional>  # Policy to apply when rate limit is reached.
        upload_limit=<optional>  # Maximum upload bandwidth in bytes per second.
        download_limit=<optional>  # Maximum download bandwidth in bytes per second.
        retry_count=<optional>  # Number of times to retry failed requests.
        retry_redispatch=<optional>  # Whether to redispatch failed requests to another server.
        server_timeout=<optional>  # Timeout for requests from haproxy
            to backend servers in seconds.
        connect_timeout=<optional>  # Timeout for client requests to haproxy in seconds.
        queue_timeout=<optional>  # Timeout for requests waiting in queue in seconds.
        server_maxconn=<optional>  # Maximum connections per server.
        ip_deny_list=<optional>  # List of source IP addresses to block.
        enforce_tls=<optional>  # Whether to enforce TLS for all traffic coming to the backend.
        tls_terminate=<optional>  # Whether to enable tls termination on the dedicated frontend.
        unit_address=<optional>  # IP address of the unit
            (if not provided, will use binding address).
    )

    # 2.To initialize the requirer with no parameters, i.e
    # self.haproxy_route_tcp_requirer = HaproxyRouteTcpRequirer(self)
    # This will simply initialize the requirer class and it won't perfom any action.

    # Afterwards regardless of how you initialized the requirer you can call the
    # provide_haproxy_route_requirements method anywhere in your charm to update the requirer data.
    # The method takes the same number of parameters as the requirer class.
    # provide_haproxy_route_tcp_requirements(port=, ...)

    self.framework.observe(
        self.framework.on.config_changed, self._on_config_changed
    )
    self.framework.observe(
        self.haproxy_route_tcp_requirer.on.ready, self._on_endpoints_ready
    )
    self.framework.observe(
        self.haproxy_route_tcp_requirer.on.removed, self._on_endpoints_removed
    )

    def _on_config_changed(self, event: ConfigChangedEvent) -> None:
        self.haproxy_route_tcp_requirer.provide_haproxy_route_tcp_requirements(...)

    def _on_endpoints_ready(self, _: EventBase) -> None:
        # Handle endpoints ready event
        if endpoints := self.haproxy_route_tcp_requirer.get_proxied_endpoints():
            # Do something with the endpoints information
        ...

    def _on_endpoints_removed(self, _: EventBase) -> None:
        # Handle endpoints removed event
        ...

    # 3.To initialize the requirer together with helper methods.
    # This will use chaining of the helper methods to populate the requirer
    # data attributes.
    self.haproxy_tcp_route_requirer = HaproxyRouteTcpRequirer(self, relation_name="") \
        .configure_port(4000) \
        .configure_backend_port(5000) \
        .configure_health_check(60, 5, 5) \
        .configure_rate_limit(10, TCPRateLimitPolicy.SILENT) \
        .update_relation_data()


## Using the library as the provider
The provider charm should expose the interface as shown below:
```yaml
provides:
    haproxy-route-tcp:
        interface: haproxy-route-tcp
```
Note that this interface supports relating to multiple endpoints.

Then, to initialise the library:
```python
from charms.haproxy.v0.haproxy_route import HaproxyRouteTcpProvider

class SomeCharm(CharmBase):
    self.haproxy_route_tcp_provider = HaproxyRouteTcpProvider(self)
    self.framework.observe(
        self.haproxy_route_tcp_provider.on.data_available, self._on_haproxy_route_data_available
    )

    def _on_haproxy_route_data_available(self, event: EventBase) -> None:
        data = self.haproxy_route_tcp_provider.get_data(self.haproxy_route_tcp_provider.relations)
        # data is an object of the `HaproxyRouteTcpRequirersData` class, see below for the
        # available attributes
        ...

        # Publish the endpoints to the requirers
        for requirer_data in data.requirers_data:
            self.haproxy_route_tcp.publish_proxied_endpoints(
                ["..."], requirer_data.relation_id
            )
"""

import json
import logging
from collections import defaultdict
from enum import Enum
from typing import Annotated, Any, MutableMapping, Optional, cast

from ops import CharmBase, ModelError, RelationBrokenEvent
from ops.charm import CharmEvents
from ops.framework import EventBase, EventSource, Object
from ops.model import Relation
from pydantic import (
    AnyUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    IPvAnyAddress,
    ValidationError,
    model_validator,
)
from pydantic.dataclasses import dataclass
from typing_extensions import Self

# The unique Charmhub library identifier, never change it
LIBID = "b1b5c0a6f1b5481c9923efa042846681"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 3

logger = logging.getLogger(__name__)
HAPROXY_ROUTE_TCP_RELATION_NAME = "haproxy-route-tcp"
HAPROXY_CONFIG_INVALID_CHARACTERS = "\n\t#\\'\"\r$ "


def value_contains_invalid_characters(value: Optional[str]) -> Optional[str]:
    """Validate if value contains invalid haproxy config characters.

    Args:
        value: The value to validate.

    Raises:
        ValueError: When value contains invalid characters.

    Returns:
        The validated value.
    """
    if value is None:
        return value

    if [char for char in value if char in HAPROXY_CONFIG_INVALID_CHARACTERS]:
        raise ValueError(f"Relation data contains invalid character(s) {value}")
    return value


VALIDSTR = Annotated[str, BeforeValidator(value_contains_invalid_characters)]


class DataValidationError(Exception):
    """Raised when data validation fails."""


class HaproxyRouteTcpInvalidRelationDataError(Exception):
    """Raised when data validation of the haproxy-route relation fails."""


class _DatabagModel(BaseModel):
    """Base databag model.

    Attrs:
        model_config: pydantic model configuration.
    """

    model_config = ConfigDict(
        # tolerate additional keys in databag
        extra="ignore",
        # Allow instantiating this class by field name (instead of forcing alias).
        populate_by_name=True,
        # Custom config key: whether to nest the whole datastructure (as json)
        # under a field or spread it out at the toplevel.
        _NEST_UNDER=None,
    )  # type: ignore
    """Pydantic config."""

    @classmethod
    def load(cls, databag: MutableMapping) -> "_DatabagModel":
        """Load this model from a Juju json databag.

        Args:
            databag: Databag content.

        Raises:
            DataValidationError: When model validation failed.

        Returns:
            _DatabagModel: The validated model.
        """
        nest_under = cls.model_config.get("_NEST_UNDER")
        if nest_under:
            return cls.model_validate(json.loads(databag[nest_under]))

        try:
            data = {
                k: json.loads(v)
                for k, v in databag.items()
                # Don't attempt to parse model-external values
                if k in {(f.alias or n) for n, f in cls.model_fields.items()}
            }
        except json.JSONDecodeError as e:
            msg = f"invalid databag contents: expecting json. {databag}"
            logger.error(msg)
            raise DataValidationError(msg) from e

        try:
            return cls.model_validate_json(json.dumps(data))
        except ValidationError as e:
            msg = f"failed to validate databag: {databag}"
            logger.error(str(e), exc_info=True)
            raise DataValidationError(msg) from e

    @classmethod
    def from_dict(cls, values: dict) -> "_DatabagModel":
        """Load this model from a dict.

        Args:
            values: Dict values.

        Raises:
            DataValidationError: When model validation failed.

        Returns:
            _DatabagModel: The validated model.
        """
        try:
            logger.info("Loading values from dictionary: %s", values)
            return cls.model_validate(values)
        except ValidationError as e:
            msg = f"failed to validate: {values}"
            logger.debug(msg, exc_info=True)
            raise DataValidationError(msg) from e

    def dump(
        self, databag: Optional[MutableMapping] = None, clear: bool = True
    ) -> Optional[MutableMapping]:
        """Write the contents of this model to Juju databag.

        Args:
            databag: The databag to write to.
            clear: Whether to clear the databag before writing.

        Returns:
            MutableMapping: The databag.
        """
        if clear and databag:
            databag.clear()

        if databag is None:
            databag = {}
        nest_under = self.model_config.get("_NEST_UNDER")
        if nest_under:
            databag[nest_under] = self.model_dump_json(
                by_alias=True,
                # skip keys whose values are default
                exclude_defaults=True,
            )
            return databag

        dct = self.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        databag.update({k: json.dumps(v) for k, v in dct.items()})
        return databag


class TCPHealthCheckType(Enum):
    """Enum of possible rate limiting policies.

    Attrs:
        GENERIC: deny a client's HTTP request to return a 403 Forbidden error.
        MYSQL: closes the connection immediately without sending a response.
        POSTGRES: disconnects immediately without notifying the client
            that the connection has been closed.
        REDIS: closes the connection immediately without sending a response.
        SMTP: closes the connection immediately without sending a response.
    """

    GENERIC = "generic"
    MYSQL = "mysql"
    POSTGRES = "postgres"
    REDIS = "redis"
    SMTP = "smtp"


class TCPServerHealthCheck(BaseModel):
    """Configuration model for backend server health checks.

    Attributes:
        interval: Number of seconds between consecutive health check attempts.
        rise: Number of consecutive successful health checks required for up.
        fall: Number of consecutive failed health checks required for DOWN.
        check_type: Health check type, Can be “generic”, “mysql”, “postgres”, “redis” or “smtp”.
        send: Only used in generic health checks,
            specify a string to send in the health check request.
        expect: Only used in generic health checks,
            specify the expected response from a health check request.
        db_user: Only used if type is postgres or mysql,
            specify the user name to enable HAproxy to send a Client Authentication packet.
    """

    # interval, rise and fall don't have a default value since the class itself is optional
    # in the requirer databag model, so once the class is instantiated we need all of the
    # required attributes to be present as we can assume that health-check is being configured.
    interval: int = Field(
        description="The interval (in seconds) between health checks.",
        gt=0,
    )
    rise: int = Field(
        description="How many successful health checks before server is considered up.",
        gt=0,
    )
    fall: int = Field(
        description="How many failed health checks before server is considered down.", gt=0
    )
    check_type: Optional[TCPHealthCheckType] = Field(
        description=(
            "The health check type, can be 'generic', 'mysql', 'postgres', 'redis' or 'smtp'"
        ),
        default=None,
    )
    # send and expect does not have VALIDSTR validation because we need the flexibilty to
    # specify anything in the health-check TCP requests. They will need to be properly
    # sanitized / validated in the provider charm.
    send: Optional[str] = Field(
        description=(
            "Only used in generic health checks, "
            "specify a string to send in the health check request."
        ),
        default=None,
    )
    expect: Optional[str] = Field(
        description=(
            "Only used in generic health checks, "
            "specify the expected response from a health check request."
        ),
        default=None,
    )
    db_user: Optional[VALIDSTR] = Field(
        description=(
            "Only used if type is postgres or mysql, "
            "specify the user name to enable HAproxy to send a Client Authentication packet."
        ),
        default=None,
    )

    @model_validator(mode="after")
    def check_all_required_fields_set(self) -> Self:
        """Check that all required fields for health check are set.

        Raises:
            ValueError: When validation fails.

        Returns:
            The validated model.
        """
        if (
            self.send is not None or self.expect is not None
        ) and self.check_type != TCPHealthCheckType.GENERIC:
            raise ValueError("send and expect can only be set if type is 'generic'")
        if self.db_user is not None and self.check_type not in [
            TCPHealthCheckType.MYSQL,
            TCPHealthCheckType.POSTGRES,
        ]:
            raise ValueError("db_user can only be set if type is postgres or mysql")
        return self


# tarpit is not yet implemented
class TCPRateLimitPolicy(Enum):
    """Enum of possible rate limiting policies.

    Attrs:
        REJECT: Send a TCP reset packet to close the connection.
        SILENT: disconnects immediately without notifying the client
            that the connection has been closed (no packet sent).
    """

    REJECT = "reject"
    SILENT = "silent-drop"


class RateLimit(BaseModel):
    """Configuration model for connection rate limiting.

    Attributes:
        connections_per_minute: Number of connections allowed per minute for a client.
        policy: Action to take when the rate limit is exceeded.
    """

    connections_per_minute: int = Field(description="How many connections are allowed per minute.")
    policy: TCPRateLimitPolicy = Field(
        description="Configure the rate limit policy.", default=TCPRateLimitPolicy.REJECT
    )


class LoadBalancingAlgorithm(Enum):
    """Enum of possible http_route types.

    Attrs:
        LEASTCONN: The server with the lowest number of connections receives the connection.
        SRCIP: Load balance using the hash of The source IP address.
        ROUNDROBIN: Each server is used in turns, according to their weights.
    """

    LEASTCONN = "leastconn"
    SRCIP = "source"
    ROUNDROBIN = "roundrobin"


class TCPLoadBalancingConfiguration(BaseModel):
    """Configuration model for load balancing.

    Attributes:
        algorithm: Algorithm to use for load balancing.
        consistent_hashing: Use consistent hashing to avoid redirection
            when servers are added/removed.
    """

    algorithm: LoadBalancingAlgorithm = Field(
        description="Configure the load balancing algorithm for the service.",
    )
    # Note: Later when the generic LoadBalancingAlgorithm.HASH is implemented this attribute
    # will also apply under that mode.
    consistent_hashing: bool = Field(
        description=(
            "Only used when the `algorithm` is SRCIP. "
            "Use consistent hashing to avoid redirection when servers are added/removed. "
            "Default is False as it usually does not give a balanced distribution."
        ),
        default=False,
    )

    @model_validator(mode="after")
    def validate_attributes(self) -> Self:
        """Check that algorithm-specific configs are only set with their respective algorithm.

        Raises:
            ValueError: When validation fails in one of these cases:
                1. self.cookie is not None when self.algorithm != COOKIE
                2. self.consistent_hashing is True when algorithm is neither COOKIE nor SRCIP

        Returns:
            The validated model.
        """
        if self.consistent_hashing and self.algorithm != LoadBalancingAlgorithm.SRCIP:
            raise ValueError("Consistent hashing only applies when algorithm is COOKIE or SRCIP.")
        return self


class BandwidthLimit(BaseModel):
    """Configuration model for bandwidth rate limiting.

    Attributes:
        upload: Limit upload speed (bytes per second).
        download: Limit download speed (bytes per second).
    """

    upload: Optional[int] = Field(description="Upload limit (bytes per seconds).", default=None)
    download: Optional[int] = Field(
        description="Download limit (bytes per seconds).", default=None
    )


# retry-on is not yet implemented
class Retry(BaseModel):
    """Configuration model for retry.

    Attributes:
        count: How many times should a request retry.
        redispatch: Whether to redispatch failed requests to another server.
    """

    count: int = Field(description="How many times should a request retry.")
    redispatch: bool = Field(
        description="Whether to redispatch failed requests to another server.", default=False
    )


class TimeoutConfiguration(BaseModel):
    """Configuration model for timeout.

    Attributes:
        server: Timeout for requests from haproxy to backend servers.
        connect: Timeout for client requests to haproxy.
        queue: Timeout for requests waiting in the queue after server-maxconn is reached.
    """

    server: Optional[int] = Field(
        description="Timeout (in seconds) for requests from haproxy to backend servers.",
    )
    connect: Optional[int] = Field(
        description="Timeout (in seconds) for client requests to haproxy.",
    )
    queue: Optional[int] = Field(
        description="Timeout (in seconds) for requests in the queue.",
    )


class TcpRequirerApplicationData(_DatabagModel):
    """Configuration model for HAProxy route requirer application data.

    Attributes:
        port: The port exposed on the provider.
        backend_port: The port where the backend service is listening. Defaults to the
            provider port.
        hosts: List of backend server addresses. Currently only support IP addresses.
        sni: Server name identification. Used to route traffic to the service.
        check: TCP health check configuration
        load_balancing: Load balancing configuration.
        rate_limit: Rate limit configuration.
        bandwidth_limit: Bandwith limit configuration.
        retry: Retry configuration.
        timeout: Timeout configuration.
        server_maxconn: Maximum connections per server.
        ip_deny_list: List of source IP addresses to block.
        enforce_tls: Whether to enforce TLS for all traffic coming to the backend.
        tls_terminate: Whether to enable tls termination on the dedicated frontend.
    """

    port: int = Field(description="The port exposed on the provider.", gt=0, le=65535)
    backend_port: Optional[int] = Field(
        description=(
            "The port where the backend service is listening. Defaults to the provider port."
        ),
        default=None,
        gt=0,
        le=65525,
    )
    sni: Optional[VALIDSTR] = Field(
        description=(
            "Server name identification. Used to route traffic to the service. "
            "Only available if TLS is enabled."
        ),
        default=None,
    )
    hosts: list[IPvAnyAddress] = Field(
        description="The list of backend server addresses. Currently only support IP addresses.",
        default=[],
    )
    check: Optional[TCPServerHealthCheck] = Field(
        description="Configure health check for the service.",
        default=None,
    )
    load_balancing: Optional[TCPLoadBalancingConfiguration] = Field(
        description="Configure loadbalancing.", default=None
    )
    rate_limit: Optional[RateLimit] = Field(
        description="Configure rate limit for the service.", default=None
    )
    bandwidth_limit: Optional[BandwidthLimit] = Field(
        description="Configure bandwidth limit for the service.", default=None
    )
    retry: Optional[Retry] = Field(
        description="Configure retry for incoming requests.", default=None
    )
    timeout: Optional[TimeoutConfiguration] = Field(
        description="Configure timeout",
        default=None,
    )
    server_maxconn: Optional[int] = Field(
        description="Configure maximum connection per server", default=None
    )
    ip_deny_list: list[IPvAnyAddress] = Field(
        description="List of IP addresses to block.", default=[]
    )
    enforce_tls: bool = Field(description="Whether to enforce TLS for all traffic.", default=True)
    tls_terminate: bool = Field(description="Whether to enable tls termination.", default=True)

    @model_validator(mode="after")
    def assign_default_backend_port(self) -> "Self":
        """Assign a default value to backend_port if not set.

        The value is equal to the provider port.

        Returns:
            The model with backend_port default value applied.
        """
        if self.backend_port is None:
            self.backend_port = self.port
        return self

    @model_validator(mode="after")
    def sni_set_when_not_enforcing_tls(self) -> "Self":
        """Check if sni is configured but TLS is disabled.

        Raises:
            ValueError: If sni is configured and TLS is disabled.

        Returns:
            The validated model.
        """
        if not self.enforce_tls and self.sni is not None:
            raise ValueError("You can't set SNI and disable TLS at the same time.")
        return self


class HaproxyRouteTcpProviderAppData(_DatabagModel):
    """haproxy-route provider databag schema.

    Attributes:
        endpoints: The list of proxied endpoints that maps to the backend.
    """

    endpoints: list[AnyUrl]


class TcpRequirerUnitData(_DatabagModel):
    """haproxy-route requirer unit data.

    Attributes:
        address: IP address of the unit.
    """

    address: IPvAnyAddress = Field(description="IP address of the unit.")


@dataclass
class HaproxyRouteTcpRequirerData:
    """haproxy-route requirer data.

    Attributes:
        relation_id: Id of the relation.
        application: Name of the requirer application.
        application_data: Application data.
        units_data: Units data
    """

    relation_id: int
    application: str
    application_data: TcpRequirerApplicationData
    units_data: list[TcpRequirerUnitData]


@dataclass
class HaproxyRouteTcpRequirersData:
    """haproxy-route requirers data.

    Attributes:
        requirers_data: List of requirer data.
        relation_ids_with_invalid_data: Set of relation ids that contains invalid data.
    """

    requirers_data: list[HaproxyRouteTcpRequirerData]
    relation_ids_with_invalid_data: set[int]

    @model_validator(mode="after")
    def check_ports_unique(self) -> Self:
        """Check that requirers define unique ports.

        Returns:
            The validated model, with invalid relation IDs updated in
                `self.relation_ids_with_invalid_data`
        """
        # Maybe the logic here can be optimized, we want to keep track of
        # the relation IDs that request overlapping ports to ignore them during
        # rendering of the haproxy configuration.
        relation_ids_per_port: dict[int, list[int]] = defaultdict(list[int])
        for requirer_data in self.requirers_data:
            relation_ids_per_port[requirer_data.application_data.port].append(
                requirer_data.relation_id
            )

        for relation_ids in relation_ids_per_port.values():
            if len(relation_ids) > 1:
                self.relation_ids_with_invalid_data.update(relation_ids)
        return self


class HaproxyRouteTcpDataAvailableEvent(EventBase):
    """HaproxyRouteDataAvailableEvent custom event.

    This event indicates that the requirers data are available.
    """


class HaproxyRouteTcpDataRemovedEvent(EventBase):
    """HaproxyRouteDataRemovedEvent custom event.

    This event indicates that one of the endpoints was removed.
    """


class HaproxyRouteTcpProviderEvents(CharmEvents):
    """List of events for the haproxy-route TCP provider.

    Attributes:
        data_available: This event indicates that
            the haproxy-route endpoints are available.
        data_removed: This event indicates that one of the endpoints was removed.
    """

    data_available = EventSource(HaproxyRouteTcpDataAvailableEvent)
    data_removed = EventSource(HaproxyRouteTcpDataRemovedEvent)


class HaproxyRouteTcpProvider(Object):
    """Haproxy-route interface provider implementation.

    Attributes:
        on: Custom events of the provider.
        relations: Related appliations.
    """

    on = HaproxyRouteTcpProviderEvents()

    def __init__(
        self,
        charm: CharmBase,
        relation_name: str = HAPROXY_ROUTE_TCP_RELATION_NAME,
        raise_on_validation_error: bool = False,
    ) -> None:
        """Initialize the HaproxyRouteProvider.

        Args:
            charm: The charm that is instantiating the library.
            relation_name: The name of the relation.
            raise_on_validation_error: Whether the library should raise
                HaproxyRouteTcpInvalidRelationDataError when requirer data validation fails.
                If this is set to True the provider charm needs to also catch and handle the
                thrown exception.
        """
        super().__init__(charm, relation_name)

        self._relation_name = relation_name
        self.charm = charm
        self.raise_on_validation_error = raise_on_validation_error
        on = self.charm.on
        self.framework.observe(on[self._relation_name].relation_changed, self._configure)
        self.framework.observe(on[self._relation_name].relation_broken, self._on_endpoint_removed)
        self.framework.observe(
            on[self._relation_name].relation_departed, self._on_endpoint_removed
        )

    @property
    def relations(self) -> list[Relation]:
        """The list of Relation instances associated with this endpoint."""
        return list(self.charm.model.relations[self._relation_name])

    def _configure(self, _event: EventBase) -> None:
        """Handle relation events."""
        if relations := self.relations:
            # Only for data validation
            _ = self.get_data(relations)
            self.on.data_available.emit()

    def _on_endpoint_removed(self, _: EventBase) -> None:
        """Handle relation broken/departed events."""
        self.on.data_removed.emit()

    def get_data(self, relations: list[Relation]) -> HaproxyRouteTcpRequirersData:
        """Fetch requirer data.

        Args:
            relations: A list of Relation instances to fetch data from.

        Raises:
            HaproxyRouteTcpInvalidRelationDataError: When requirer data validation fails.

        Returns:
            HaproxyRouteRequirersData: Validated data from all haproxy-route requirers.
        """
        requirers_data: list[HaproxyRouteTcpRequirerData] = []
        relation_ids_with_invalid_data: set[int] = set()
        for relation in relations:
            try:
                application_data = self._get_requirer_application_data(relation)
                units_data = self._get_requirer_units_data(relation)
                haproxy_route_tcp_requirer_data = HaproxyRouteTcpRequirerData(
                    application_data=application_data,
                    units_data=units_data,
                    relation_id=relation.id,
                    application=relation.app.name,
                )
                requirers_data.append(haproxy_route_tcp_requirer_data)
            except DataValidationError as exc:
                if self.raise_on_validation_error:
                    logger.error(
                        "haproxy-route-tcp data validation failed for relation %s: %s",
                        relation,
                        str(exc),
                    )
                    raise HaproxyRouteTcpInvalidRelationDataError(
                        f"haproxy-route-tcp data validation failed for relation: {relation}"
                    ) from exc
                relation_ids_with_invalid_data.add(relation.id)
                continue
        return HaproxyRouteTcpRequirersData(
            requirers_data=requirers_data,
            relation_ids_with_invalid_data=relation_ids_with_invalid_data,
        )

    def _get_requirer_units_data(self, relation: Relation) -> list[TcpRequirerUnitData]:
        """Fetch and validate the requirer's units data.

        Args:
            relation: The relation to fetch unit data from.

        Raises:
            DataValidationError: When unit data validation fails.

        Returns:
            list[RequirerUnitData]: List of validated unit data from the requirer.
        """
        requirer_units_data: list[TcpRequirerUnitData] = []

        for unit in relation.units:
            databag = relation.data.get(unit)
            if not databag:
                logger.error(
                    "Requirer unit data does not exist even though the unit is still present."
                )
                continue
            try:
                data = cast(TcpRequirerUnitData, TcpRequirerUnitData.load(databag))
                requirer_units_data.append(data)
            except DataValidationError:
                logger.error("Invalid requirer application data for %s", unit)
                raise
        return requirer_units_data

    def _get_requirer_application_data(self, relation: Relation) -> TcpRequirerApplicationData:
        """Fetch and validate the requirer's application databag.

        Args:
            relation: The relation to fetch application data from.

        Raises:
            DataValidationError: When requirer application data validation fails.

        Returns:
            RequirerApplicationData: Validated application data from the requirer.
        """
        try:
            return cast(
                TcpRequirerApplicationData,
                TcpRequirerApplicationData.load(relation.data[relation.app]),
            )
        except DataValidationError:
            logger.error("Invalid requirer application data for %s", relation.app.name)
            raise

    def publish_proxied_endpoints(self, endpoints: list[str], relation: Relation) -> None:
        """Publish to the app databag the proxied endpoints.

        Args:
            endpoints: The list of proxied endpoints to publish.
            relation: The relation with the requirer application.
        """
        HaproxyRouteTcpProviderAppData(
            endpoints=[cast(AnyUrl, endpoint) for endpoint in endpoints]
        ).dump(relation.data[self.charm.app], clear=True)


class HaproxyRouteTcpEnpointsReadyEvent(EventBase):
    """HaproxyRouteTcpEnpointsReadyEvent custom event."""


class HaproxyRouteTcpEndpointsRemovedEvent(EventBase):
    """HaproxyRouteTcpEndpointsRemovedEvent custom event."""


class HaproxyRouteTcpRequirerEvents(CharmEvents):
    """List of events that the TLS Certificates requirer charm can leverage.

    Attributes:
        ready: when the provider proxied endpoints are ready.
        removed: when the provider
    """

    ready = EventSource(HaproxyRouteTcpEnpointsReadyEvent)
    removed = EventSource(HaproxyRouteTcpEndpointsRemovedEvent)


class HaproxyRouteTcpRequirer(Object):
    """haproxy-route interface requirer implementation.

    Attributes:
        on: Custom events of the requirer.
    """

    on = HaproxyRouteTcpRequirerEvents()

    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    def __init__(
        self,
        charm: CharmBase,
        relation_name: str,
        *,
        port: Optional[int] = None,
        backend_port: Optional[int] = None,
        hosts: Optional[list[str]] = None,
        sni: Optional[str] = None,
        check_interval: Optional[int] = None,
        check_rise: Optional[int] = None,
        check_fall: Optional[int] = None,
        check_type: Optional[TCPHealthCheckType] = None,
        check_send: Optional[str] = None,
        check_expect: Optional[str] = None,
        check_db_user: Optional[str] = None,
        load_balancing_algorithm: Optional[LoadBalancingAlgorithm] = None,
        load_balancing_consistent_hashing: bool = False,
        rate_limit_connections_per_minute: Optional[int] = None,
        rate_limit_policy: TCPRateLimitPolicy = TCPRateLimitPolicy.REJECT,
        upload_limit: Optional[int] = None,
        download_limit: Optional[int] = None,
        retry_count: Optional[int] = None,
        retry_redispatch: bool = False,
        server_timeout: Optional[int] = None,
        connect_timeout: Optional[int] = None,
        queue_timeout: Optional[int] = None,
        server_maxconn: Optional[int] = None,
        ip_deny_list: Optional[list[IPvAnyAddress]] = None,
        enforce_tls: bool = True,
        tls_terminate: bool = True,
        unit_address: Optional[str] = None,
    ) -> None:
        """Initialize the HaproxyRouteRequirer.

        Args:
            charm: The charm that is instantiating the library.
            relation_name: The name of the relation to bind to.
            port: The provider port.
            backend_port: List of ports the service is listening on.
            hosts: List of backend server addresses. Currently only support IP addresses.
            sni: List of URL paths to route to this service.
            check_interval: Interval between health checks in seconds.
            check_rise: Number of successful health checks before server is considered up.
            check_fall: Number of failed health checks before server is considered down.
            check_type: Health check type,
                Can be “generic”, “mysql”, “postgres”, “redis” or “smtp”.
            check_send: Only used in generic health checks,
                specify a string to send in the health check request.
            check_expect: Only used in generic health checks,
                specify the expected response from a health check request.
            check_db_user: Only used if type is postgres or mysql,
                specify the user name to enable HAproxy to send a Client Authentication packet.
            load_balancing_algorithm: Algorithm to use for load balancing.
            load_balancing_consistent_hashing: Whether to use consistent hashing.
            rate_limit_connections_per_minute: Maximum connections allowed per minute.
            rate_limit_policy: Policy to apply when rate limit is reached.
            upload_limit: Maximum upload bandwidth in bytes per second.
            download_limit: Maximum download bandwidth in bytes per second.
            retry_count: Number of times to retry failed requests.
            retry_redispatch: Whether to redispatch failed requests to another server.
            server_timeout: Timeout for requests from haproxy to backend servers in seconds.
            connect_timeout: Timeout for client requests to haproxy in seconds.
            queue_timeout: Timeout for requests waiting in queue in seconds.
            server_maxconn: Maximum connections per server.
            ip_deny_list: List of source IP addresses to block.
            enforce_tls: Whether to enforce TLS for all traffic coming to the backend.
            tls_terminate: Whether to enable tls termination on the dedicated frontend.
            unit_address: IP address of the unit (if not provided, will use binding address).
        """
        super().__init__(charm, relation_name)

        self._relation_name = relation_name
        self.relation = self.model.get_relation(self._relation_name)
        self.charm = charm
        self.app = self.charm.app

        # build the full application data
        self._application_data = self._generate_application_data(
            port=port,
            backend_port=backend_port,
            hosts=hosts,
            sni=sni,
            check_interval=check_interval,
            check_rise=check_rise,
            check_fall=check_fall,
            check_type=check_type,
            check_send=check_send,
            check_expect=check_expect,
            check_db_user=check_db_user,
            load_balancing_algorithm=load_balancing_algorithm,
            load_balancing_consistent_hashing=load_balancing_consistent_hashing,
            rate_limit_connections_per_minute=rate_limit_connections_per_minute,
            rate_limit_policy=rate_limit_policy,
            upload_limit=upload_limit,
            download_limit=download_limit,
            retry_count=retry_count,
            retry_redispatch=retry_redispatch,
            server_timeout=server_timeout,
            connect_timeout=connect_timeout,
            queue_timeout=queue_timeout,
            server_maxconn=server_maxconn,
            ip_deny_list=ip_deny_list,
            enforce_tls=enforce_tls,
            tls_terminate=tls_terminate,
        )
        self._unit_address = unit_address

        on = self.charm.on
        self.framework.observe(on[self._relation_name].relation_created, self._configure)
        self.framework.observe(on[self._relation_name].relation_changed, self._configure)
        self.framework.observe(on[self._relation_name].relation_broken, self._on_relation_broken)

    def _configure(self, _: EventBase) -> None:
        """Handle relation events."""
        self.update_relation_data()
        if self.relation and self.get_proxied_endpoints():
            # This event is only emitted when the provider databag changes
            # which only happens when relevant changes happened
            # Additionally this event is purely informational and it's up to the requirer to
            # fetch the proxied endpoints in their code using get_proxied_endpoints
            self.on.ready.emit()

    def _on_relation_broken(self, _: RelationBrokenEvent) -> None:
        """Handle relation broken event."""
        self.on.removed.emit()

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def provide_haproxy_route_tcp_requirements(
        self,
        *,
        port: int,
        backend_port: Optional[int] = None,
        hosts: Optional[list[str]] = None,
        sni: Optional[str] = None,
        check_interval: Optional[int] = None,
        check_rise: Optional[int] = None,
        check_fall: Optional[int] = None,
        check_type: Optional[TCPHealthCheckType] = None,
        check_send: Optional[str] = None,
        check_expect: Optional[str] = None,
        check_db_user: Optional[str] = None,
        load_balancing_algorithm: Optional[LoadBalancingAlgorithm] = None,
        load_balancing_consistent_hashing: bool = False,
        rate_limit_connections_per_minute: Optional[int] = None,
        rate_limit_policy: TCPRateLimitPolicy = TCPRateLimitPolicy.REJECT,
        upload_limit: Optional[int] = None,
        download_limit: Optional[int] = None,
        retry_count: Optional[int] = None,
        retry_redispatch: bool = False,
        server_timeout: Optional[int] = None,
        connect_timeout: Optional[int] = None,
        queue_timeout: Optional[int] = None,
        server_maxconn: Optional[int] = None,
        ip_deny_list: Optional[list[IPvAnyAddress]] = None,
        enforce_tls: bool = True,
        tls_terminate: bool = True,
        unit_address: Optional[str] = None,
    ) -> None:
        """Update haproxy-route requirements data in the relation.

        Args:
            port: The provider port.
            backend_port: List of ports the service is listening on.
            hosts: List of backend server addresses. Currently only support IP addresses.
            sni: List of URL paths to route to this service.
            check_interval: Interval between health checks in seconds.
            check_rise: Number of successful health checks before server is considered up.
            check_fall: Number of failed health checks before server is considered down.
            check_type: Health check type,
                Can be “generic”, “mysql”, “postgres”, “redis” or “smtp”.
            check_send: Only used in generic health checks,
                specify a string to send in the health check request.
            check_expect: Only used in generic health checks,
                specify the expected response from a health check request.
            check_db_user: Only used if type is postgres or mysql,
                specify the user name to enable HAproxy to send a Client Authentication packet.
            load_balancing_algorithm: Algorithm to use for load balancing.
            load_balancing_consistent_hashing: Whether to use consistent hashing.
            rate_limit_connections_per_minute: Maximum connections allowed per minute.
            rate_limit_policy: Policy to apply when rate limit is reached.
            upload_limit: Maximum upload bandwidth in bytes per second.
            download_limit: Maximum download bandwidth in bytes per second.
            retry_count: Number of times to retry failed requests.
            retry_redispatch: Whether to redispatch failed requests to another server.
            server_timeout: Timeout for requests from haproxy to backend servers in seconds.
            connect_timeout: Timeout for client requests to haproxy in seconds.
            queue_timeout: Timeout for requests waiting in queue in seconds.
            server_maxconn: Maximum connections per server.
            ip_deny_list: List of source IP addresses to block.
            enforce_tls: Whether to enforce TLS for all traffic coming to the backend.
            tls_terminate: Whether to enable tls termination on the dedicated frontend.
            unit_address: IP address of the unit (if not provided, will use binding address).
        """
        self._unit_address = unit_address
        self._application_data = self._generate_application_data(
            port=port,
            backend_port=backend_port,
            hosts=hosts,
            sni=sni,
            check_interval=check_interval,
            check_rise=check_rise,
            check_fall=check_fall,
            check_type=check_type,
            check_send=check_send,
            check_expect=check_expect,
            check_db_user=check_db_user,
            load_balancing_algorithm=load_balancing_algorithm,
            load_balancing_consistent_hashing=load_balancing_consistent_hashing,
            rate_limit_connections_per_minute=rate_limit_connections_per_minute,
            rate_limit_policy=rate_limit_policy,
            upload_limit=upload_limit,
            download_limit=download_limit,
            retry_count=retry_count,
            retry_redispatch=retry_redispatch,
            server_timeout=server_timeout,
            connect_timeout=connect_timeout,
            queue_timeout=queue_timeout,
            server_maxconn=server_maxconn,
            ip_deny_list=ip_deny_list,
            enforce_tls=enforce_tls,
            tls_terminate=tls_terminate,
        )
        self.update_relation_data()

    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    def _generate_application_data(
        self,
        *,
        port: Optional[int] = None,
        backend_port: Optional[int] = None,
        hosts: Optional[list[str]] = None,
        sni: Optional[str] = None,
        check_interval: Optional[int] = None,
        check_rise: Optional[int] = None,
        check_fall: Optional[int] = None,
        check_type: Optional[TCPHealthCheckType] = None,
        check_send: Optional[str] = None,
        check_expect: Optional[str] = None,
        check_db_user: Optional[str] = None,
        load_balancing_algorithm: Optional[LoadBalancingAlgorithm] = None,
        load_balancing_consistent_hashing: bool = False,
        rate_limit_connections_per_minute: Optional[int] = None,
        rate_limit_policy: TCPRateLimitPolicy = TCPRateLimitPolicy.REJECT,
        upload_limit: Optional[int] = None,
        download_limit: Optional[int] = None,
        retry_count: Optional[int] = None,
        retry_redispatch: bool = False,
        server_timeout: Optional[int] = None,
        connect_timeout: Optional[int] = None,
        queue_timeout: Optional[int] = None,
        server_maxconn: Optional[int] = None,
        ip_deny_list: Optional[list[IPvAnyAddress]] = None,
        enforce_tls: bool = True,
        tls_terminate: bool = True,
    ) -> dict[str, Any]:
        """Generate the complete application data structure.

        Args:
            port: The provider port.
            backend_port: List of ports the service is listening on.
            hosts: List of backend server addresses. Currently only support IP addresses.
            sni: List of URL paths to route to this service.
            check_interval: Interval between health checks in seconds.
            check_rise: Number of successful health checks before server is considered up.
            check_fall: Number of failed health checks before server is considered down.
            check_type: Health check type,
                Can be “generic”, “mysql”, “postgres”, “redis” or “smtp”.
            check_send: Only used in generic health checks,
                specify a string to send in the health check request.
            check_expect: Only used in generic health checks,
                specify the expected response from a health check request.
            check_db_user: Only used if type is postgres or mysql,
                specify the user name to enable HAproxy to send a Client Authentication packet.
            load_balancing_algorithm: Algorithm to use for load balancing.
            load_balancing_consistent_hashing: Whether to use consistent hashing.
            rate_limit_connections_per_minute: Maximum connections allowed per minute.
            rate_limit_policy: Policy to apply when rate limit is reached.
            upload_limit: Maximum upload bandwidth in bytes per second.
            download_limit: Maximum download bandwidth in bytes per second.
            retry_count: Number of times to retry failed requests.
            retry_redispatch: Whether to redispatch failed requests to another server.
            server_timeout: Timeout for requests from haproxy to backend servers in seconds.
            connect_timeout: Timeout for client requests to haproxy in seconds.
            queue_timeout: Timeout for requests waiting in queue in seconds.
            server_maxconn: Maximum connections per server.
            ip_deny_list: List of source IP addresses to block.
            enforce_tls: Whether to enforce TLS for all traffic coming to the backend.
            tls_terminate: Whether to enable tls termination on the dedicated frontend.

        Returns:
            dict: A dictionary containing the complete application data structure.
        """
        # Apply default value to list parameters to avoid problems with mutable default args.
        if not hosts:
            hosts = []
        if not ip_deny_list:
            ip_deny_list = []

        application_data: dict[str, Any] = {
            "port": port,
            "backend_port": backend_port,
            "hosts": hosts,
            "sni": sni,
            "load_balancing": self._generate_load_balancing_configuration(
                load_balancing_algorithm, load_balancing_consistent_hashing
            ),
            "check": self._generate_server_health_check_configuration(
                check_interval,
                check_rise,
                check_fall,
                check_type,
                check_send,
                check_expect,
                check_db_user,
            ),
            "timeout": self._generate_timeout_configuration(
                server_timeout, connect_timeout, queue_timeout
            ),
            "rate_limit": self._generate_rate_limit_configuration(
                rate_limit_connections_per_minute, rate_limit_policy
            ),
            "bandwidth_limit": {
                "download": download_limit,
                "upload": upload_limit,
            },
            "retry": self._generate_retry_configuration(retry_count, retry_redispatch),
            "ip_deny_list": ip_deny_list,
            "server_maxconn": server_maxconn,
            "enforce_tls": enforce_tls,
            "tls_terminate": tls_terminate,
        }

        return application_data

    def _generate_server_health_check_configuration(
        self,
        interval: Optional[int],
        rise: Optional[int],
        fall: Optional[int],
        check_type: Optional[TCPHealthCheckType],
        send: Optional[str],
        expect: Optional[str],
        db_user: Optional[str],
    ) -> Optional[dict[str, int | str | TCPHealthCheckType | None]]:
        """Generate configuration for server health checks.

        Args:
        interval: Number of seconds between consecutive health check attempts.
        rise: Number of consecutive successful health checks required for up.
        fall: Number of consecutive failed health checks required for DOWN.
        check_type: Health check type, Can be “generic”, “mysql”, “postgres”, “redis” or “smtp”.
        send: Only used in generic health checks,
            specify a string to send in the health check request.
        expect: Only used in generic health checks,
            specify the expected response from a health check request.
        db_user: Only used if type is postgres or mysql,
            specify the user name to enable HAproxy to send a Client Authentication packet.

        Returns:
            Optional[dict[str, int | str | TCPHealthCheckType | None]]:
                Health check configuration dictionary.
        """
        if not (interval and rise and fall):
            return None
        return {
            "interval": interval,
            "rise": rise,
            "fall": fall,
            "check_type": check_type,
            "send": send,
            "expect": expect,
            "db_user": db_user,
        }

    def _generate_rate_limit_configuration(
        self,
        connections_per_minute: Optional[int],
        policy: TCPRateLimitPolicy,
    ) -> Optional[dict[str, Any]]:
        """Generate rate limit configuration.

        Args:
            connections_per_minute: Maximum connections allowed per minute.
            policy: Policy to apply when rate limit is reached.

        Returns:
            Optional[dict[str, Any]]: Rate limit configuration,
                or None if required fields are not configured.
        """
        if not connections_per_minute:
            return None
        return {
            "connections_per_minute": connections_per_minute,
            "policy": policy,
        }

    def _generate_timeout_configuration(
        self,
        server_timeout_in_seconds: Optional[int],
        connect_timeout_in_seconds: Optional[int],
        queue_timeout_in_seconds: Optional[int],
    ) -> Optional[dict[str, Optional[int]]]:
        """Generate rate limit configuration.

        Args:
            server_timeout_in_seconds: Server timeout.
            connect_timeout_in_seconds: Connect timeout.
            queue_timeout_in_seconds: Queue timeout

        Returns:
            Optional[dict[str, Any]]: Rate limit configuration,
                or None if required fields are not configured.
        """
        if not (
            server_timeout_in_seconds or connect_timeout_in_seconds or queue_timeout_in_seconds
        ):
            return None
        return {
            "server": server_timeout_in_seconds,
            "connect": connect_timeout_in_seconds,
            "queue": queue_timeout_in_seconds,
        }

    def _generate_retry_configuration(
        self, count: Optional[int], redispatch: bool
    ) -> Optional[dict[str, Any]]:
        """Generate retry configuration.

        Args:
            count: Number of times to retry failed requests.
            redispatch: Whether to redispatch failed requests to another server.

        Returns:
            Optional[dict[str, Any]]: Retry configuration dictionary,
                or None if required fields are not configured.
        """
        if not count:
            return None
        return {
            "count": count,
            "redispatch": redispatch,
        }

    def _generate_load_balancing_configuration(
        self, algorithm: Optional[LoadBalancingAlgorithm], consistent_hashing: bool
    ) -> Optional[dict[str, Any]]:
        """Generate load balancing configuration.

        Args:
            algorithm: The load balancing algorithm.
            consistent_hashing: Whether to use consistent hashing.

        Returns:
            Optional[dict[str, Any]]: Load balancing configuration dictionary,
                or None if required fields are not configured.
        """
        if not algorithm:
            return None
        return {
            "algorithm": algorithm,
            "consistent_hashing": consistent_hashing,
        }

    def update_relation_data(self) -> None:
        """Update both application and unit data in the relation."""
        if not self._application_data.get("port"):
            logger.warning("port must be set, skipping update.")
            return

        if relation := self.relation:
            self._update_application_data(relation)
            self._update_unit_data(relation)

    def _update_application_data(self, relation: Relation) -> None:
        """Update application data in the relation databag.

        Args:
            relation: The relation instance.
        """
        if self.charm.unit.is_leader():
            application_data = self._prepare_application_data()
            application_data.dump(relation.data[self.app], clear=True)

    def _update_unit_data(self, relation: Relation) -> None:
        """Prepare and update the unit data in the relation databag.

        Args:
            relation: The relation instance.
        """
        unit_data = self._prepare_unit_data()
        unit_data.dump(relation.data[self.charm.unit], clear=True)

    def _prepare_application_data(self) -> TcpRequirerApplicationData:
        """Prepare and validate the application data.

        Raises:
            DataValidationError: When validation of application data fails.

        Returns:
            RequirerApplicationData: The validated application data model.
        """
        try:
            return cast(
                TcpRequirerApplicationData,
                TcpRequirerApplicationData.from_dict(self._application_data),
            )
        except ValidationError as exc:
            logger.error("Validation error when preparing requirer application data.")
            raise DataValidationError(
                "Validation error when preparing requirer application data."
            ) from exc

    def _prepare_unit_data(self) -> TcpRequirerUnitData:
        """Prepare and validate unit data.

        Raises:
            DataValidationError: When no address or unit IP is available.

        Returns:
            RequirerUnitData: The validated unit data model.
        """
        address = self._unit_address
        if not address:
            network_binding = self.charm.model.get_binding(self._relation_name)
            if (
                network_binding is not None
                and (bind_address := network_binding.network.bind_address) is not None
            ):
                address = str(bind_address)
            else:
                logger.error("No unit IP available.")
                raise DataValidationError("No unit IP available.")
        return TcpRequirerUnitData(address=cast(IPvAnyAddress, address))

    def get_proxied_endpoints(self) -> list[AnyUrl]:
        """The full ingress URL to reach the current unit.

        Returns:
            The provider URL or None if the URL isn't available yet or is not valid.
        """
        relation = self.relation
        if not relation or not relation.app:
            return []

        # Fetch the provider's app databag
        try:
            databag = relation.data[relation.app]
        except ModelError:
            logger.exception("Error reading remote app data.")
            return []

        if not databag:  # not ready yet
            return []

        try:
            provider_data = cast(
                HaproxyRouteTcpProviderAppData, HaproxyRouteTcpProviderAppData.load(databag)
            )
            return provider_data.endpoints
        except DataValidationError:
            logger.exception("Invalid provider url.")
            return []

    # The following methods allows for chaining which aims to improve the developper experience
    # The following methods allows for chaining which aims to improve the developper experience
    def configure_port(self, port: int) -> "Self":
        """Set the provider port.

        Args:
            port: The provider port to set

        Returns:
            Self: The HaproxyRouteTcpRequirer class
        """
        self._application_data["port"] = port
        return self

    def configure_backend_port(self, backend_port: int) -> "Self":
        """Set the backend port.

        Args:
            backend_port: The backend port to set

        Returns:
            Self: The HaproxyRouteTcpRequirer class
        """
        self._application_data["backend_port"] = backend_port
        return self

    def configure_hosts(self, hosts: Optional[list[int]] = None) -> "Self":
        """Set backend hosts.

        Args:
            hosts: The hosts list to set

        Returns:
            Self: The HaproxyRouteTcpRequirer class
        """
        if not hosts:
            hosts = []
        self._application_data["hosts"] = hosts
        return self

    def configure_sni(self, sni: str) -> "Self":
        """Set the SNI.

        Args:
            sni: The SNI to set

        Returns:
            Self: The HaproxyRouteTcpRequirer class
        """
        self._application_data["sni"] = sni
        return self

    def configure_health_check(
        self,
        interval: int,
        rise: int,
        fall: int,
        check_type: TCPHealthCheckType = TCPHealthCheckType.GENERIC,
        send: Optional[str] = None,
        expect: Optional[str] = None,
        db_user: Optional[str] = None,
    ) -> "Self":
        """Configure server health check.

        Args:
        interval: Number of seconds between consecutive health check attempts.
        rise: Number of consecutive successful health checks required for up.
        fall: Number of consecutive failed health checks required for DOWN.
        check_type: Health check type, Can be "generic", "mysql", "postgres", "redis" or "smtp".
        send: Only used in generic health checks,
            specify a string to send in the health check request.
        expect: Only used in generic health checks,
            specify the expected response from a health check request.
        db_user: Only used if type is postgres or mysql,
            specify the user name to enable HAproxy to send a Client Authentication packet.

        Returns:
            Self: The HaproxyRouteTcpRequirer class
        """
        self._application_data["check"] = self._generate_server_health_check_configuration(
            interval,
            rise,
            fall,
            check_type,
            send,
            expect,
            db_user,
        )
        return self

    def configure_rate_limit(
        self,
        connections_per_minute: int,
        policy: TCPRateLimitPolicy = TCPRateLimitPolicy.REJECT,
    ) -> "Self":
        """Configure rate limit.

        Args:
            connections_per_minute: The number of connections per minute allowed
            policy: The rate limit policy to apply

        Returns:
            Self: The HaproxyRouteTcpRequirer class
        """
        self._application_data["rate_limit"] = self._generate_rate_limit_configuration(
            connections_per_minute, policy
        )
        return self

    def configure_bandwidth_limit(
        self,
        upload_bytes_per_second: Optional[int] = None,
        download_bytes_per_second: Optional[int] = None,
    ) -> "Self":
        """Configure bandwidth limit.

        Args:
            upload_bytes_per_second: Upload bandwidth limit in bytes per second
            download_bytes_per_second: Download bandwidth limit in bytes per second

        Returns:
            Self: The HaproxyRouteTcpRequirer class
        """
        if not upload_bytes_per_second and not download_bytes_per_second:
            logger.error(
                "At least one of `upload_bytes_per_second` "
                "or `upload_bytes_per_second` must be set."
            )
            return self
        self._application_data["bandwidth_limit"] = {
            "download": download_bytes_per_second,
            "upload": upload_bytes_per_second,
        }

        return self

    def configure_retry(
        self,
        retry_count: int,
        retry_redispatch: bool = False,
    ) -> "Self":
        """Configure retry.

        Args:
            retry_count: The number of retries to attempt
            retry_redispatch: Whether to enable retry redispatch

        Returns:
            Self: The HaproxyRouteTcpRequirer class
        """
        self._application_data["retry"] = self._generate_retry_configuration(
            retry_count, retry_redispatch
        )
        return self

    def configure_timeout(
        self,
        server_timeout_in_seconds: Optional[int],
        connect_timeout_in_seconds: Optional[int],
        queue_timeout_in_seconds: Optional[int],
    ) -> "Self":
        """Configure timeout.

        Args:
            server_timeout_in_seconds: Server timeout.
            connect_timeout_in_seconds: Connect timeout.
            queue_timeout_in_seconds: Queue timeout

        Returns:
            Self: The HaproxyRouteTcpRequirer class
        """
        if not (
            server_timeout_in_seconds or connect_timeout_in_seconds or queue_timeout_in_seconds
        ):
            logger.error(
                "At least one of `server_timeout_in_seconds`, `connect_timeout_in_seconds` "
                "or `queue_timeout_in_seconds` must be set."
            )
            return self
        self._application_data["timeout"] = self._generate_timeout_configuration(
            server_timeout_in_seconds, connect_timeout_in_seconds, queue_timeout_in_seconds
        )
        return self

    def configure_server_max_connections(self, max_connections: int) -> "Self":
        """Set the server max connections.

        Args:
            max_connections: The number of max connections to set

        Returns:
            Self: The HaproxyRouteTcpRequirer class
        """
        self._application_data["server_maxconn"] = max_connections
        return self

    def disable_tls_termination(self) -> "Self":
        """Disable TLS termination.

        Returns:
            Self: The HaproxyRouteTcpRequirer class
        """
        self._application_data["tls_terminate"] = False
        return self

    def allow_http(self) -> "Self":
        """Do not enforce TLS.

        Returns:
            Self: The HaproxyRouteTcpRequirer class
        """
        self._application_data["enforce_tls"] = False
        return self

    def configure_deny_list(self, ip_deny_list: Optional[list[IPvAnyAddress]] = None) -> "Self":
        """Configure IP deny list.

        Args:
            ip_deny_list: List of IP addresses to deny

        Returns:
            Self: The HaproxyRouteTcpRequirer class
        """
        if not ip_deny_list:
            ip_deny_list = []
        self._application_data["ip_deny_list"] = False
        return self
