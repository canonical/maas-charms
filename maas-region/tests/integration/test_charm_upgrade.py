# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the point release upgrade workflow.

To exercise point releases, a multi-unit MAAS deployment is rolled back to a known
older revision of that track before `pre-upgrade-check` and `upgrade` are run,
mirroring the workflow:

    juju run maas-region/leader pre-upgrade-check
    juju run maas-region/0 upgrade
    juju run maas-region/1 maas-region/2 upgrade

"""

import asyncio
import json
import re
import time

import pytest
from conftest import APP_NAME, POSTGRESQL_CHANNEL
from pytest_operator.plugin import OpsTest

NUM_UNITS = 3
UNITS = [f"{APP_NAME}/{n}" for n in range(NUM_UNITS)]
SNAP_REFRESH_TIMEOUT = 180

ACTION_WAIT = "3m"

# Update when creating a new track. The architecture comes from --model-arch, which
# is required to run the integration tests on different architectures.
DEFAULT_ARCH = "amd64"
OLD_SNAP_REVISIONS = {
    "amd64": "42437",
    "arm64": "42438",
}
OLD_SNAP_VERSION = "3.8.0~beta5"
MAAS_SNAP_CHANNEL = "3.8/edge"


@pytest.fixture(scope="module")
def old_snap_revision(pytestconfig: pytest.Config) -> str:
    """Get the MAAS OLD_SNAP_VERSION revision built for the architecture under test.

    Args:
        pytestconfig (pytest.Config): the test session's configuration

    Returns:
        str: the snap revision to roll back to
    """
    arch = pytestconfig.getoption("--model-arch") or DEFAULT_ARCH
    if revision := OLD_SNAP_REVISIONS.get(arch):
        return revision
    pytest.fail(
        f"No MAAS {OLD_SNAP_VERSION} revision recorded for {arch}. Add one to"
        " OLD_SNAP_REVISIONS."
    )


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest):
    """Test preparation."""
    charm = await ops_test.build_charm(".")

    if ops_test.model is None:
        raise ValueError("Model is not set")

    await asyncio.gather(
        ops_test.model.deploy(charm, application_name=APP_NAME, num_units=NUM_UNITS),
        ops_test.model.wait_for_idle(
            apps=[APP_NAME],
            status="waiting",
            raise_on_blocked=True,
            timeout=1000,
            wait_for_exact_units=NUM_UNITS,
        ),
    )


@pytest.mark.abort_on_fail
async def test_rollback_to_old_revision(ops_test: OpsTest, old_snap_revision: str):
    """Roll the workload back to an older point release of the deployed track."""
    if ops_test.model is None:
        raise ValueError("Model is not set")

    for unit in UNITS:
        await juju_exec(
            ops_test, unit, "snap", "refresh", "maas", f"--revision={old_snap_revision}"
        )

    for unit in UNITS:
        installed = await get_installed_snap_info(ops_test, unit)
        assert installed["revision"] == old_snap_revision
        assert installed["version"] == OLD_SNAP_VERSION, (
            f"Revision {old_snap_revision} is MAAS {installed['version']}, not"
            f" {OLD_SNAP_VERSION}. Update OLD_SNAP_REVISIONS and OLD_SNAP_VERSION."
        )
        # `set_workload_version` equivalent
        await juju_exec(ops_test, unit, "application-version-set", installed["version"])

    # Check juju reports the rolledback version
    status = await ops_test.model.get_status()
    app = status.applications[APP_NAME]
    assert app is not None
    version = app.workload_version
    assert version == OLD_SNAP_VERSION, (
        f"juju reports {APP_NAME} running MAAS {version} after the rollback, expected"
        f" {OLD_SNAP_VERSION}"
    )
    for unit in UNITS:
        reported = app.units[unit].workload_version
        assert reported == OLD_SNAP_VERSION, (
            f"juju reports {unit} running MAAS {reported} after the rollback, expected"
            f" {OLD_SNAP_VERSION}"
        )


@pytest.mark.abort_on_fail
async def test_database_integration(ops_test: OpsTest, old_snap_revision: str):
    """Initialize MAAS on the old revision by integrating with the database."""
    if ops_test.model is None:
        raise ValueError("Model is not set")

    await asyncio.gather(
        ops_test.model.deploy(
            "postgresql",
            application_name="postgresql",
            channel=POSTGRESQL_CHANNEL,
            series="noble",
            config={"plugin_btree_gin_enable": True, "experimental_max_connections": 400},
        ),
        ops_test.model.wait_for_idle(
            apps=["postgresql"], status="active", raise_on_blocked=True, timeout=1000
        ),
    )

    await asyncio.gather(
        ops_test.model.integrate(f"{APP_NAME}", "postgresql"),
        ops_test.model.wait_for_idle(
            apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=1000
        ),
    )

    for unit in UNITS:
        assert (await get_installed_snap_info(ops_test, unit))["revision"] == old_snap_revision


@pytest.mark.abort_on_fail
async def test_pre_upgrade_check_reports_point_upgrade(ops_test: OpsTest, old_snap_revision: str):
    """The leader reports the target revision available for a point upgrade."""
    target = await latest_in_store(ops_test, UNITS[0], MAAS_SNAP_CHANNEL)
    installed = await get_installed_snap_info(ops_test, UNITS[0])
    assert target["revision"] != old_snap_revision, (
        f"Revision {old_snap_revision} is the newest on channel {MAAS_SNAP_CHANNEL}, so there"
        " is no point upgrade to test. Set OLD_SNAP_REVISIONS to an older revision."
    )

    results = await run_action(ops_test, f"{APP_NAME}/leader", action="pre-upgrade-check")
    (leader_results,) = results.values()


    assert leader_results["installed-snap"] == (
        f"{installed['version']} (revision {old_snap_revision}) on channel {MAAS_SNAP_CHANNEL}"
    )
    assert leader_results["upgrade-target-snap"] == (
        f"{target['version']} (revision {target['revision']}) on channel {MAAS_SNAP_CHANNEL}"
    )
    assert leader_results["info"] == (
        f"Point upgrade is possible from {installed['version']} to {target['version']}."
    )

    assert "host-base" not in leader_results
    assert "upgrade-target-charm-bases" not in leader_results
    # There are no standalone rack controllers in this deployment
    assert "rack-controllers" not in leader_results
    assert "rack-info" not in leader_results


@pytest.mark.abort_on_fail
async def test_upgrade_single_unit(ops_test: OpsTest, old_snap_revision: str):
    """A single unit upgrades, and the others are left on the old revision."""
    if ops_test.model is None:
        raise ValueError("Model is not set")

    target = await latest_in_store(ops_test, UNITS[0], MAAS_SNAP_CHANNEL)

    results = await run_action(ops_test, UNITS[0], action="upgrade")
    (upgrade_results,) = results.values()
    assert upgrade_results["info"] == f"Upgrade started for snap on channel {MAAS_SNAP_CHANNEL}"

    # The upgrade action returns before the lock is granted, so wait for the upgrade to
    # be complete
    await wait_for_revision(ops_test, UNITS[0], target["revision"])
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=1000
    )

    upgraded = await get_installed_snap_info(ops_test, UNITS[0])
    assert upgraded["version"] == target["version"]
    assert upgraded["channel"] == MAAS_SNAP_CHANNEL
    assert "held" in upgraded["notes"]
    assert "cohort" in upgraded["notes"]

    for unit in UNITS[1:]:
        assert (await get_installed_snap_info(ops_test, unit))["revision"] == old_snap_revision

    status = await ops_test.model.get_status()
    assert status.applications[APP_NAME].units[UNITS[0]].workload_version == target["version"]

@pytest.mark.abort_on_fail
async def test_upgrade_remaining_units(ops_test: OpsTest):
    """The remaining units upgrade when the action is run on all of them at once."""
    if ops_test.model is None:
        raise ValueError("Model is not set")

    target = await latest_in_store(ops_test, UNITS[0], MAAS_SNAP_CHANNEL)

    await run_action(ops_test, *UNITS[1:], action="upgrade")

    for unit in UNITS[1:]:
        await wait_for_revision(ops_test, unit, target["revision"])
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=1000
    )

    status = await ops_test.model.get_status()
    app = status.applications[APP_NAME]

    # Assert workload versions have been properly updated on all units and the application
    assert app is not None
    version = app.workload_version
    assert version == target["version"]
    for unit in UNITS:
        installed = await get_installed_snap_info(ops_test, unit)
        assert installed["revision"] == target["revision"]
        assert "held" in installed["notes"]
        reported_version = status.applications[APP_NAME].units[unit].workload_version
        assert reported_version == target["version"]
        (maas_status,) = (await run_action(ops_test, unit, action="get-maas-status")).values()
        assert maas_status["services"], f"MAAS reported no services on {unit} after the upgrade"


@pytest.mark.abort_on_fail
async def test_pre_upgrade_check_reports_no_upgrade_needed(ops_test: OpsTest):
    """With every unit on the newest revision, no further point upgrade is offered."""
    target = await latest_in_store(ops_test, UNITS[0], MAAS_SNAP_CHANNEL)

    results = await run_action(ops_test, f"{APP_NAME}/leader", action="pre-upgrade-check")
    (leader_results,) = results.values()

    assert leader_results["info"] == (
        f"Current installed revision ({target['revision']}) is the latest available on channel"
        f" {MAAS_SNAP_CHANNEL}. No upgrade is needed."
    )
    assert "upgrade-target-snap" not in leader_results
    assert "rack-controllers" not in leader_results
    assert "rack-info" not in leader_results


async def juju_exec(ops_test: OpsTest, unit: str, *command: str) -> str:
    """Run a command on a unit's machine.

    Args:
        ops_test (OpsTest): the test harness
        unit (str): the unit to run on, e.g. "maas-region/0"
        command (str): the command and its arguments

    Returns:
        str: the command's stdout
    """
    return_code, stdout, stderr = await ops_test.juju("exec", "--unit", unit, "--", *command)
    assert return_code == 0, f"`{' '.join(command)}` failed on {unit}: {stderr}"
    return stdout


async def run_action(
    ops_test: OpsTest, *units: str, action: str, wait: str = ACTION_WAIT
) -> dict[str, dict]:
    """Run an action on one or more units, and assert that it succeeded.

    Args:
        ops_test (OpsTest): the test harness
        units (str): the units to run on, e.g. "maas-region/leader"
        action (str): the name of the action
        wait (str): how long to wait for the results, e.g. "3m"

    Returns:
        dict[str, dict]: the action results, keyed by the unit that produced them
    """
    return_code, stdout, stderr = await ops_test.juju(
        "run", "--format", "json", "--wait", wait, *units, action
    )
    assert return_code == 0, f"action {action} failed on {', '.join(units)}: {stdout}{stderr}"
    return {unit: data.get("results", {}) for unit, data in json.loads(stdout).items()}


async def get_installed_snap_info(ops_test: OpsTest, unit: str) -> dict[str, str]:
    """Read the state of the MAAS snap installed on a unit.

    Args:
        ops_test (OpsTest): the test harness
        unit (str): the unit to inspect, e.g. "maas-region/0"

    Returns:
        dict[str, str]: the snap's `version`, `revision`, `channel` and `notes`
    """
    # `snap list` prints a header, then info about the installed snap
    row = (await juju_exec(ops_test, unit, "snap", "list", "maas")).strip().splitlines()[-1]
    _, version, revision, channel, _, notes = row.split()
    return {
        "version": version.split("-")[0],
        "revision": revision,
        "channel": channel,
        "notes": notes,
    }


async def latest_in_store(ops_test: OpsTest, unit: str, channel: str) -> dict[str, str]:
    """Read the newest version and revision the store offers for a channel.

    Args:
        ops_test (OpsTest): the test harness
        unit (str): the unit to query the store from, e.g. "maas-region/0"
        channel (str): the snap channel, e.g. "3.8/edge"

    Returns:
        dict[str, str]: the channel's version and revision
    """
    # e.g. "  3.8/edge:  3.8.0~beta6-18657-g.a90cdf264  2026-08-18 (42495) 269MB -"
    store_channel_re = re.compile(
        r"^\s*(?P<channel>\S+):\s+(?P<version>\S+)\s+\S+\s+\((?P<revision>\d+)\)"
    )

    snap_info = await juju_exec(ops_test, unit, "snap", "info", "maas")
    for line in snap_info.splitlines():
        match = store_channel_re.match(line)
        if match and match.group("channel") == channel:
            return {
                "version": match.group("version").split("-")[0],
                "revision": match.group("revision"),
            }
    pytest.fail(f"The snap store reports no revision for channel {channel}")


async def wait_for_revision(ops_test: OpsTest, unit: str, revision: str) -> None:
    """Wait until a unit's MAAS snap is at the given revision.

    Args:
        ops_test (OpsTest): the test harness
        unit (str): the unit to poll, e.g. "maas-region/0"
        revision (str): the snap revision to wait for
    """
    deadline = time.monotonic() + SNAP_REFRESH_TIMEOUT
    while True:
        installed = await get_installed_snap_info(ops_test, unit)
        if installed["revision"] == revision:
            return
        if time.monotonic() > deadline:
            pytest.fail(
                f"{unit} is still on revision {installed['revision']} after"
                f" {SNAP_REFRESH_TIMEOUT}s, expected {revision}"
            )
        await asyncio.sleep(10)
