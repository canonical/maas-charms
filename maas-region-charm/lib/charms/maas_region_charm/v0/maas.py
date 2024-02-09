"""MAAS operator library.

Allows MAAS Agents to enroll with Region controllers
"""

import dataclasses
import json
import logging
from collections import defaultdict
from typing import Any, Dict, MutableMapping

import ops
from ops.charm import CharmEvents
from ops.framework import EventSource, Handle, Object
from typing_extensions import Self

# The unique Charmhub library identifier, never change it
LIBID = "50055f0422414543ba96d10a9fb7d129"

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
        return cls(**{f: data[f] for f in data if f not in BUILTIN_JUJU_KEYS})

    def dump(self, databag: MutableMapping[str, str] | None = None) -> None:
        """Write the contents of this model to Juju databag."""
        if databag is None:
            databag = {}
        else:
            databag.clear()
        for f in dataclasses.fields(self):
            databag[f.name] = getattr(self, f.name)


@dataclasses.dataclass
class MaasRequirerUnitData(MaasDatabag):
    """The schema for the Requirer side of this relation."""

    model: str
    unit: str
    system_id: str


@dataclasses.dataclass
class MaasProviderAppData(MaasDatabag):
    """The schema for the Provider side of this relation."""

    api_url: str
    maas_secret: str


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

    on = MaasRegionRequirerEvents()

    def __init__(
        self,
        charm: ops.CharmBase,
        key: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT_NAME,
    ):
        super().__init__(charm, key or endpoint)
        self._charm = charm

        # filter out common unhappy relation states
        relation = self.model.get_relation(endpoint)
        self._relation: ops.Relation | None = (
            relation if relation and relation.app and relation.data else None
        )

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

    def get_enroll_data(self) -> MaasProviderAppData | None:
        """Get enrollment data from databag."""
        relation = self._relation
        if relation:
            assert relation.app is not None
            try:
                databag = relation.data[relation.app]
                return MaasProviderAppData.load(databag)
            except TypeError:
                log.info(f"invalid databag contents: {databag}")
        return None

    def is_published(self) -> bool:
        """Verify that the local side has done all they need to do."""
        relation = self._relation
        if not relation:
            return False

        unit_data = relation.data[self._charm.unit]
        try:
            MaasRequirerUnitData.load(unit_data)
            return True
        except TypeError:
            return False

    def publish_unit_system_id(self, id: str) -> None:
        """Publish unit system_id in the databag."""
        databag_model = MaasRequirerUnitData(
            model=self._charm.model.name,
            unit=self._charm.unit.name,
            system_id=id,
        )
        if relation := self._relation:
            unit_databag = relation.data[self.model.unit]
            databag_model.dump(unit_databag)


class MaasRegionProvider(Object):
    """Provides-side of the MAAS relation."""

    def __init__(
        self,
        charm: ops.CharmBase,
        key: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT_NAME,
    ):
        super().__init__(charm, key or endpoint)
        self._charm = charm
        self._relations = self.model.relations[endpoint]

    def publish_enroll_token(self, maas_api: str, maas_secret: str) -> None:
        """Publish enrollment data.

        Args:
            maas_api (str): MAAS API URL
            maas_secret (str): Enrollment token
        """
        local_app_databag = MaasProviderAppData(api_url=maas_api, maas_secret=maas_secret)
        for relation in self._relations:
            if relation:
                local_app_databag.dump(relation.data[self.model.app])

    def gather_rack_units(self) -> dict[str, str]:
        """Get a map of Rack units.

        Returns:
            dict[str, str]: map of units
        """
        data = defaultdict(str)
        for relation in self._relations:
            if not relation.app:
                continue
            for worker_unit in relation.units:
                try:
                    worker_data = MaasRequirerUnitData.load(relation.data[worker_unit])
                    sysid = worker_data.system_id
                except TypeError as e:
                    log.info(f"invalid databag contents: {e}")
                    continue
                data[worker_unit.name] = sysid
        return data
