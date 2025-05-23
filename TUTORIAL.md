# MAAS Operator Charm LAB

## Environment

Create some environment variables to facilitate this tutorial

```bash
# LaunchPad ID
export LP_ID="my-lp-id"
export MAAS_REGION_CHARM=./maas-region/maas-region_ubuntu-24.04-amd64.charm
export MAAS_AGENT_CHARM=./maas-agent/maas-agent_ubuntu-24.04-amd64.charm
```

## Install required packages

```bash
sudo snap install juju
sudo snap install lxd
sudo snap install charmcraft --classic
```

Juju needs to be invoked once to initialize its local state, including a SSH
key that is required in the next steps.

```bash
# The actual command doesn't matter, just run juju once
juju whoami
```

## Setup LXD

### Create networks

Managed network for control plane

```shell
lxc network create jujulab
lxc network edit jujulab <<EOF
name: jujulab
description: "Juju lab network"
type: bridge
config:
  dns.domain: juju-lab
  ipv4.address: 10.70.0.1/24
  ipv4.dhcp: "true"
  ipv4.dhcp.ranges: 10.70.0.65-10.70.0.126
  ipv4.nat: "true"
  ipv6.address: none
EOF
```

Integrate the lab network with the host's DNS service

```shell
resolvectl dns jujulab 10.70.0.1
resolvectl domain jujulab '~juju-lab'
```

Make this integration persistent (optional)

```shell
cat <<EOF | sudo tee /etc/systemd/system/lxd-dns-net-juju.service
[Unit]
Description=LXD per-link DNS configuration for jujulab
BindsTo=sys-subsystem-net-devices-jujulab.device
After=sys-subsystem-net-devices-jujulab.device

[Service]
Type=oneshot
ExecStart=/usr/bin/resolvectl dns jujulab 10.70.0.1
ExecStart=/usr/bin/resolvectl domain jujulab '~juju-lab'
ExecStopPost=/usr/bin/resolvectl revert jujulab
RemainAfterExit=yes

[Install]
WantedBy=sys-subsystem-net-devices-jujulab.device
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now lxd-dns-net-juju
```

Unmanaged network for Devices-Under-Test (DUT)

```shell
lxc network create jujudata
lxc network edit jujudata <<EOF
name: jujudata
description: "Juju test network"
type: bridge
config:
  dns.domain: jujudata
  ipv4.address: 10.70.1.1/24
  ipv4.dhcp: "false"
  ipv4.nat: "false"
  ipv6.address: none
EOF
```

### Create project and profiles

Create a Project to isolate our lab.
> We don't need a *Separate set of images for the project*,
> so we disable it to save some disk space and download time.

```shell
lxc project create juju-lab -c features.images=false
lxc project switch juju-lab
```

Profile for Juju controllers (2 NICs)

```shell
lxc profile create juju-host
lxc profile edit juju-host <<EOF
name: juju-host
description: Juju host
config:
    limits.cpu: 2
    limits.memory: 4GB
    user.user-data: |
        #cloud-config
        ssh_authorized_keys:
        - $(cat ${HOME}/.ssh/id_rsa.pub | cut -d' ' -f1-2)
        - $(cat ${HOME}/.local/share/juju/ssh/juju_id_rsa.pub | cut -d' ' -f1-2)
devices:
    eth0:
        type: nic
        name: eth0
        network: jujulab
    eth1:
        type: nic
        name: eth1
        network: jujudata
    root:
        path: /
        pool: default
        type: disk
EOF
```

Profile for DUTs (1 NIC)

```shell
lxc profile create juju-dut
lxc profile edit juju-dut <<EOF
name: juju-dut
description: Juju lab DUT
devices:
    eth0:
        type: nic
        name: eth0
        network: jujudata
    root:
        path: /
        pool: default
        type: disk
EOF
```

### Create VMs

```shell
for h in $(seq 1 3); do \
    lxc launch ubuntu:noble "m$h" --vm -p juju-host;\
done;\
sleep 5;\
for h in $(seq 1 3); do \
    lxc exec "m$h" -- cloud-init status --wait;\
    ssh-keyscan -H "m$h.juju-lab" >> ~/.ssh/known_hosts;\
done
```

## Bootstrap Juju controller

Bootstrap cloud

```shell
cat >| maas-bootstrap.yaml <<EOF
clouds:
    maas-bootstrap:
        type: manual
        endpoint: ubuntu@m1.juju-lab
        regions:
            default: {}
EOF

juju add-cloud maas-bootstrap ./maas-bootstrap.yaml
juju bootstrap maas-bootstrap maas-controller
juju add-machine -m controller ssh:ubuntu@m2.juju-lab
juju add-machine -m controller ssh:ubuntu@m3.juju-lab
juju enable-ha -n 3 --to 1,2
juju controllers --refresh
```

## Install Postgres DB

Deploy DB using the charm

```shell
juju deploy -m controller postgresql --channel 16/beta --series noble --to 0
juju add-unit -m controller postgresql -n 2 --to 1,2
```

## Install HAProxy

Deploy HAProxy using the charm

```shell
juju deploy -m controller haproxy --series noble --to 0
juju add-unit -m controller haproxy -n 2 --to 1,2
```

## Install MAAS

Deploy Region using the charm

```shell
juju deploy -m controller ${MAAS_REGION_CHARM} --to 0
juju status --watch 10s  # wait for it to initialize
```

Consume offers

```shell
juju integrate -m controller maas-region postgresql
juju integrate -m controller maas-region haproxy
juju status --watch 10s  # wait for it to settle
```

Create an Admin user

```shell
juju run -m controller maas-region/leader create-admin username=maas password=maas email=maas@example.com ssh-import=lp:${LP_ID}
```

Deploy Rack/Agent using the charm

```shell
juju deploy -m controller ${MAAS_AGENT_CHARM} --to 1
juju integrate -m controller maas-agent maas-region
juju add-unit -m controller maas-agent --to 2
```

Get the MAAS URL

```shell
juju run -m controller maas-region/leader get-api-endpoint
```
