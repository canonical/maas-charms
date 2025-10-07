<!--
Avoid using this README file for information that is maintained or published elsewhere, e.g.:

* metadata.yaml > published on Charmhub
* documentation > published on (or linked to from) Charmhub
* detailed contribution guide > documentation or CONTRIBUTING.md

Use links instead.
-->

# maas-agent

Charmhub package name: rack-template
More information: <https://charmhub.io/maas-agent>

Describe your charm in one or two sentences.

> [!Note]
> The MAAS Agent charm is now considered end of life, and further updates will be stopped.
> To maintain functionality, the MAAS Region charm can now be deployed in [Region+Rack](https://charmhub.io/maas-region/docs/setting-up-and-configuring-charmed-maas) mode by supplying `enable_rack_mode=true` as a configuration option.

For any use cases still dependent on MAAS Agent, You will need to deploy alongside MAAS Region Revision 211 or below, as all revisions afterwards have had the relation logic removed.

## Other resources

<!-- If your charm is documented somewhere else other than Charmhub, provide a link separately. -->

- [Read more](https://example.com)

- [Contributing](CONTRIBUTING.md) <!-- or link to other contribution documentation -->

- See the [Juju SDK documentation](https://juju.is/docs/sdk) for more information about developing and improving charms.
