"""Stdio MCP facade: one native backend per request, reaped after its response.

Only initialization is replayed. A failed tool call is returned as an error and
is NEVER replayed, since tools can have side effects. Client connection stays
available after backend timeout, EOF, or process failure.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time


def reader(stream, events, origin):
    try:
        for line in stream:
            events.put((origin, json.loads(line)))
    except (ValueError, OSError) as exc:
        events.put((origin, {"_read_error": str(exc)}))
    finally:
        events.put((origin, None))


def stop(process):
    """EOF first, then bounded process-tree termination even if parent is alive."""
    if process.stdin:
        with __import__('contextlib').suppress(OSError):
            process.stdin.close()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    # POSIX groups survive a parent exit. Windows uses a kill-on-close Job below.
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


class WindowsJob:
    """Assign before any RPC so every spawned Git process dies with its backend."""
    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not self.api.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "cannot contain MCP backend process tree")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def resume(self, process):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL("ntdll")
            api.NtResumeProcess.argtypes = [wintypes.HANDLE]
            api.NtResumeProcess.restype = ctypes.c_long
            if api.NtResumeProcess(wintypes.HANDLE(int(process._handle))) != 0:
                raise OSError("cannot resume contained MCP backend")


def send(stream, message):
    stream.write(json.dumps(message, ensure_ascii=False) + "\n")
    stream.flush()


class Proxy:
    def __init__(self, command, *, startup=30.0, query=60.0, indexing=900.0):
        self.command, self.startup, self.query, self.indexing = command, startup, query, indexing
        self.events = queue.Queue()
        self.deferred = deque()
        self.initialize = None
        self.sequence = 0

    def transaction(self, message):
        process, job = None, None
        self.sequence += 1
        origin = self.sequence
        try:
            process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=sys.stderr, text=True, encoding="utf-8", bufsize=1,
                                       start_new_session=os.name != "nt",
                                       creationflags=0x4 if os.name == "nt" else 0)
            job = WindowsJob(process)
            job.resume(process)
            threading.Thread(target=reader, args=(process.stdout, self.events, origin), daemon=True).start()
            initialization = message.get("method") == "initialize"
            internal_id = "harness-initialize-" + str(origin)
            send(process.stdin, message if initialization else {**self.initialize, "id": internal_id})
            ready = initialization
            timeout = self.indexing if message.get("params", {}).get("name") == "index_repository" else self.query
            deadline = time.monotonic() + self.startup
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("MCP backend deadline exceeded; request was not retried")
                try:
                    source, value = self.events.get(timeout=remaining)
                except queue.Empty:
                    raise TimeoutError("MCP backend deadline exceeded; request was not retried")
                if source == "client":
                    if value is None:
                        raise EOFError("MCP client disconnected")
                    if value.get("_read_error"):
                        raise ValueError(value["_read_error"])
                    # Responses to backend requests and cancellation must flow while a call runs.
                    if "method" not in value or value.get("method") == "notifications/cancelled":
                        send(process.stdin, value)
                    else:
                        self.deferred.append(value)
                    continue
                if source != origin:
                    continue  # A prior backend's terminal reader event.
                if value is None:
                    raise RuntimeError("MCP backend exited before its response")
                if value.get("_read_error"):
                    raise ValueError(value["_read_error"])
                if not ready and value.get("id") == internal_id:
                    if "error" in value:
                        raise RuntimeError("MCP backend initialization failed")
                    send(process.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                    send(process.stdin, message)
                    ready = True
                    deadline = time.monotonic() + timeout
                    continue
                send(sys.stdout, value)
                if ready and value.get("id") == message.get("id") and ("result" in value or "error" in value):
                    return
        except EOFError:
            raise
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            send(sys.stdout, {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32603, "message": str(exc)}})
        finally:
            if job:
                job.close()
            if process:
                stop(process)
                process.stdout.close()

    def serve(self):
        threading.Thread(target=reader, args=(sys.stdin, self.events, "client"), daemon=True).start()
        try:
            while True:
                if self.deferred:
                    value = self.deferred.popleft()
                else:
                    source, value = self.events.get()
                    if source != "client":
                        continue
                if value is None:
                    return
                if "id" not in value or "method" not in value:
                    continue
                if value.get("method") == "initialize":
                    self.initialize = value
                if self.initialize is None:
                    send(sys.stdout, {"jsonrpc": "2.0", "id": value["id"], "error": {"code": -32002, "message": "initialize required"}})
                    continue
                self.transaction(value)
        except (EOFError, BrokenPipeError):
            return


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup-seconds", type=float, default=30)
    parser.add_argument("--query-seconds", type=float, default=60)
    parser.add_argument("--index-seconds", type=float, default=900)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or min(args.startup_seconds, args.query_seconds, args.index_seconds) <= 0:
        parser.error("provide a backend command and positive deadlines")
    Proxy(command, startup=args.startup_seconds, query=args.query_seconds, indexing=args.index_seconds).serve()


if __name__ == "__main__":
    main()
