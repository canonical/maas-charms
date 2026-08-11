# Contributing

To make contributions to this charm, you'll need a working [development setup](https://juju.is/docs/sdk/dev-setup).

You'll need the following tools installed:
- [uv](https://docs.astral.sh/uv/) - Python package manager
- tox 4.16+ with `tox-uv` plugin (install via `uv tool install tox --with tox-uv`)

You can create an environment for development with `tox`:

```shell
tox devenv -e integration
source venv/bin/activate
```

## Testing

This project uses `tox` for managing test environments. There are some pre-configured environments
that can be used for linting and formatting code when you're preparing contributions to the charm:

```shell
tox run -e format        # update your code according to linting rules
tox run -e lint          # code style
tox run -e static        # static type checking for the charm source
tox run -e unit          # unit tests
tox run -e scenario      # scenario tests
tox run -e integration   # integration tests
tox                      # runs 'format', 'lint', 'static', 'coverage-report', 'scenario', and 'integration' environments
```

## Build the charm

Build the charm in this git repository using:

```shell
charmcraft pack
```

## Development

### Jhack

During development, [jhack](https://github.com/canonical/jhack) is a useful tool that provides scripts to help with development and debugging. Some useful commands are: 

- Sync your local charm code to all deployed units of the maas-region charm:
```shell 
cd maas-region
jhack sync maas-region
```
- Show the contents of a specific relation:
```shell
jhack show-relation maas-region:maas-cluster
```
- Show the events obtained by the charm:
```shell
jhack tail maas-region
```

### Testing changes on a dev branch

Despite having jhack sync, you may still need to publish your charm on a dev branch 
to test changes. Run the following sequentially to build, upload, and release the charm to a dev branch:
```shell
charmcraft pack
charmcraft upload maas-region_ubuntu@24.04-amd64.charm  # this produces a revision to use in the next command
charmcraft release maas-region --revision=$REVISION --channel latest/edge/$DEV_BRANCH_NAME
```
Note that this will be deleted after 30 days, see [the docs](https://canonical.com/juju/docs/juju-cli/3.6/reference/charm/#branch).

<!-- You may want to include any contribution/style guidelines in this document>
