variable "proxmox_endpoint" {
  description = "Proxmox VE API endpoint, e.g. https://proxmox.example.com:8006"
  type        = string
}

variable "proxmox_api_token" {
  description = "Proxmox API token in the form '<user>@<realm>!<tokenname>=<secret>' (sensitive)."
  type        = string
  sensitive   = true
}

variable "proxmox_insecure" {
  description = "Skip TLS verification on Proxmox API. Set true only for self-signed certs."
  type        = bool
  default     = false
}

variable "template_vmid" {
  description = <<-EOT
    VMID of the source LXC template to clone from. Pre-create this once with
    worker autostart configured (or use deploy/proxmox/scaler/deploy/scripts/
    provision-node.sh which creates from scratch instead — Terraform is the
    "I have an existing template" path).
  EOT
  type        = number
}

variable "node_targets" {
  description = <<-EOT
    Map of Proxmox node name → desired LXC count and starting VMID for that
    node. Default cluster-wide is 1 worker per node ("each node runs one
    ESPHome build worker"). Bump count for beefier nodes; set count to 0 to
    skip a node entirely.

    Example:
      node_targets = {
        pve1       = { count = 1, first_vmid = 200 }
        pve-beast  = { count = 4, first_vmid = 300 }
        pve-tiny   = { count = 0, first_vmid = 400 }
      }
  EOT
  type = map(object({
    count      = number
    first_vmid = number
  }))
}

variable "hostname_prefix" {
  description = "Hostname prefix for cloned LXCs. Final hostname = <prefix>-<node>-<index>."
  type        = string
  default     = "esphome-worker"
}

variable "storage" {
  description = "Proxmox storage pool for LXC root disks."
  type        = string
  default     = "local-lvm"
}

variable "bridge" {
  description = "Linux bridge for LXC network."
  type        = string
  default     = "vmbr0"
}

variable "full_clone" {
  description = "Use full clones (independent storage) vs linked clones (shared base, faster)."
  type        = bool
  default     = true
}

variable "cores" {
  description = "vCPUs per LXC."
  type        = number
  default     = 2
}

variable "memory_mb" {
  description = "Memory per LXC, in MB."
  type        = number
  default     = 2048
}

variable "swap_mb" {
  description = "Swap per LXC, in MB. 0 disables swap."
  type        = number
  default     = 512
}

variable "worker_tag" {
  description = <<-EOT
    Discovery tag the scaler uses to identify its LXCs. Every LXC this module
    creates carries this tag, plus 'managed-by-terraform' so a human can
    spot the provenance in the Proxmox UI.
  EOT
  type        = string
  default     = "esphome-fleet-worker"
}

variable "extra_tags" {
  description = "Additional Proxmox tags applied to every LXC (alongside worker_tag)."
  type        = list(string)
  default     = ["managed-by-terraform"]
}
