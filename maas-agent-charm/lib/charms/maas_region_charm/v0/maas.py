"""MAAS operator library.

Allows MAAS Agents to enroll with Region controllers
"""

import json

from ops import CharmBase, RelationChangedEvent, RelationEvent
from ops.charm import CharmEvents
from ops.framework import EventSource, Object

# The unique Charmhub library identifier, never change it
LIBID = "50055f0422414543ba96d10a9fb7d129"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1


class MaasInterfaceError(Exception):
    """Common ancestor for MAAS interface related exceptions."""


class MaasRequiresEvent(RelationEvent):
    """Base class for MAAS region events."""

    @property
    def api_url(self) -> str | None:
        """Returns the MAAS URL."""
        if not self.relation.app:
            return None

        return self.relation.data[self.relation.app].get("api_url")

    @property
    def maas_secret(self) -> str | None:
        """Returns the MAAS URL."""
        if not self.relation.app:
            return None

        return self.relation.data[self.relation.app].get("maas_secret")


class MaasApiUrlChangedEvent(MaasRequiresEvent):
    """MAAS API URL has changed."""


class MaasSecretChangedEvent(MaasRequiresEvent):
    """MAAS secret has changed."""


class MaasRegionRequiresEvents(CharmEvents):
    """MAAS events."""

    api_url_changed = EventSource(MaasApiUrlChangedEvent)
    maas_secret_changed = EventSource(MaasSecretChangedEvent)


class MaasAgentEnrollEvent(RelationEvent):
    """Event emitted when an Agent request enrollment."""


class MaasRegionProvidesEvents(CharmEvents):
    """MAAS events."""

    agent_enroll = EventSource(MaasAgentEnrollEvent)


class MaasRegionRequires(Object):
    """Requires-side of the MAAS relation."""

    on = MaasRegionRequiresEvents()

    def __init__(self, charm: CharmBase, relation_name: str):
        super().__init__(charm, relation_name)
        self.charm = charm
        self.local_app = charm.model.app
        self.relation_name = relation_name
        self.framework.observe(
            charm.on[relation_name].relation_changed,
            self._on_relation_changed,
        )

    def _on_relation_changed(self, event: RelationChangedEvent) -> None:
        old_data = json.loads(event.relation.data[self.local_app].get("data", "{}"))
        new_data = (
            {key: value for key, value in event.relation.data[event.app].items() if key != "data"}
            if event.app
            else {}
        )

        # update data
        event.relation.data[self.local_app].update({"data": json.dumps(new_data)})

        if old_data.get("api_url") != new_data.get("api_url"):
            getattr(self.on, "api_url_changed").emit(
                event.relation, app=event.app, unit=event.unit
            )
        elif old_data.get("maas_secret") != new_data.get("maas_secret"):
            getattr(self.on, "maas_secret_changed").emit(
                event.relation, app=event.app, unit=event.unit
            )

    def fetch_relation_data(self) -> dict[str, str]:
        """Get data from relation databag.

        Returns:
            dict[str, str]: stored data
        """
        relation = self.charm.model.get_relation(self.relation_name)
        if not relation or not relation.app:
            return {}

        if data := relation.data[relation.app]:
            return {
                "api_url": data.get("api_url"),
                "maas_secret": data.get("maas_secret"),
            }
        return {}


class MaasRegionProvides(Object):
    """Provides-side of the MAAS relation."""

    on = MaasRegionProvidesEvents()

    def __init__(self, charm: CharmBase, relation_name: str):
        super().__init__(charm, relation_name)
        self.charm = charm
        self.local_app = charm.model.app
        self.local_unit = charm.unit
        self.relation_name = relation_name
        self.framework.observe(
            self.charm.on[relation_name].relation_changed, self._on_relation_changed
        )

    def _on_relation_changed(self, event: RelationChangedEvent) -> None:
        if not self.local_unit.is_leader():
            return
        getattr(self.on, "agent_enroll").emit(event.relation, app=event.app, unit=event.unit)

    def update_relation_data(self, relation_id: int, data: dict[str, str]) -> None:
        """Update relation databag.

        Args:
            relation_id (int): relation ID
            data (dict[str, str]): new data
        """
        relation = self.charm.model.get_relation(self.relation_name, relation_id)
        if not relation:
            raise MaasInterfaceError(
                "Relation %s %s couldn't be retrieved", self.relation_name, relation_id
            )

        if self.local_app not in relation.data or relation.data[self.local_app] is None:
            return

        relation.data[self.local_app].update(data)
