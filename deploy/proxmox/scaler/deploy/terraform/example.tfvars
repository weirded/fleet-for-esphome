proxmox_endpoint  = "https://proxmox.example.com:8006"
proxmox_api_token = "terraform@pve!provisioner=00000000-0000-0000-0000-000000000000"  # sensitive
proxmox_insecure  = false  # flip true if your Proxmox uses a self-signed cert

template_vmid = 900

# Default: one worker per node. Add the nodes in your cluster here.
# Override count for beefy/tiny nodes individually.
node_targets = {
  pve1 = {
    count      = 1
    first_vmid = 200
  }
  pve-beast = {
    count      = 4
    first_vmid = 300
  }
  pve-tiny = {
    count      = 0  # skip this node entirely
    first_vmid = 400
  }
}

hostname_prefix = "esphome-worker"
storage         = "local-zfs"
bridge          = "vmbr0"
full_clone      = true
cores           = 2
memory_mb       = 2048
swap_mb         = 512

worker_tag = "esphome-fleet-worker"
extra_tags = ["managed-by-terraform"]
