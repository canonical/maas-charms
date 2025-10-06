<!--
Avoid using this README file for information that is maintained or published elsewhere, e.g.:

* metadata.yaml > published on Charmhub
* documentation > published on (or linked to from) Charmhub
* detailed contribution guide > documentation or CONTRIBUTING.md

Use links instead.
-->

# maas-region

Charmhub package name: operator-template
More information: <https://charmhub.io/maas-region>

Describe your charm in one or two sentences.

[!NOTE] As of MAAS-Region latest/edge rev 212, logic for interacting with the MAAS Agent charm has been removed, and MAAS-Agent end-of-life.
To maintain functionality, the recommendation is to deploy MAAS Region with `enable_rack_mode=true` configuration set.

For any use cases still dependent on MAAS Agent, you will need to deploy alongside MAAS Region Revision 211 or below, as all revisions afterwards have had the relation logic removed.

## Other resources

<!-- If your charm is documented somewhere else other than Charmhub, provide a link separately. -->

- [Read more](https://example.com)

- [Contributing](CONTRIBUTING.md) <!-- or link to other contribution documentation -->

- See the [Juju SDK documentation](https://juju.is/docs/sdk) for more information about developing and improving charms.
