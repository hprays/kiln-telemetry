output "ingress_port" {
  description = "Gateway port on loopback."
  value       = var.ingress_port
}

output "api_ports" {
  description = "Loopback ports for operator checks."
  value       = local.node_ports
}
