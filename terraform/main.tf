locals {
  app_dir     = "${path.module}/../app"
  node_ports  = [for i in range(var.replicas) : var.ingress_port + i + 1]
  helper_hash = filesha256("${path.module}/../ops/lifecycle.py")
  source_files = sort([
    for file in fileset(local.app_dir, "*") : file
    if endswith(file, ".py") || endswith(file, ".html") || endswith(file, ".css") || endswith(file, ".js")
  ])
  app_hash = sha256(join("", [for file in local.source_files : filesha256("${local.app_dir}/${file}")]))
}

resource "shell_script" "store" {
  environment = {
    KIND = "store"
    NAME = "kiln-store"
  }
  lifecycle_commands {
    create = "create"
    read   = "read"
    update = "update"
    delete = "delete"
  }
}

resource "shell_script" "backups" {
  environment = {
    KIND = "backups"
    NAME = "kiln-backups"
  }
  lifecycle_commands {
    create = "create"
    read   = "read"
    update = "update"
    delete = "delete"
  }
  depends_on = [shell_script.store]
}

resource "shell_script" "credential" {
  environment = {
    KIND = "credential"
    NAME = "kiln-credential"
  }
  lifecycle_commands {
    create = "create"
    read   = "read"
    update = "update"
    delete = "delete"
  }
  depends_on = [shell_script.store]
}

resource "shell_script" "api" {
  count = var.replicas
  environment = {
    KIND          = "api"
    NAME          = "kiln-${count.index + 1}"
    BIND_HOST     = "127.0.0.1"
    PORT          = tostring(local.node_ports[count.index])
    CACHE_SECONDS = tostring(var.cache_seconds)
    SOURCE_HASH   = local.app_hash
    HELPER_HASH   = local.helper_hash
  }
  lifecycle_commands {
    create = "create"
    read   = "read"
    update = "update"
    delete = "delete"
  }
  lifecycle {
    replace_triggered_by = [shell_script.credential]
  }
  depends_on = [shell_script.store, shell_script.backups, shell_script.credential]
}

resource "shell_script" "gateway" {
  environment = {
    KIND        = "gateway"
    NAME        = "kiln-gateway"
    BIND_HOST   = "127.0.0.1"
    PORT        = tostring(var.ingress_port)
    BACKENDS    = join(",", local.node_ports)
    SOURCE_HASH = filesha256("${local.app_dir}/gateway.py")
    HELPER_HASH = local.helper_hash
  }
  lifecycle_commands {
    create = "create"
    read   = "read"
    update = "update"
    delete = "delete"
  }
  depends_on = [shell_script.api]
}
