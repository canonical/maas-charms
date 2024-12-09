"""MAAS Site Manager operator library.

Allows MAAS clusters to enrol with Site Manager
"""

import dataclasses
import json
import logging
from typing import Any, Dict, List, MutableMapping, Union

import ops

# The unique Charmhub library identifier, never change it
LIBID = "c232507f53c34b929e1e7c2bb030d2ce"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 2

DEFAULT_ENDPOINT_NAME = "maas-site-manager"
TOKEN_SECRET_KEY = "enrol-token"

log = logging.getLogger(__name__)


class SiteManagerEnrolInterfaceError(Exception):
    """Common ancestor for Enrol interface related exceptions."""


@dataclasses.dataclass
class EnrolDatabag:
    """Base class from Enrol databags."""

    @classmethod
    def load(cls, data: Dict[str, str]) -> "EnrolDatabag":
        """Load from dictionary."""
        init_vals = {}
        for f in dataclasses.fields(cls):
            val = data.get(f.name)
            init_vals[f.name] = val if f.type == str else json.loads(val)  # type: ignore  # noqa: E721
        return cls(**init_vals)

    def dump(self, databag: Union[MutableMapping[str, str], None] = None) -> None:
        """Write the contents of this model to Juju databag."""
        if databag is None:
            databag = {}
        else:
            databag.clear()
        for f in dataclasses.fields(self):
            val = getattr(self, f.name)
            databag[f.name] = val if f.type == str else json.dumps(val)  # noqa: E721


@dataclasses.dataclass
class EnrolRequirerAppData(EnrolDatabag):
    """The schema for the Requirer side of this relation."""

    uuid: str


@dataclasses.dataclass
class EnrolProviderAppData(EnrolDatabag):
    """The schema for the Provider side of this relation."""

    token_id: str

    def get_token(self, model: ops.Model) -> str:
        """Retrieve enrolment token.

        Returns:
            str: the token
        """
        token = model.get_secret(id=self.token_id)
        return token.get_content().get(TOKEN_SECRET_KEY, "")


class TokenIssuedEvent(ops.EventBase):
    """Event emitted when Site Manager has emitted a token for this relation."""

    def __init__(self, handle: ops.Handle, token: str):
        super().__init__(handle)
        self._token: str = token

    def snapshot(self) -> Dict[str, Any]:
        """Serialize the event to disk.

        Not meant to be called by charm code.
        """
        data = super().snapshot()
        data.update({"token": self._token})
        return data

    def restore(self, snapshot: Dict[str, Any]):
        """Deserialize the event from disk.

        Not meant to be called by charm code.
        """
        self._token = snapshot["token"]


class TokenWithdrawEvent(ops.EventBase):
    """Event emitted when the relation with the "site-manager" provider has been severed.

    Or when the relation data has been wiped.
    """


class EnrolRequirerEvents(ops.CharmEvents):
    """MAAS events."""

    token_issued = ops.EventSource(TokenIssuedEvent)
    created = ops.EventSource(ops.RelationCreatedEvent)
    removed = ops.EventSource(TokenWithdrawEvent)


class EnrolRequirer(ops.Object):
    """Requires-side of the Enrolment relation."""

    on = EnrolRequirerEvents()  # type: ignore

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
        if self._charm.unit.is_leader() and self._relation:
            if data := self.get_enrol_data():
                token = data.get_token(self.model)
                self.on.token_issued.emit(token)
            elif self.is_published():
                self.on.removed.emit()

    def _on_relation_created(self, event: ops.RelationCreatedEvent) -> None:
        self.on.created.emit(relation=event.relation, app=event.app, unit=event.unit)

    def _on_relation_broken(self, _event: ops.RelationBrokenEvent) -> None:
        self.on.removed.emit()

    def get_enrol_data(self) -> Union[EnrolProviderAppData, None]:
        """Get enrolment data from databag."""
        relation = self._relation
        if relation:
            assert relation.app is not None
            try:
                databag = relation.data[relation.app]
                return EnrolProviderAppData.load(databag)  # type: ignore
            except TypeError:
                log.debug(f"invalid databag contents: {databag}")  # type: ignore
        return None

    def is_published(self) -> bool:
        """Verify that the local side has done all they need to do."""
        relation = self._relation
        if not relation:
            return False
        app_data = relation.data[self._charm.app]
        try:
            EnrolRequirerAppData.load(app_data)  # type: ignore
            return True
        except TypeError:
            return False

    def request_enrol(self, cluster_uuid: str) -> None:
        """Request enrolment."""
        if not self._charm.unit.is_leader():
            return
        databag_model = EnrolRequirerAppData(
            uuid=cluster_uuid,
        )
        if relation := self._relation:
            app_databag = relation.data[self.model.app]
            databag_model.dump(app_databag)


class EnrolProvider(ops.Object):
    """Provides-side of the Enrol relation."""

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
        label = f"enrol-{relation.name}-{relation.id}.secret"
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

    def publish_enrol_token(self, relation: ops.Relation, token: str) -> None:
        """Publish enrolment data.

        Args:
            relation (Relation): the Relation
            token (str): Enrolment token
        """
        secret_id = self._update_secret(relation, {TOKEN_SECRET_KEY: token})
        local_app_databag = EnrolProviderAppData(token_id=secret_id)
        local_app_databag.dump(relation.data[self.model.app])
