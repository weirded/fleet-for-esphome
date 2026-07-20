# Terraform module — multi-node Proxmox LXC worker pool

Provisions LXC workers across **multiple Proxmox nodes**, with a per-node count. Default is `count = 1` per node ("each node runs one ESPHome build worker"). Override per-node for beefier hosts (`count = 4`) or to skip lightweight ones (`count = 0`).

`terraform apply` is idempotent: bumping `count` for a node adds clones; lowering removes the highest-numbered ones; adding a node creates its pool.

## Prerequisites

1. **A pre-built LXC template** referenced by `template_vmid`. The scaler doesn't customize the OS — whatever the template has (Debian/Ubuntu + Docker + the worker container autostarted with `SERVER_URL` / `SERVER_TOKEN` env baked in) is what every clone inherits.
   - Don't have a template? Use `deploy/proxmox/scaler/deploy/scripts/provision-node.sh` instead — it creates LXCs from scratch (no template needed) and is the "no-Terraform" path.
2. **A Proxmox API token** with `VM.Allocate`, `VM.Config.*`, `VM.PowerMgmt`, `Datastore.AllocateSpace` on the relevant containers/storage. Format: `<user>@<realm>!<tokenname>=<secret>`. Goes in `proxmox_api_token`.

## Use

```bash
cd deploy/proxmox/scaler/deploy/terraform
cp example.tfvars terraform.tfvars   # then fill in real values
terraform init
terraform plan
terraform apply
```

After apply:

```bash
terraform output scaler_env_snippet
```

Paste that into your scaler's `config.env`. Add the `PROXMOX_SCALER_FLEET_*` and `PROXMOX_SCALER_PROXMOX_TOKEN_*` lines yourself — they aren't in Terraform state by design.

## node_targets — examples

**Homogeneous cluster, 1 worker per node (the default):**

```hcl
node_targets = {
  pve1 = { count = 1, first_vmid = 200 }
  pve2 = { count = 1, first_vmid = 210 }
  pve3 = { count = 1, first_vmid = 220 }
}
```

**Mixed cluster, beefy node gets more workers:**

```hcl
node_targets = {
  pve-tiny  = { count = 1, first_vmid = 200 }
  pve-beast = { count = 4, first_vmid = 300 }   # 4 parallel workers on the powerful node
}
```

**Quarantine a node** (e.g. taken offline for maintenance) without removing its definition:

```hcl
node_targets = {
  pve1     = { count = 1, first_vmid = 200 }
  pve-old  = { count = 0, first_vmid = 300 }   # stays in state, no LXCs provisioned
}
```

## Resizing

- **Grow a node**: bump `node_targets["X"].count`, run `terraform apply`. New VMIDs (`first_vmid + count_old` through `first_vmid + count_new - 1`) get cloned.
- **Shrink a node**: lower `count`, run `terraform apply`. Highest-numbered VMIDs are destroyed — make sure they're stopped first (the scaler should already have stopped them if `min_total_workers` permits).
- **Add a node**: add a new entry. Don't reuse `first_vmid` ranges across nodes (Proxmox VMIDs are cluster-unique).

## Why a separate `first_vmid` per node?

VMIDs are **cluster-unique** in Proxmox VE. The module needs distinct ranges per node so it can grow each node's pool without colliding with another node's pool. Picking ranges by hand makes operator intent visible (`pve-beast: 300-303`) instead of having Terraform allocate VMIDs opaquely.

## What this module does NOT do

- **Provision the template.** That's a one-time operator task; baking it via Terraform bloats the module and means worker-image updates need Terraform runs.
- **Run the scaler.** Use the Dockerfile or systemd unit at `deploy/proxmox/scaler/`.
- **Create API tokens or RBAC.** Provider needs a token to authenticate; can't bootstrap its own auth.
- **Manage per-LXC worker config.** That lives in the template; rotating the Fleet token means re-baking the template (or running `provision-node.sh` again with a new token).

## Provider notes

`bpg/proxmox` `~> 0.66`. Actively maintained, first-class LXC clone support.
