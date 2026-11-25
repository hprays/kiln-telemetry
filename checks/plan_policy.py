"""Validate network binding and process budget against a Terraform JSON plan."""
import argparse
import copy
import json
from pathlib import Path


def inspect(plan):
    resources = plan.get("planned_values", {}).get("root_module", {}).get("resources", [])
    apis = [r for r in resources if r["type"] == "shell_script" and r["name"] == "api"]
    gateways = [r for r in resources if r["type"] == "shell_script" and r["name"] == "gateway"]
    errors = []
    if not 1 <= len(apis) <= 3 or len(gateways) != 1:
        errors.append("topology-budget")
    ports = set()
    for r in gateways + apis:
        env = r["values"].get("environment") or {}
        if env.get("BIND_HOST") != "127.0.0.1":
            errors.append("loopback-only:" + r["address"])
        value = str(env.get("PORT", ""))
        if not value.isdecimal() or not 1024 <= int(value) <= 65535 or value in ports:
            errors.append("distinct-unprivileged-port:" + r["address"])
        ports.add(value)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    errors = inspect(plan)
    if errors:
        print("FAIL", *errors)
        return 1
    print("PASS loopback-only, distinct-unprivileged-port, topology-budget")
    if args.negative_controls:
        for attr, value, expected in [("BIND_HOST", "0.0.0.0", "loopback-only"),
                                      ("PORT", "80", "distinct-unprivileged-port")]:
            test = copy.deepcopy(plan)
            gateway = next(r for r in test["planned_values"]["root_module"]["resources"]
                           if r["name"] == "gateway")
            gateway["values"]["environment"][attr] = value
            caught = any(err.startswith(expected) for err in inspect(test))
            print(("PASS" if caught else "FAIL"), "rejected test plan for", expected)
            if not caught:
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
