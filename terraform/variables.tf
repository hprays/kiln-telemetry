variable "python_executable" {
  type        = string
  description = "Python 3.10+ executable on the Windows host."
  default     = "python"
}

variable "ingress_port" {
  type        = number
  description = "Loopback gateway port. API replicas use the subsequent ports."
  default     = 18340
  validation {
    condition     = floor(var.ingress_port) == var.ingress_port && var.ingress_port >= 1024 && var.ingress_port <= 65532
    error_message = "Choose an integer from 1024 to 65532 to leave room for three API ports."
  }
}

variable "replicas" {
  type        = number
  description = "Number of API processes on this host."
  default     = 2
  validation {
    condition     = contains([1, 2, 3], var.replicas)
    error_message = "Use 1, 2 or 3 API processes."
  }
}

variable "cache_seconds" {
  type        = number
  description = "Maximum age in seconds for a cached summary. Use zero for the baseline."
  default     = 4
  validation {
    condition     = var.cache_seconds >= 0 && var.cache_seconds <= 30
    error_message = "Use a TTL between 0 and 30 seconds."
  }
}
