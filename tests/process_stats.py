"""CPU time and resident set for the Terraform-owned Windows processes."""
import ctypes
import json
from ctypes import wintypes

from measure import STATE


class MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [
        (key, ctypes.c_size_t) for key in ("peak_rss", "rss", "peak_paged", "paged", "peak_nonpaged",
                                           "nonpaged", "pagefile", "peak_pagefile")]


def sample_processes(nodes):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    result = {}
    for node in nodes:
        record = json.loads((STATE / (node + ".json")).read_text(encoding="utf-8"))
        handle = kernel.OpenProcess(0x1000 | 0x0010, False, record["pid"])
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            birth, _, system, user = [(t.dwHighDateTime << 32) | t.dwLowDateTime for t in times]
            if birth != record["birth"]:
                raise RuntimeError("process identity changed during measurement")
            mem = MemoryCounters()
            mem.cb = ctypes.sizeof(mem)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(mem), mem.cb):
                raise ctypes.WinError(ctypes.get_last_error())
            result[node] = {"cpu_s": (system + user) / 10_000_000, "rss_mib": round(mem.rss / 2**20, 3)}
        finally:
            kernel.CloseHandle(handle)
    return result
