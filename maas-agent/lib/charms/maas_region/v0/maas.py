"""MAAS operator library.

Allows MAAS Agents to enroll with Region controllers
"""

import dataclasses
import json
import logging
from typing import Any, Dict, List, MutableMapping, Union

import ops
from ops.charm import CharmEvents
from ops.framework import EventSource, Handle, Object
from typing_extensions import Self

# The unique Charmhub library identifier, never change it
LIBID = "3e4a25698b094f96a59aa01367416ecb"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1


DEFAULT_ENDPOINT_NAME = "maas-region"
BUILTIN_JUJU_KEYS = {"ingress-address", "private-address", "egress-subnets"}


log = logging.getLogger(__name__)


class MaasInterfaceError(Exception):
    """Common ancestor for MAAS interface related exceptions."""


@dataclasses.dataclass
class MaasDatabag:
    """Base class from MAAS databags."""

    @classmethod
    def load(cls, data: Dict[str, str]) -> Self:
        """Load from dictionary."""
        init_vals = {}
        for f in dataclasses.fields(cls):
            val = data.get(f.name)
            init_vals[f.name] = val if f.type == str else json.loads(val)  # type: ignore
        return cls(**init_vals)

    def dump(self, databag: Union[MutableMapping[str, str], None] = None) -> None:
        """Write the contents of this model to Juju databag."""
        if databag is None:
            databag = {}
        else:
            databag.clear()
        for f in dataclasses.fields(self):
            val = getattr(self, f.name)
            databag[f.name] = val if f.type == str else json.dumps(val)


@dataclasses.dataclass
class MaasRequirerUnitData(MaasDatabag):
    """The schema for the Requirer side of this relation."""

    unit: str
    url: str


@dataclasses.dataclass
class MaasProviderAppData(MaasDatabag):
    """The schema for the Provider side of this relation."""

    api_url: str
    regions: List[str]
    maas_secret_id: str

    def get_secret(self, model: ops.Model) -> str:
        """Retrieve MAAS secret.

        Returns:
            str: the secret
        """
        secret = model.get_secret(id=self.maas_secret_id)
        return secret.get_content().get("maas-secret", "")


class MaasConfigReceivedEvent(ops.EventBase):
    """Event emitted when the Region has shared the secret."""

    def __init__(
        self,
        handle: Handle,
        config: Dict[str, Any],
    ):
        super().__init__(handle)
        self.config = config

    def snapshot(self) -> Dict[str, Any]:
        """Serialize the event to disk.

        Not meant to be called by charm code.
        """
        data = super().snapshot()
        data.update({"config": json.dumps(self.config)})
        return data

    def restore(self, snapshot: Dict[str, Any]):
        """Deserialize the event from disk.

        Not meant to be called by charm code.
        """
        self.config = json.loads(snapshot["config"])


class MaasAgentRemovedEvent(ops.EventBase):
    """Event emitted when the relation with the "maas-region" provider has been severed.

    Or when the relation data has been wiped.
    """


class MaasRegionRequirerEvents(CharmEvents):
    """MAAS events."""

    config_received = EventSource(MaasConfigReceivedEvent)
    created = EventSource(ops.RelationCreatedEvent)
    removed = EventSource(MaasAgentRemovedEvent)


class MaasRegionRequirer(Object):
    """Requires-side of the MAAS relation."""

    on = MaasRegionRequirerEvents()  # type: ignore

    def __init__(
        self,
        charm: ops.CharmBase,
        key: Union[str, None] = None,
        endpoint: str = DEFAULT_ENDPOINT_NAME,
    ):
        super().__init__(charm, key or endpoint)
        self._charm = charm
        self._endpoint = endpoint

        self.framework.observe(
            self._charm.on[endpoint].relation_changed,
            self._on_relation_changed,
        )
        self.framework.observe(
            self._charm.on[endpoint].relation_created,
            self._on_relation_created,
        )
        self.framework.observe(
            self._charm.on[endpoint].relation_broken,
            self._on_relation_broken,
        )

    @property
    def _relation(self) -> Union[ops.Relation, None]:
        # filter out common unhappy relation states
        relation = self.model.get_relation(self._endpoint)
        return relation if relation and relation.app and relation.data else None

    def _on_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        if self._relation:
            if new_config := self.get_enroll_data():
                cfg: dict[str, str] = {}
                new_config.dump(cfg)
                self.on.config_received.emit(cfg)
            elif self.is_published():
                self.on.removed.emit()

    def _on_relation_created(self, event: ops.RelationCreatedEvent) -> None:
        self.on.created.emit(relation=event.relation, app=event.app, unit=event.unit)

    def _on_relation_broken(self, _event: ops.RelationBrokenEvent) -> None:
        self.on.removed.emit()

    def get_enroll_data(self) -> Union[MaasProviderAppData, None]:
        """Get enrollment data from databag."""
        relation = self._relation
        if relation:
            assert relation.app is not None
            try:
                databag = relation.data[relation.app]
                return MaasProviderAppData.load(databag)  # type: ignore
            except TypeError:
                log.debug(f"invalid databag contents: {databag}")  # type: ignore
        return None

    def is_published(self) -> bool:
        """Verify that the local side has done all they need to do."""
        relation = self._relation
        if not relation:
            return False

        unit_data = relation.data[self._charm.unit]
        try:
            MaasRequirerUnitData.load(unit_data)  # type: ignore
            return True
        except TypeError:
            return False

    def publish_unit_url(self, url: str) -> None:
        """Publish unit url in the databag."""
        databag_model = MaasRequirerUnitData(
            unit=self._charm.unit.name,
            url=url,
        )
        if relation := self._relation:
            unit_databag = relation.data[self.model.unit]
            databag_model.dump(unit_databag)


class MaasRegionProvider(Object):
    """Provides-side of the MAAS relation."""

    def __init__(
        self,
        charm: ops.CharmBase,
        key: Union[str, None] = None,
        endpoint: str = DEFAULT_ENDPOINT_NAME,
    ):
        super().__init__(charm, key or endpoint)
        self._charm = charm
        self._endpoint = endpoint

    @property
    def _relations(self) -> List[ops.Relation]:
        return self.model.relations[self._endpoint]

    def _update_secret(self, relation: ops.Relation, content: Dict[str, str]) -> str:
        label = f"enroll-{relation.name}-{relation.id}.secret"
        try:
            secret = self.model.get_secret(label=label)
            secret.set_content(content)
        except ops.model.SecretNotFoundError:
            secret = self._charm.app.add_secret(
                content=content,
                label=label,
            )
            secret.grant(relation)
        return secret.get_info().id

    def publish_enroll_token(self, maas_api: str, regions: List[str], maas_secret: str) -> None:
        """Publish enrollment data.

        Args:
            maas_api (str): MAAS API URL
            regions (list[str]): List of MAAS regions
            maas_secret (str): Enrollment token
        """
        for relation in self._relations:
            if relation:
                secret_id = self._update_secret(relation, {"maas-secret": maas_secret})
                local_app_databag = MaasProviderAppData(
                    api_url=maas_api,
                    regions=regions,
                    maas_secret_id=secret_id,
                )
                local_app_databag.dump(relation.data[self.model.app])

    def gather_rack_units(self) -> Dict[str, ops.model.Unit]:
        """Get a map of Rack units.

        Returns:
            dict[str, ops.model.Unit]: map of units
        """
        data: dict[str, ops.model.Unit] = {}
        for relation in self._relations:
            if not relation.app:
                continue
            for worker_unit in relation.units:
                try:
                    worker_data = MaasRequirerUnitData.load(relation.data[worker_unit])  # type: ignore
                    url = worker_data.url
                except TypeError as e:
                    log.debug(f"invalid databag contents: {e}")
                    continue
                data[url] = worker_unit
        return data
