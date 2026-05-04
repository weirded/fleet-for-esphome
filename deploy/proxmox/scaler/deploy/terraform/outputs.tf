output "vmids_by_node" {
  description = "Map of node → list of VMIDs the module created on that node."
  value = {
    for node in keys(var.node_targets) :
    node => sort([for entry in local.pool_entries : entry.vmid if entry.node == node])
  }
}

output "all_vmids" {
  description = "Flat list of every VMID provisioned across the cluster."
  value       = sort([for entry in local.pool_entries : entry.vmid])
}

output "scaler_env_snippet" {
  description = <<-EOT
    Drop-in snippet for the scaler's config.env. Includes WORKERS_PER_NODE
    derived from the most-common node count, with PER_NODE_OVERRIDES for
    the outliers. Paste into your config.env (Proxmox host + token still
    have to be filled in by hand — they aren't in Terraform state by design).
  EOT
  value = <<-EOT
    PROXMOX_SCALER_PROXMOX_HOST=${replace(replace(var.proxmox_endpoint, "https://", ""), "http://", "")}
    PROXMOX_SCALER_WORKER_TAG=${var.worker_tag}
    PROXMOX_SCALER_WORKERS_PER_NODE=1
    PROXMOX_SCALER_PER_NODE_OVERRIDES=${join(",", [
      for node, target in var.node_targets : "${node}:${target.count}"
      if target.count != 1
    ])}
    PROXMOX_SCALER_MIN_TOTAL_WORKERS=0
    PROXMOX_SCALER_TARGET_PER_WORKER=2
    PROXMOX_SCALER_POLL_INTERVAL=30
    PROXMOX_SCALER_COOLDOWN_SECONDS=600
  EOT
}
