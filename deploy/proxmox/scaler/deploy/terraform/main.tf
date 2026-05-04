provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token
  insecure  = var.proxmox_insecure
}

# Flatten { node1 = {count=2, first_vmid=200}, node2 = {count=1, first_vmid=300} }
# into a list of one element per LXC: [{node="node1", index=0, vmid=200}, {node="node1", index=1, vmid=201}, ...].
locals {
  pool_entries = flatten([
    for node, target in var.node_targets : [
      for i in range(target.count) : {
        node       = node
        index      = i
        vmid       = target.first_vmid + i
      }
    ]
  ])

  # Map keyed by vmid for the for_each below.
  pool_by_vmid = { for entry in local.pool_entries : tostring(entry.vmid) => entry }
}

resource "proxmox_virtual_environment_container" "worker" {
  for_each = local.pool_by_vmid

  node_name = each.value.node
  vm_id     = each.value.vmid
  tags      = concat([var.worker_tag], var.extra_tags)
  started   = false

  clone {
    vm_id = var.template_vmid
    full  = var.full_clone
  }

  initialization {
    hostname = "${var.hostname_prefix}-${each.value.node}-${each.value.index + 1}"

    ip_config {
      ipv4 {
        address = "dhcp"
      }
    }
  }

  cpu {
    cores = var.cores
  }

  memory {
    dedicated = var.memory_mb
    swap      = var.swap_mb
  }

  disk {
    datastore_id = var.storage
  }

  network_interface {
    name   = "eth0"
    bridge = var.bridge
  }

  startup {
    order = 1
  }
}
