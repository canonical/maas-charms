# MAAS Region Operator

The MAAS Region Operator is a Juju machine charm that deploys and manages the
[MAAS](https://maas.io/) region controller. It provides the MAAS API and manages
the controller's configuration, upgrades, backups, and integrations with
PostgreSQL, HAProxy, and observability services.

The Charmhub package and Juju application name remain `maas-region`; the
repository name is `maas-region-operator`.

## Deployment and documentation

- [Charmhub package](https://charmhub.io/maas-region)
- [Charm documentation](https://discourse.charmhub.io/t/maas-region-docs-index/13286)
- [LXD lab tutorial](TUTORIAL.md)
- [Report an issue](https://github.com/canonical/maas-region-operator/issues)

The charm requires a PostgreSQL database through its `maas-db` relation. For a
controller that also provides rack services, set `enable_rack_mode=true`.

> [!IMPORTANT]
> The MAAS Agent charm is end-of-life. MAAS Region revisions after 211 no longer
> support the agent relation. Enable rack mode on MAAS Region to retain rack
> functionality; deployments that still depend on MAAS Agent require MAAS Region
> revision 211 or earlier.

## Development

The charm lives at the repository root: `charmcraft.yaml`, `pyproject.toml`,
`uv.lock`, and `tox.ini` are alongside `src/`, `lib/`, and `tests/`. Run all build
and test commands from this directory; there is no nested charm directory.

With Python 3.14, [uv](https://docs.astral.sh/uv/), and
[Charmcraft](https://canonical.com/juju/docs/charmcraft/) installed:

```shell
uv tool install tox --with tox-uv
tox run -e lint,unit,static,scenario
charmcraft pack
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for development, integration testing, and
publishing instructions.

## License

[GNU General Public License v3.0](LICENSE).
