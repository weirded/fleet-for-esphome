terraform {
  required_version = ">= 1.5"

  required_providers {
    proxmox = {
      # bpg/proxmox is the actively-maintained provider as of 2026.
      # https://registry.terraform.io/providers/bpg/proxmox/latest/docs
      source  = "bpg/proxmox"
      version = "~> 0.66"
    }
  }
}
