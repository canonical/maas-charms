# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Backups implementation."""

import json
import logging
import math
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from boto3.session import Session
from botocore import loaders
from botocore.client import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ParamValidationError,
    SSLError,
)
from botocore.regions import EndpointResolver
from charms.data_platform_libs.v0.s3 import CredentialsChangedEvent, S3Requirer
from ops.charm import ActionEvent
from ops.framework import Object
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus

from helper import MaasHelper

logger = logging.getLogger(__name__)

BACKUP_ID_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
FAILED_TO_ACCESS_CREATE_BUCKET_ERROR_MESSAGE = (
    "failed to access/create the bucket, check your S3 settings"
)
S3_BLOCK_MESSAGES = [
    FAILED_TO_ACCESS_CREATE_BUCKET_ERROR_MESSAGE,
]
SNAP_PATH_TO_IDS = "/var/snap/maas/common/maas/maas_id"
SNAP_PATH_TO_IMAGES = "/var/snap/maas/common/maas/image-storage"
SNAP_PATH_TO_PRESEEDS = "/var/snap/maas/current/preseeds"
METADATA_PATH = "backup/latest"
REFER_TO_DEBUG_LOG = " Please check the juju debug-log for more details."
MAAS_REGION_RELATION = "maas-cluster"

# filenames
MODEL_UUID_FILENAME = "model-uuid.txt"
METADATA_FILENAME = "backup_metadata.json"
CONTROLLER_LIST_FILENAME = "controllers.txt"
IMAGE_TAR_FILENAME = "image-storage.tar.gz"
PRESEED_TAR_FILENAME = "preseeds.tar.gz"


class RegionsNotAvailableError(Exception):
    """Raised when regions cannot be obtained from MAAS."""

    pass


class ProgressPercentage:
    """Parent class to track the progress of a file transfer to/from S3."""

    def __init__(self, filename: str, log_label: str, update_interval: int = 10):
        self._filename = filename
        self._size = 0
        self._seen_so_far = 0
        self._lock = threading.Lock()
        self._update_interval = update_interval
        self._last_percentage = 0
        self._log_label = log_label
        self._verb = "transferring"
        self._direction = "to/from"

    def __call__(self, bytes_amount: int):
        """Track the progress of the upload as a callback."""
        if self._size <= 0:
            logger.info(
                f"{self._verb} {self._log_label} {self._direction} s3: {100:.2f}% (empty file)"
            )
            return

        with self._lock:
            self._seen_so_far += bytes_amount
            percentage = (self._seen_so_far / self._size) * 100
            if (percentage - self._last_percentage) >= self._update_interval or percentage >= 100:
                self._last_percentage = percentage
                logger.info(
                    f"{self._verb} {self._log_label} {self._direction} s3: {percentage:.2f}%"
                )


class UploadProgressPercentage(ProgressPercentage):
    """Class to track the progress of a file upload to s3."""

    def __init__(self, filename: str, log_label: str, update_interval: int = 10):
        super().__init__(filename, log_label, update_interval)
        self._size = float(os.path.getsize(filename))
        self._verb = "uploading"
        self._direction = "to"


class DownloadProgressPercentage(ProgressPercentage):
    """Class to track the progress of a file upload to S3."""

    def __init__(self, filename: str, log_label: str, size: float, update_interval: int = 10):
        super().__init__(filename, log_label, update_interval)
        self._size = size
        self._verb = "downloading"
        self._direction = "from"


class MAASBackups(Object):
    """In this class, we manage MAASBackups backups."""

    def __init__(self, charm, relation_name: str):
        """Manage MAASBackups backups."""
        super().__init__(charm, "backup")
        self.charm = charm
        self.relation_name = relation_name

        # s3 relation handles the config options for s3 backups
        self.s3_client = S3Requirer(self.charm, self.relation_name)
        self.framework.observe(
            self.s3_client.on.credentials_changed, self._on_s3_credential_changed
        )
        self.framework.observe(self.s3_client.on.credentials_gone, self._on_s3_credential_gone)
        self.framework.observe(self.charm.on.create_backup_action, self._on_create_backup_action)
        self.framework.observe(self.charm.on.list_backups_action, self._on_list_backups_action)
        self.framework.observe(self.charm.on.restore_backup_action, self._on_restore_backup_action)

    def _get_s3_session_resource(
        self, s3_parameters: dict[str, str], ca_file_path: str | None
    ) -> Any:
        session = Session(
            aws_access_key_id=s3_parameters["access-key"],
            aws_secret_access_key=s3_parameters["secret-key"],
            region_name=s3_parameters["region"],
        )
        return session.resource(
            "s3",
            endpoint_url=self._construct_endpoint(s3_parameters),
            verify=(ca_file_path),
            config=Config(
                # https://github.com/boto/boto3/issues/4400#issuecomment-2600742103
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def _get_s3_session_client(
        self, s3_parameters: dict[str, str], ca_file_path: str | None
    ) -> Any:
        session = Session(
            aws_access_key_id=s3_parameters["access-key"],
            aws_secret_access_key=s3_parameters["secret-key"],
            region_name=s3_parameters["region"],
        )
        return session.client(
            "s3",
            endpoint_url=self._construct_endpoint(s3_parameters),
            verify=(ca_file_path),
            config=Config(
                # https://github.com/boto/boto3/issues/4400#issuecomment-2600742103
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    @contextmanager
    def _s3_resource(self, s3_parameters: dict[str, str]) -> Iterator[Any]:
        ca_chain = s3_parameters.get("tls-ca-chain", [])
        with tempfile.NamedTemporaryFile() if ca_chain else nullcontext() as ca_file:
            if ca_file:
                ca = "\n".join(ca_chain)
                ca_file.write(ca.encode())
                ca_file.flush()

                s3 = self._get_s3_session_resource(s3_parameters, ca_file.name)
            else:
                s3 = self._get_s3_session_resource(s3_parameters, None)

            yield s3

    @contextmanager
    def _s3_client(self, s3_parameters: dict[str, str]) -> Iterator[Any]:
        ca_chain = s3_parameters.get("tls-ca-chain", [])
        with tempfile.NamedTemporaryFile() if ca_chain else nullcontext() as ca_file:
            if ca_file:
                ca = "\n".join(ca_chain)
                ca_file.write(ca.encode())
                ca_file.flush()

                s3 = self._get_s3_session_client(s3_parameters, ca_file.name)
            else:
                s3 = self._get_s3_session_client(s3_parameters, None)

            yield s3

    def _log_error(
        self,
        event: ActionEvent,
        msg: str,
        fail_msg: str | None = None,
        msg_prefix: str | None = None,
        exc: Exception | None = None,
    ) -> None:
        """Log an error, optionally including exception info."""
        if exc:
            logger.exception(msg=msg, exc_info=exc)
        else:
            logger.error(msg=msg)

        message = fail_msg or msg
        event.fail(f"{msg_prefix}: {message}" if msg_prefix else message)

    def _are_backup_settings_ok(self) -> tuple[bool, str]:
        """Validate whether backup settings are OK."""
        if self.model.get_relation(self.relation_name) is None:
            return (
                False,
                "Relation with s3-integrator charm missing, cannot create/restore backup.",
            )

        _, missing_parameters = self._retrieve_s3_parameters()
        if missing_parameters:
            return False, f"Missing S3 parameters: {missing_parameters}"

        return True, ""

    def _can_unit_perform_backup(self) -> tuple[bool, str]:
        """Validate whether this unit can perform a backup."""
        if not self.charm.unit.is_leader():
            return False, "Unit is not the leader"

        if self.charm.is_blocked:
            return False, "Unit is in a blocking state"

        return self._are_backup_settings_ok()

    def _construct_endpoint(self, s3_parameters: dict[str, str]) -> str:
        """Construct the S3 service endpoint using the region.

        This is needed when the provided endpoint is from AWS, and it doesn't contain the region.
        """
        # Use the provided endpoint if a region is not needed.
        endpoint = s3_parameters["endpoint"]

        # Load endpoints data.
        loader = loaders.create_loader()
        data = loader.load_data("endpoints")

        # Construct the endpoint using the region.
        resolver = EndpointResolver(data)
        endpoint_data = resolver.construct_endpoint("s3", s3_parameters["region"])

        # Use the built endpoint if it is an AWS endpoint.
        if endpoint_data and endpoint.endswith(endpoint_data["dnsSuffix"]):
            endpoint = f"{endpoint.split('://')[0]}://{endpoint_data['hostname']}"

        return endpoint

    def _create_bucket_if_not_exists(self, s3_parameters: dict[str, str]) -> None:
        bucket_name = s3_parameters["bucket"]
        region = s3_parameters.get("region")

        with self._s3_resource(s3_parameters) as s3:
            bucket = s3.Bucket(bucket_name)
            try:
                bucket.meta.client.head_bucket(Bucket=bucket_name)
                logger.info("Bucket %s exists.", bucket_name)
                exists = True
            except ConnectTimeoutError as e:
                # Re-raise the error if the connection timeouts, so the user has the possibility to
                # fix network issues and call juju resolve to re-trigger the hook that calls
                # this method.
                logger.error(
                    f"error: {e!s} - please fix the error and call juju resolve on this unit"
                )
                raise e
            except ClientError:
                logger.warning(
                    "Bucket %s doesn't exist or you don't have access to it.", bucket_name
                )
                exists = False
            except SSLError as e:
                logger.error(f"error: {e!s} - Is TLS enabled and CA chain set on S3?")
                raise e
            if exists:
                return

            try:
                bucket.create(CreateBucketConfiguration={"LocationConstraint": region})

                bucket.wait_until_exists()
                logger.info("Created bucket '%s' in region=%s", bucket_name, region)
            except ClientError as error:
                if error.response["Error"]["Code"] == "InvalidLocationConstraint":
                    logger.info(
                        "Specified location-constraint is not valid, trying create without it"
                    )
                    try:
                        bucket.create()

                        bucket.wait_until_exists()
                        logger.info("Created bucket '%s', ignored region=%s", bucket_name, region)
                    except ClientError as error:
                        logger.exception(
                            "Couldn't create bucket named '%s' in region=%s.", bucket_name, region
                        )
                        raise error
                else:
                    logger.exception(
                        "Couldn't create bucket named '%s' in region=%s.", bucket_name, region
                    )
                    raise error

    def _format_backup_list(
        self,
        backup_list: list[tuple[str, str, str, str, str, str, str]],
        s3_bucket: str,
        s3_path: str,
    ) -> str:
        """Format provided list of backups as a table."""
        backups = [
            f"Storage bucket name: {s3_bucket:s}",
            f"Backups base path: {s3_path:s}/backup/\n",
            f"{'backup-id':<20} | {'action':<11} | {'status':<8} | {'maas':<8} | {'size':<10} | {'controllers':<22} | {'backup-path'}",
        ]
        backups.append("-" * len(backups[2]))
        for (
            backup_id,
            backup_action,
            backup_status,
            version,
            size,
            controllers,
            path,
        ) in backup_list:
            backups.append(
                f"{backup_id:<20s} | {backup_action:<11s} | {backup_status:<8s} | {version:<8s} | {size:<10s} | {controllers:<22s} | {path:s}"
            )
        return "\n".join(backups)

    def _generate_backup_list_output(self) -> str:
        """Generate a list of backups in a formatted table.

        List contains successful backups in order of ascending time.
        """
        s3_parameters, _ = self._retrieve_s3_parameters()
        backups = self._list_backups(s3_parameters)
        backup_list = []
        for backup in backups:
            # TODO: backup_action is statically set for now. It can be enriched with extra content
            # if such functionality is added to the backup mechanism.
            backup_action = "full backup"
            backup_path = f"/{s3_parameters['path'].lstrip('/')}/backup/{backup['id']}"
            backup_status = "finished" if backup["completed"] else "failed"
            backup_list.append(
                (
                    backup["id"],
                    backup_action,
                    backup_status,
                    backup["maas_version"],
                    backup["size"],
                    ", ".join(backup["controller_ids"]),
                    backup_path,
                )
            )

        backup_list.sort(key=lambda x: x[0])

        return self._format_backup_list(
            backup_list, s3_parameters["bucket"], s3_parameters["path"]
        )

    def _list_backups(self, s3_parameters) -> list[dict]:
        """Retrieve the list of backups.

        Returns:
            a list of previously created backups or an empty list if there are no backups in the S3
                bucket.
        """
        backups = []
        with self._s3_client(s3_parameters) as client:
            paginator = client.get_paginator("list_objects_v2")
            page_iterator = paginator.paginate(
                Bucket=s3_parameters["bucket"],
                Prefix=f"{s3_parameters['path'].lstrip('/')}/backup/",
                Delimiter="/",
            )
            for page in page_iterator:
                for common_prefix in page.get("CommonPrefixes", []):
                    backups.append(
                        common_prefix["Prefix"]
                        .lstrip(f"{s3_parameters['path'].lstrip('/')}/backup/")
                        .rstrip("/")
                    )

        backup_dict_list = []

        for backup in backups:
            backup_dict_list.append(self._get_backup_details(s3_parameters, backup))

        return backup_dict_list

    def _get_backup_details(self, s3_parameters: dict, backup_id: str) -> dict:
        total_size = 0
        contents = []

        with self._s3_client(s3_parameters) as client:
            prefix = f"{s3_parameters['path'].lstrip('/')}/backup/{backup_id}/"
            paginator = client.get_paginator("list_objects_v2")
            page_iterator = paginator.paginate(
                Bucket=s3_parameters["bucket"],
                Prefix=prefix,
                Delimiter="/",
            )
            for page in page_iterator:
                for content in page.get("Contents", []):
                    contents.append(content["Key"].removeprefix(prefix))
                    total_size += content["Size"]

        size = as_size(total_size)
        complete_files = set(contents) == {
            METADATA_FILENAME,
            CONTROLLER_LIST_FILENAME,
            IMAGE_TAR_FILENAME,
            PRESEED_TAR_FILENAME,
        }

        metadata: dict[str, str] = {}
        if metadata_str := self._read_content_from_s3(
            f"backup/{backup_id}/{METADATA_FILENAME}", s3_parameters
        ):
            metadata = json.loads(metadata_str)

        controller_ids: list[str] = []
        if controller_str := self._read_content_from_s3(
            f"backup/{backup_id}/{CONTROLLER_LIST_FILENAME}", s3_parameters
        ):
            controller_ids = controller_str.strip("\n").split("\n")

        completed = metadata.get("success", False) and complete_files
        maas_version = metadata.get("maas_snap_version", "")

        return {
            "id": backup_id,
            "size": size,
            "controller_ids": controller_ids,
            "completed": completed,
            "maas_version": maas_version,
        }

    def _on_s3_credential_changed(self, event: CredentialsChangedEvent) -> None:
        if not self.charm.unit.is_leader():
            return

        s3_parameters, _ = self._retrieve_s3_parameters()
        if not s3_parameters:
            return

        try:
            self._create_bucket_if_not_exists(s3_parameters)
        except (ClientError, ValueError, ParamValidationError, SSLError):
            self.charm.unit.status = BlockedStatus(FAILED_TO_ACCESS_CREATE_BUCKET_ERROR_MESSAGE)
            return

        self.charm.unit.status = ActiveStatus()

    def _on_s3_credential_gone(self, event) -> None:
        if self.charm.is_blocked and self.charm.unit.status.message in S3_BLOCK_MESSAGES:
            self.charm.unit.status = ActiveStatus()

    def _on_create_backup_action(self, event) -> None:
        can_unit_perform_backup, validation_message = self._can_unit_perform_backup()
        if not can_unit_perform_backup:
            self._log_error(event=event, msg=validation_message, msg_prefix="Backup failed")
            logger.error(f"Backup failed: {validation_message}")
            event.fail(validation_message)
            return

        # Test uploading metadata to S3 to test credentials before backup.
        datetime_backup_requested = datetime.now().strftime(BACKUP_ID_FORMAT)
        metadata = f"""Date Backup Requested: {datetime_backup_requested}
Model Name: {self.model.name}
Application Name: {self.model.app.name}
Unit Name: {self.charm.unit.name}
Juju Version: {self.charm.model.juju_version!s}
"""

        logging.info("Uploading metadata to S3")
        s3_parameters, _ = self._retrieve_s3_parameters()
        if not self._upload_content_to_s3(
            metadata,
            METADATA_PATH,
            s3_parameters,
        ):
            error_message = "Failed to upload metadata to provided S3."
            logger.error(f"Backup failed: {error_message}")
            event.fail(error_message + REFER_TO_DEBUG_LOG)
            return

        self.charm.unit.status = MaintenanceStatus("creating backup")

        self._run_backup(event, s3_parameters)

        self.charm.unit.status = ActiveStatus()

    def _run_backup(
        self,
        event: ActionEvent,
        s3_parameters: dict,
    ) -> None:
        backup_id = self._generate_backup_id()
        s3_path = os.path.join(s3_parameters["path"], f"backup/{backup_id}").lstrip("/")

        backup_success = self._execute_backup_to_s3(
            event=event,
            s3_parameters=s3_parameters,
            s3_path=s3_path,
        )

        # TODO: upload logs in addition to metadata
        event.log("Uploading backup metadata to S3...")
        metadata_success = self._upload_backup_metadata(
            s3_parameters=s3_parameters,
            s3_path=s3_path,
            success=backup_success,
        )
        if not metadata_success:
            error_message = f"Failed to upload backup metadata to S3 for backup-id {backup_id}."
            logger.error(f"Backup failed: {error_message}")
            event.fail(error_message + REFER_TO_DEBUG_LOG)
            return

        if backup_success:
            event.log(f"Backup succeeded with backup-id {backup_id}")
            event.set_results({"backups": f"backup created with id {backup_id}"})
        else:
            error_message = "Failed to archive and upload MAAS files to S3."
            logger.error(f"Backup failed: {error_message}")
            event.fail(error_message + REFER_TO_DEBUG_LOG)

    def _execute_backup_to_s3(
        self,
        event: ActionEvent,
        s3_parameters: dict[str, str],
        s3_path: str,
    ) -> bool:
        with self._s3_client(s3_parameters) as client:
            bucket_name = s3_parameters["bucket"]

            try:
                self._backup_maas_to_s3(
                    event=event,
                    client=client,
                    bucket_name=bucket_name,
                    s3_path=s3_path,
                )
            except Exception as e:
                msg = f"Failed to backup to S3 bucket={bucket_name}, path={s3_path}."
                logger.error(msg)
                logger.error(repr(e))
                event.fail(msg + REFER_TO_DEBUG_LOG)
                return False

        return True

    def _backup_maas_to_s3(
        self,
        event: ActionEvent,
        client: Any,
        bucket_name: str,
        s3_path: str,
    ):
        # get region ids
        event.log("Retrieving region ids from MAAS...")
        try:
            regions = self.charm.get_region_system_ids()
        except subprocess.CalledProcessError:
            # Avoid logging the apikey of an Admin user
            raise RegionsNotAvailableError("Failed to retrieve region ids from the MAAS API")

        sorted_regions = sorted(regions)
        # upload regions
        region_path = os.path.join(s3_path, CONTROLLER_LIST_FILENAME)
        with tempfile.NamedTemporaryFile(suffix=".txt") as f:
            f.write("\n".join(sorted_regions).encode("utf-8"))
            f.flush()
            event.log("Uploading region ids to S3...")
            client.upload_file(
                f.name,
                bucket_name,
                region_path,
                Callback=UploadProgressPercentage(f.name, log_label="region ids"),
            )

        # archive and upload images
        image_path = os.path.join(s3_path, IMAGE_TAR_FILENAME)
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as f:
            event.log("Creating image archive for S3 backup...")
            with tarfile.open(fileobj=f, mode="w:gz") as tar:
                tar.add(
                    SNAP_PATH_TO_IMAGES,
                    arcname="",
                )
            f.flush()
            event.log("Uploading image archive to S3, see debug-log for progress...")
            client.upload_file(
                f.name,
                bucket_name,
                image_path,
                Callback=UploadProgressPercentage(f.name, "image archive"),
            )

        # archive and upload preseeds
        preseed_path = os.path.join(s3_path, PRESEED_TAR_FILENAME)
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as f:
            event.log("Creating preseed archive for S3 backup...")
            with tarfile.open(fileobj=f, mode="w:gz") as tar:
                tar.add(
                    SNAP_PATH_TO_PRESEEDS,
                    arcname="",
                )
            f.flush()
            event.log("Uploading preseed archive to S3...")
            client.upload_file(
                f.name,
                bucket_name,
                preseed_path,
                Callback=UploadProgressPercentage(f.name, "preseed archive"),
            )

    def _upload_backup_metadata(
        self,
        s3_parameters: dict[str, str],
        s3_path: str,
        success: bool,
    ) -> bool:
        ca_chain = s3_parameters.get("tls-ca-chain", [])
        with tempfile.NamedTemporaryFile() if ca_chain else nullcontext() as ca_file:
            if ca_file:
                ca = "\n".join(ca_chain)
                ca_file.write(ca.encode())
                ca_file.flush()
                client = self._get_s3_session_client(s3_parameters, ca_file.name)
            else:
                client = self._get_s3_session_client(s3_parameters, None)

            bucket_name = s3_parameters["bucket"]

            try:
                metadata_path = os.path.join(s3_path, METADATA_FILENAME)
                metadata = self._get_backup_metadata()
                metadata["success"] = success
                with tempfile.NamedTemporaryFile(suffix=".yaml") as f:
                    f.write(json.dumps(metadata).encode())
                    f.flush()
                    client.upload_file(
                        f.name,
                        bucket_name,
                        metadata_path,
                    )
            except Exception as e:
                logger.exception(e)
                return False
        return True

    def _get_backup_metadata(self) -> dict[str, Any]:
        return {
            "maas_snap_version": self.charm.version,
            "maas_snap_channel": MaasHelper.get_installed_channel(),
            "unit_name": self.charm.unit.name,
            "juju_version": str(self.charm.model.juju_version),
        }

    def _generate_backup_id(self) -> str:
        """Create a backup id for failed backup operations (to store log file)."""
        return datetime.strftime(datetime.now(), BACKUP_ID_FORMAT)

    def _on_list_backups_action(self, event) -> None:
        """List the previously created backups."""
        are_backup_settings_ok, validation_message = self._are_backup_settings_ok()
        if not are_backup_settings_ok:
            logger.warning(validation_message)
            event.fail(validation_message)
            return

        try:
            formatted_list = self._generate_backup_list_output()
            event.set_results({"backups": formatted_list})
        except BotoCoreError as e:
            logger.exception(e)
            event.fail(f"Failed to list MAAS backups with error: {e!s}")

    def _on_restore_backup_action(self, event: ActionEvent) -> None:
        if not self._pre_restore_checks(event):
            return

        backup_id: str = event.params.get("backup-id", "")
        controller_id: str = event.params.get("controller-id", "")
        logger.info(
            f"A restore with backup-id '{backup_id}' has been requested on the unit with controller-id '{controller_id}'"
        )

        # Validate the provided backup id
        logger.info("Validating provided backup-id")
        try:
            s3_parameters, _ = self._retrieve_s3_parameters()
            backups = [b["id"] for b in self._list_backups(s3_parameters)]
            is_backup_id_real = backup_id and backup_id in backups
            if backup_id and not is_backup_id_real:
                error_message = f"Invalid backup-id: {backup_id}"
                logger.error(f"Restore failed: {error_message}")
                event.fail(error_message)
                return
        except BotoCoreError as e:
            logger.exception(e)
            error_message = "Failed to retrieve backups list"
            logger.error(f"Restore failed: {error_message}")
            event.fail(error_message)
            return

        self.charm.unit.status = MaintenanceStatus("Restoring from backup...")

        self._run_restore(
            event=event,
            s3_parameters=s3_parameters,
            backup_id=backup_id,
            controller_id=controller_id,
        )

        event.log("Region restore complete")

        self.charm.unit.status = ActiveStatus()
        event.set_results({"restore-status": "restore finished"})

    def _run_restore(
        self, event: ActionEvent, s3_parameters: dict[str, str], backup_id: str, controller_id: str
    ) -> None:
        s3_path = os.path.join(s3_parameters["path"], f"backup/{backup_id}").lstrip("/")

        if not self._check_backup_maas_version(
            event=event, s3_path=s3_path, s3_parameters=s3_parameters
        ):
            event.fail(
                "Failed to validate MAAS version from S3 backup. Check the juju debug-log for more detail."
            )
            return

        if not self._update_controller_id(
            event=event, s3_path=s3_path, s3_parameters=s3_parameters, controller_id=controller_id
        ):
            event.fail(
                "Failed to update maas-region IDs from S3 backup. Check the juju debug-log for more detail."
            )
            return

        if not self._download_and_unarchive_from_s3(
            event=event,
            s3_path=os.path.join(s3_path, PRESEED_TAR_FILENAME).lstrip("/"),
            s3_parameters=s3_parameters,
            local_path=Path(SNAP_PATH_TO_PRESEEDS),
        ):
            event.fail(
                "Failed to download and extract preseeds from S3 backup. Check the juju debug-log for more detail."
            )
            return

        if not self._download_and_unarchive_from_s3(
            event=event,
            s3_path=os.path.join(s3_path, IMAGE_TAR_FILENAME).lstrip("/"),
            s3_parameters=s3_parameters,
            local_path=Path(SNAP_PATH_TO_IMAGES),
        ):
            event.fail(
                "Failed to download and extract images from S3 backup. Check the juju debug-log for more detail."
            )
            return

    def _check_backup_maas_version(
        self, event: ActionEvent, s3_path: str, s3_parameters: dict[str, str]
    ) -> bool:
        event.log("Downloading metadata from s3...")
        with self._download_file_from_s3(
            event=event,
            s3_path=os.path.join(s3_path, METADATA_FILENAME).lstrip("/"),
            s3_parameters=s3_parameters,
        ) as f:
            if f:
                metadata = f.read_text()
            else:
                self._log_error(
                    event, "Could not read metadata from s3", msg_prefix="Restore failed"
                )
                return False

        meta_dict: dict[str, str] = json.loads(metadata)
        backup = meta_dict.get("maas_snap_version")
        if not backup:
            self._log_error(
                event, "Could not locate snap version in backup", msg_prefix="Restore failed"
            )
            return False

        installed = self.charm.version
        if not installed:
            self._log_error(
                event,
                "Could not locate snap version on running MAAS instance",
                msg_prefix="Restore failed",
            )
            return False

        installed_major, installed_minor, installed_point = installed.split("/", 1)[0].split(
            ".", 2
        )
        backup_major, backup_minor, backup_point = backup.split("/", 1)[0].split(".", 2)

        if installed_major != backup_major:
            self._log_error(
                event,
                "MAAS major version does not match backup major version",
                msg_prefix="Restore failed",
            )
            return False

        if installed_minor != backup_minor:
            self._log_error(
                event,
                "MAAS minor version does not match backup minor version",
                msg_prefix="Restore failed",
            )
            return False

        if installed_point < backup_point:
            self._log_error(
                event,
                "MAAS point version is not greater or equal to backup point version",
                msg_prefix="Restore failed",
            )
            return False

        return True

    def _update_controller_id(
        self, event: ActionEvent, s3_path: str, s3_parameters: dict[str, str], controller_id: str
    ) -> bool:
        """Fetch the controllers from S3 and the regions from the relation."""
        event.log("Downloading controllers list from s3...")

        with self._download_file_from_s3(
            event=event,
            s3_path=os.path.join(s3_path, CONTROLLER_LIST_FILENAME).lstrip("/"),
            s3_parameters=s3_parameters,
        ) as f:
            if f:
                controllers_content = f.read_text()
            else:
                self._log_error(
                    event, "Could not read controllers list from s3", msg_prefix="Restore failed"
                )
                return False

        controllers = controllers_content.strip("\n").split("\n")
        relation = self.model.get_relation(MAAS_REGION_RELATION)
        if relation is None:
            self._log_error(
                event, "Could not fetch MAAS regions list", msg_prefix="Restore failed"
            )
            return False

        # the relation only includes remote units, and does not include the local unit
        regions = relation.units.union({self.model.unit})

        if len(controllers) != len(regions):
            self._log_error(
                event,
                f"Restore failed: The number of maas-region units ({len(regions)}) "
                f"does not match the expected value from the backup ({len(controllers)}).",
                msg_prefix="Restore failed",
            )
            return False

        controller_file = Path(SNAP_PATH_TO_IDS)

        if controller_id in controllers:
            controller_file.write_text(f"{controller_id}\n")
            return True

        self._log_error(
            event,
            f"{controller_id} is not a valid ID from the controllers list; "
            f"should be one of {', '.join(controllers)}!",
            msg_prefix="Restore failed",
        )
        return False

    def _download_and_unarchive_from_s3(
        self,
        event: ActionEvent,
        s3_path: str,
        s3_parameters: dict[str, str],
        local_path: Path,
        file_type: str | None = None,
    ) -> bool:
        file_type = file_type or self._pathname(s3_path)

        # Clean before restore
        shutil.rmtree(local_path, ignore_errors=True)
        if local_path.exists():
            self._log_error(
                event, f"Could not remove existing {file_type}", msg_prefix="Untar failed"
            )
            return False
        local_path.mkdir(parents=True)

        event.log(f"Downloading {file_type} from s3...")
        with self._download_file_from_s3(
            event=event, s3_path=s3_path, s3_parameters=s3_parameters
        ) as f:
            if f is None:
                self._log_error(
                    event, f"Could not read {file_type} from s3", msg_prefix="Untar failed"
                )
                return False

            try:
                with tarfile.open(f, mode="r:gz") as tar:
                    tar.extractall(path=local_path)

                if local_path.exists() and any(local_path.iterdir()):
                    return True

                self._log_error(
                    event,
                    f"{file_type.capitalize()} from S3 did not contain any files.",
                    msg_prefix="Untar failed",
                )
                return False

            except tarfile.TarError as e:
                self._log_error(
                    event,
                    f"{file_type.capitalize()} is not a valid .tar.gz file or is corrupted.",
                    msg_prefix="Untar failed",
                    exc=e,
                )
            except (FileNotFoundError, OSError) as e:
                self._log_error(
                    event,
                    f"Filesystem error while extracting {file_type}",
                    msg_prefix="Untar failed",
                    exc=e,
                )

        return False

    @contextmanager
    def _download_file_from_s3(
        self,
        event: ActionEvent,
        s3_path: str,
        s3_parameters: dict[str, str],
        file_type: str | None = None,
    ) -> Iterator[Path | None]:
        """Download a file to a temporary location, yielding the temp path if successful."""
        tmp_path: str = ""
        bucket = s3_parameters["bucket"]

        logger.info(f"Download request for {bucket}:{s3_path}")

        try:
            with self._s3_client(s3_parameters) as client:
                with tempfile.NamedTemporaryFile(suffix=Path(s3_path).suffix, delete=False) as f:
                    tmp_path = f.name

                size = client.head_object(Bucket=bucket, Key=s3_path)["ContentLength"]

                if (free := shutil.disk_usage(tmp_path).free) and (size >= free):
                    self._log_error(
                        event,
                        f"Not enough free storage to download {s3_path}, required {size} but has {free}",
                        msg_prefix="Download failed",
                    )

                client.download_file(
                    bucket,
                    s3_path,
                    tmp_path,
                    Callback=DownloadProgressPercentage(
                        f.name, log_label=file_type or self._pathname(s3_path), size=size
                    ),
                )

                yield Path(tmp_path)

        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                self._log_error(
                    event,
                    f"Could not find object in {bucket}:{s3_path}",
                    msg_prefix="Download failed",
                    exc=e,
                )

            else:
                self._log_error(
                    event,
                    f"Could not read object from {bucket}:{s3_path}",
                    msg_prefix="Download failed",
                    exc=e,
                )
            yield None

        except Exception as e:
            self._log_error(
                event,
                f"Could not read content from {bucket}:{s3_path}",
                msg_prefix="Download failed",
                exc=e,
            )
            yield None

        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        return None

    def _pathname(self, pathname: str) -> str:
        return Path(pathname).name.split(".")[0]

    def _pre_restore_checks(self, event: ActionEvent) -> bool:
        """Run some checks before starting the restore.

        Returns:
            a boolean indicating whether restore should be run.
        """
        are_backup_settings_ok, validation_message = self._are_backup_settings_ok()
        if not are_backup_settings_ok:
            self._log_error(event, validation_message, msg_prefix="Restore failed")
            return False

        if not event.params.get("backup-id"):
            self._log_error(
                event,
                "The 'backup-id' parameter must be specified to perform a restore",
                msg_prefix="Restore failed",
            )
            return False

        if not event.params.get("controller-id"):
            self._log_error(
                event,
                "The 'controller-id' parameter must be specified to perform a restore",
                msg_prefix="Restore failed",
            )
            return False

        logger.info("Checking if cluster is in blocked state")
        if self.charm.is_blocked:
            self._log_error(
                event, "Cluster or unit is in a blocking state", msg_prefix="Restore failed"
            )
            return False

        logger.info("Checking that the PostgreSQL relation has been removed")
        if relation := self.model.get_relation(relation_name="maas-db"):
            self._log_error(
                event,
                "PostgreSQL relation still exists, please run:\n"
                f"juju remove-relation {self.model.app.name} {relation.app.name}\n"
                "then retry this action",
                msg_prefix="Restore failed",
            )
            return False

        return True

    def _retrieve_s3_parameters(self) -> tuple[dict[str, str], list[str]]:
        """Retrieve S3 parameters from the S3 integrator relation."""
        s3_parameters = self.s3_client.get_s3_connection_info()
        required_parameters = [
            "bucket",
            "access-key",
            "secret-key",
        ]
        missing_required_parameters = [
            param for param in required_parameters if param not in s3_parameters
        ]
        if missing_required_parameters:
            logger.warning(
                f"Missing required S3 parameters in relation with S3 integrator: {missing_required_parameters}"
            )
            return {}, missing_required_parameters

        # Add some sensible defaults (as expected by the code) for missing optional parameters
        s3_parameters.setdefault("endpoint", "https://s3.amazonaws.com")
        s3_parameters.setdefault("region", "")
        s3_parameters.setdefault("path", "")
        s3_parameters.setdefault("s3-uri-style", "host")
        s3_parameters.setdefault("delete-older-than-days", "9999999")

        # Strip whitespaces from all parameters.
        for key, value in s3_parameters.items():
            if isinstance(value, str):
                s3_parameters[key] = value.strip()

        # Clean up extra slash symbols to avoid issues on 3rd-party storages
        # like Ceph Object Gateway (RadosGW).
        s3_parameters["endpoint"] = s3_parameters["endpoint"].rstrip("/")
        s3_parameters["path"] = f"/{s3_parameters['path'].strip('/')}"
        s3_parameters["bucket"] = s3_parameters["bucket"].strip("/")

        return s3_parameters, []

    def _upload_content_to_s3(
        self, content: str, s3_path: str, s3_parameters: dict[str, str]
    ) -> bool:
        """Upload the provided contents to the provided S3 bucket relative to the path from the S3 config.

        Args:
            content: The content to upload to S3
            s3_path: The S3 path from which download the content
            s3_parameters: The S3 parameters needed to perform the request

        Returns:
            a boolean indicating success.
        """
        with self._s3_resource(s3_parameters) as s3:
            path = os.path.join(s3_parameters["path"], s3_path).lstrip("/")

            try:
                logger.info(f"Uploading content to bucket={s3_parameters['bucket']}, path={path}")
                bucket = s3.Bucket(s3_parameters["bucket"])

                with tempfile.NamedTemporaryFile() as temp_file:
                    temp_file.write(content.encode("utf-8"))
                    temp_file.flush()
                    bucket.upload_file(temp_file.name, path)
            except Exception as e:
                logger.exception(
                    f"Failed to upload content to S3 bucket={s3_parameters['bucket']}, path={path}",
                    exc_info=e,
                )
                return False

        return True

    def _read_content_from_s3(self, s3_path: str, s3_parameters: dict[str, str]) -> str | None:
        """Read specified content from the provided S3 bucket relative to the path from the S3 config.

        Args:
            s3_path: The S3 path from which download the content
            s3_parameters: The S3 parameters needed to perform the request

        Returns:
            a string with the content if object is successfully downloaded and None if file is not existing or error
            occurred during download.
        """
        with self._s3_resource(s3_parameters) as s3:
            path = os.path.join(s3_parameters["path"], s3_path).lstrip("/")

            try:
                logger.info(f"Reading content from bucket={s3_parameters['bucket']}, path={path}")
                bucket = s3.Bucket(s3_parameters["bucket"])
                with BytesIO() as buf:
                    bucket.download_fileobj(path, buf)
                    return buf.getvalue().decode("utf-8")
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    logger.info(
                        f"No such object to read from S3 bucket={s3_parameters['bucket']}, path={path}"
                    )
                else:
                    logger.exception(
                        f"Failed to read content from S3 bucket={s3_parameters['bucket']}, path={path}",
                        exc_info=e,
                    )
            except Exception as e:
                logger.exception(
                    f"Failed to read content from S3 bucket={s3_parameters['bucket']}, path={path}",
                    exc_info=e,
                )

        return None


def as_size(size: int) -> str:
    """Return a string representation of a number in Binary base."""
    base = 1024  # extendable to generic SI in future
    prefixes = {
        0: "",
        1: "Ki",
        2: "Mi",
        3: "Gi",
        4: "Ti",
        5: "Pi",
        6: "Ei",
        7: "Zi",
        8: "Yi",
        9: "Ri",
        10: "Qi",
    }
    power = math.floor(math.log(size, base))
    return f"{size / (base**power):.1f}{prefixes.get(power, f' x {base}^{power} ')}B"
