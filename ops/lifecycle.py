"""Windows lifecycle adapter for the Terraform shell provider; use stop-api for the S1 drill."""

import csv
import ctypes
import http.client
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
APP = PROJECT / "app"
ROOT = PROJECT / "terraform" / ".kiln-local"
DB = ROOT / "data.sqlite3"
TOKEN = ROOT / "credential" / "sensor.token"
BACKUP = ROOT / "backups" / "checkpoint.sqlite3"
MARK = ".kiln-owner"
APIS = {"kiln-1", "kiln-2", "kiln-3"}


def guarded(path):
    if not path.is_relative_to(ROOT) or path.resolve() != path:
        raise RuntimeError("runtime path must remain within the managed directory")
    # Reject NTFS reparse points, including junctions, on each managed segment.
    for item in [path, *path.parents]:
        if item == ROOT.parent:
            break
        if item.exists() and item.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError("reparse points are not allowed in the runtime directory")
    return path


def owns(directory):
    marker = guarded(directory / MARK)
    if not directory.exists():
        return False
    if not marker.is_file() or marker.read_text(encoding="ascii") != "kiln-local-v1\n":
        raise RuntimeError("a runtime directory without the KILN ownership marker exists")
    return True


def limit_access(directory):
    # Grant the current Windows account and LocalSystem access before writing secrets.
    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                            capture_output=True, check=True)
    sid = next(csv.reader([result.stdout.decode(errors="replace").strip()]))[-1]
    if not re.fullmatch(r"S-\d+(?:-\d+)+", sid):
        raise RuntimeError("could not resolve the current Windows security identifier")
    result = subprocess.run(["icacls", str(directory), "/inheritance:r", "/grant:r",
                             f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                            capture_output=True)
    if result.returncode:
        raise RuntimeError("could not restrict the managed directory ACL")


def make_directory(directory):
    if owns(directory):
        return
    directory.mkdir()
    try:
        if directory == ROOT:
            limit_access(directory)
        (directory / MARK).write_text("kiln-local-v1\n", encoding="ascii")
    except Exception:
        if not any(directory.iterdir()):
            directory.rmdir()
        raise


def remove_directory(directory, file_names):
    if not owns(directory):
        return
    expected = set(file_names) | {MARK}
    children = list(directory.iterdir())
    if any(child.name not in expected or not guarded(child).is_file() for child in children):
        raise RuntimeError("unexpected runtime entries; inspect them before destroying this resource")
    for name in file_names:
        guarded(directory / name).unlink(missing_ok=True)
    guarded(directory / MARK).unlink()
    directory.rmdir()


def app_command(*args):
    result = subprocess.run([sys.executable, "-B", str(APP / "server.py"), *map(str, args)],
                            capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError("application storage operation failed: " + str(args[0]))


def directory_resource(kind, action):
    directory = {"store": ROOT, "credential": TOKEN.parent, "backups": BACKUP.parent}[kind]
    file_names = {
        "store": ["data.sqlite3", "data.sqlite3-wal", "data.sqlite3-shm", "data.sqlite3-journal"],
        "backups": ["checkpoint.sqlite3", "checkpoint.sqlite3-wal", "checkpoint.sqlite3-shm",
                    "checkpoint.sqlite3-journal"],
        "credential": ["sensor.token"],
    }[kind]
    if action == "delete":
        if kind == "store" and owns(ROOT):
            # Refresh forgets stopped processes before Terraform calls their delete hooks.
            # Reclaim only our known process files and only after checking ownership.
            for name in sorted(APIS | {"kiln-gateway"}):
                record = process_record(name)
                metadata = read_record(record)
                if metadata:
                    if metadata["name"] != name:
                        raise RuntimeError("process metadata does not match its resource name")
                    if process_identity(metadata["pid"], metadata["birth"]) is not None:
                        raise RuntimeError("a managed server is still running during store cleanup")
                file_names.extend([record.name, name + ".log"])
        remove_directory(directory, file_names)
        return None
    if action in {"create", "update"}:
        make_directory(directory)
        if kind == "store":
            app_command("seed", "--db", guarded(DB))
        elif kind == "credential":
            app_command("token", "--file", guarded(TOKEN))
    if not owns(directory):
        return None
    if kind == "store":
        if not guarded(DB).is_file():
            return None
        app_command("status", "--db", DB)
    elif kind == "credential" and not guarded(TOKEN).is_file():
        return None
    return {"resource": kind, "managed": "true"}


kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel.WaitForSingleObject.restype = wintypes.DWORD


def process_identity(pid, birth=None, stop=False):
    # The same OS handle is checked and stopped; a recycled PID cannot be terminated.
    rights = 0x00100000 | 0x1000 | (0x0001 if stop else 0)  # SYNCHRONIZE | QUERY_LIMITED | TERMINATE
    handle = kernel.OpenProcess(rights, False, pid)
    if not handle:
        if ctypes.get_last_error() in {87, 1168}:
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle, 0) != 258:  # WAIT_TIMEOUT means still running.
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        timestamp = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        if birth is not None and timestamp != birth:
            return None
        if stop:
            if birth is None:
                raise RuntimeError("refusing to stop a process without an ownership fingerprint")
            if not kernel.TerminateProcess(handle, 0) or kernel.WaitForSingleObject(handle, 4000) != 0:
                raise RuntimeError("owned process failed to terminate")
        return timestamp
    finally:
        kernel.CloseHandle(handle)


def process_record(name):
    if name not in APIS | {"kiln-gateway"}:
        raise RuntimeError("unknown process resource")
    return guarded(ROOT / f"{name}.json")


def read_record(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_record(path, data):
    temporary = guarded(path.with_suffix("." + uuid.uuid4().hex + ".tmp"))
    try:
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def healthy(port):
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=0.3)
    try:
        client.request("GET", "/health", headers={"Connection": "close"})
        response = client.getresponse()
        response.read(16384)
        return response.status == 200
    except (http.client.HTTPException, OSError):
        return False
    finally:
        client.close()


def spawn(name, spec, path):
    port = int(spec["PORT"])
    if spec["BIND_HOST"] != "127.0.0.1":
        raise RuntimeError("only the loopback network is supported")
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("127.0.0.1", port))
    if name == "kiln-gateway":
        command = [APP / "gateway.py", "--port", str(port), "--backends", spec["BACKENDS"]]
    else:
        command = [APP / "server.py", "serve", "--db", DB, "--port", str(port), "--node", name,
                   "--cache-seconds", spec["CACHE_SECONDS"], "--token-file", TOKEN]
    env = {key: os.environ[key] for key in ["SystemRoot", "WINDIR", "PATH", "TEMP", "TMP"]
           if key in os.environ}
    with guarded(ROOT / (name + ".log")).open("ab") as log:
        process = subprocess.Popen([sys.executable, "-B", *map(str, command)], env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
    try:
        birth = process_identity(process.pid)
        if birth is None:
            raise RuntimeError("server exited before its identity could be recorded")
        write_record(path, {"pid": process.pid, "birth": birth, "name": name, "spec": spec})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process.poll() is None:
            if healthy(port):
                return
            time.sleep(0.1)
        raise RuntimeError(f"{name} did not pass its readiness check; inspect the managed log")
    except Exception:
        if process.poll() is None:
            process.terminate()  # Child handle, never a PID discovered by port.
        process.wait(timeout=4)
        raise


def process_resource(name, action, spec=None):
    if not owns(ROOT):
        if action in {"delete", "read"}:
            return None
        raise RuntimeError("data store must exist before starting servers")
    path = process_record(name)
    old = read_record(path)
    if old and old["name"] != name:
        raise RuntimeError("process record does not match the named resource")
    live = old and process_identity(old["pid"], old["birth"]) is not None
    if action in {"delete", "stop-api"}:
        if live:
            process_identity(old["pid"], old["birth"], stop=True)
        if action == "delete":
            path.unlink(missing_ok=True)
            guarded(ROOT / (name + ".log")).unlink(missing_ok=True)
        return None
    if action == "read":
        if not live:
            return None  # No stdout tells the provider to plan recreation.
    else:
        if action == "start-api":
            if not old:
                raise RuntimeError("no deployment specification is recorded")
            spec = old["spec"]
        if live and spec != old["spec"]:
            process_identity(old["pid"], old["birth"], stop=True)
            live = False
        if not live:
            spawn(name, spec, path)
    return {"resource": name, "managed": "true"}


def main():
    action = sys.argv[1]
    if action in {"start-api", "stop-api", "status-api"}:
        if len(sys.argv) != 3 or sys.argv[2] not in APIS:
            raise ValueError("select kiln-1, kiln-2 or kiln-3")
        name = sys.argv[2]
        if action == "status-api":
            state = process_resource(name, "read")
            print(json.dumps({"name": name, "running": bool(state)}))
        else:
            process_resource(name, action)
            print(json.dumps({"name": name, "operation": action}))
        return
    if action not in {"create", "read", "update", "delete"}:
        raise ValueError("unsupported lifecycle operation")
    kind, name = os.environ["KIND"], os.environ["NAME"]
    if kind in {"store", "backups", "credential"}:
        result = directory_resource(kind, action)
    elif kind in {"api", "gateway"}:
        keys = ["PORT", "BIND_HOST", "SOURCE_HASH", "HELPER_HASH"]
        keys += ["BACKENDS"] if kind == "gateway" else ["CACHE_SECONDS"]
        result = process_resource(name, action, {key: os.environ[key] for key in keys})
    else:
        raise ValueError("unsupported resource kind")
    if result is not None:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
