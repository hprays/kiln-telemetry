terraform {
  required_version = ">= 1.9.0, < 2.0.0"
  required_providers {
    shell = {
      source  = "scottwinkler/shell"
      version = "= 1.7.10"
    }
  }
}

provider "shell" {
  interpreter = [var.python_executable, "-B", abspath("${path.module}/../ops/lifecycle.py")]
}
