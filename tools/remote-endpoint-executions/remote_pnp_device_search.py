#!/usr/bin/env python3
"""
remote_pnp_device_search.py

Purpose:
    Check whether a Plug and Play device is currently present on multiple
    authorized remote Windows hosts.

Created by:
    Avraham Makovsky

License:
    MIT

Why it exists:
    During support or lab work, it is often useful to know which machines
    currently detect a specific USB, network, serial, or debug device without
    opening Device Manager manually on every endpoint.

Requirements:
    - Windows machine running this script
    - Python 3
    - PowerShell
    - PowerShell Remoting / WinRM enabled on target hosts
    - Permission to query the target hosts

Output:
    CSV with one row per matched device, or one status row when no match/error.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List


POWERSHELL_EXE = "powershell.exe"
DEFAULT_TIMEOUT_SECONDS = 90
DEFAULT_MAX_WORKERS = 12

RESULT_COLUMNS = [
    "Host",
    "Identifier_Found",
    "Match_Count",
    "Matched_Device_Name",
    "Device_Class",
    "Present",
    "Device_Status",
    "InstanceId",
    "Problem",
    "Query_Error",
]


REMOTE_PNP_SEARCH_PS = r"""
param(
    [Parameter(Mandatory = $true)]
    [string]$ComputerName,

    [Parameter(Mandatory = $true)]
    [string]$Identifier
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$scriptBlock = {
    param([string]$Identifier)

    $ErrorActionPreference = "Stop"

    $devices = @(
        Get-PnpDevice -PresentOnly | Where-Object {
            $_.FriendlyName -and ($_.FriendlyName.IndexOf($Identifier, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
        } | Select-Object Status, Class, FriendlyName, InstanceId, Problem, Present
    )

    ConvertTo-Json -InputObject $devices -Compress -Depth 4
}

Invoke-Command -ComputerName $ComputerName -ScriptBlock $scriptBlock -ArgumentList $Identifier
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search authorized remote Windows hosts for a present Plug and Play device."
    )

    parser.add_argument("--identifier", "-i", help="Text to search in Device Manager FriendlyName. Example: ASIX")
    parser.add_argument("--hosts", help="Text file containing one host per line.")
    parser.add_argument("--notepad", action="store_true", help="Open Notepad to paste hosts. Windows only.")
    parser.add_argument("--output", "-o", help="Output CSV path.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Timeout per host in seconds.")
    parser.add_argument("--workers", type=int, default=DEFAULT_MAX_WORKERS, help="Maximum parallel workers.")

    return parser.parse_args()


def require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("This tool is intended to run on Windows.")


def create_temp_powershell_script() -> str:
    """Write the embedded PowerShell helper to a temporary script file."""
    fd, path = tempfile.mkstemp(prefix="remote_pnp_search_", suffix=".ps1", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(REMOTE_PNP_SEARCH_PS)
    return path


def load_hosts_from_file(path: str) -> List[str]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return parse_hosts(text)


def load_hosts_from_notepad() -> List[str]:
    """Collect hostnames through Notepad for quick ad-hoc use."""
    require_windows()

    temp_path = None

    try:
        fd, temp_path = tempfile.mkstemp(prefix="remote_hosts_", suffix=".txt", text=True)
        os.close(fd)

        Path(temp_path).write_text(
            "# Paste one host per line.\n"
            "# Lines starting with # or ; are ignored.\n"
            "LAB-PC-001\n"
            "LAB-PC-002\n",
            encoding="utf-8",
        )

        subprocess.run(["notepad.exe", temp_path], check=False)

        return load_hosts_from_file(temp_path)

    finally:
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass


def parse_hosts(text: str) -> List[str]:
    """Parse hostnames from lines, spaces, or commas while preserving order."""
    hosts = []
    seen = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or line.startswith(";"):
            continue

        for item in line.replace(",", " ").split():
            host = item.strip()

            if not host or host.startswith("#") or host.startswith(";"):
                continue

            key = host.lower()

            if key not in seen:
                seen.add(key)
                hosts.append(host)

    return hosts


def run_pnp_query(host: str, identifier: str, ps_script: str, timeout: int) -> List[Dict[str, str]]:
    """Query one host and normalize success, no-match, timeout, and error results."""
    base = {
        "Host": host,
        "Identifier_Found": "False",
        "Match_Count": "0",
        "Matched_Device_Name": "",
        "Device_Class": "",
        "Present": "",
        "Device_Status": "",
        "InstanceId": "",
        "Problem": "",
        "Query_Error": "",
    }

    cmd = [
        POWERSHELL_EXE,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        ps_script,
        "-ComputerName",
        host,
        "-Identifier",
        identifier,
    ]

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    except subprocess.TimeoutExpired:
        row = dict(base)
        row["Query_Error"] = f"Timeout after {timeout} seconds"
        return [row]

    except Exception as exc:
        row = dict(base)
        row["Query_Error"] = f"Local execution error: {exc}"
        return [row]

    if completed.returncode != 0:
        row = dict(base)
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        row["Query_Error"] = stderr or stdout or f"PowerShell exit code {completed.returncode}"
        return [row]

    output = (completed.stdout or "").strip()

    if not output:
        return [base]

    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        row = dict(base)
        row["Query_Error"] = "Could not parse PowerShell JSON output"
        return [row]

    if isinstance(parsed, dict):
        devices = [parsed]
    elif isinstance(parsed, list):
        devices = parsed
    else:
        devices = []

    if not devices:
        return [base]

    rows = []

    for device in devices:
        row = dict(base)
        row["Identifier_Found"] = "True"
        row["Match_Count"] = str(len(devices))
        row["Matched_Device_Name"] = str(device.get("FriendlyName") or "")
        row["Device_Class"] = str(device.get("Class") or "")
        row["Present"] = str(device.get("Present") or "")
        row["Device_Status"] = str(device.get("Status") or "")
        row["InstanceId"] = str(device.get("InstanceId") or "")
        row["Problem"] = str(device.get("Problem") or "")
        rows.append(row)

    return rows


def default_output_path() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"remote_pnp_device_search_{stamp}.csv"


def write_csv(path: str, rows: Iterable[Dict[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in RESULT_COLUMNS})


def print_summary(rows: List[Dict[str, str]]) -> None:
    by_host = {}
    for row in rows:
        by_host.setdefault(row["Host"], []).append(row)

    found_hosts = sorted({row["Host"] for row in rows if row["Identifier_Found"] == "True"})
    error_hosts = sorted({row["Host"] for row in rows if row["Query_Error"]})

    print()
    print("Summary")
    print("-------")
    print(f"Hosts checked: {len(by_host)}")
    print(f"Hosts with matches: {len(found_hosts)}")
    print(f"Hosts with errors: {len(error_hosts)}")

    if found_hosts:
        print()
        print("Matches:")
        for host in found_hosts:
            print(f"- {host}")

    if error_hosts:
        print()
        print("Errors:")
        for host in error_hosts:
            print(f"- {host}")


def main() -> int:
    args = parse_args()
    require_windows()

    identifier = args.identifier or input("Device FriendlyName identifier: ").strip()

    if not identifier:
        print("No identifier provided.", file=sys.stderr)
        return 1

    if args.hosts:
        hosts = load_hosts_from_file(args.hosts)
    elif args.notepad or sys.stdin.isatty():
        hosts = load_hosts_from_notepad()
    else:
        hosts = parse_hosts(sys.stdin.read())

    if not hosts:
        print("No hosts provided.", file=sys.stderr)
        return 1

    ps_script = create_temp_powershell_script()
    all_rows: List[Dict[str, str]] = []

    try:
        workers = max(1, min(args.workers, 64))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(run_pnp_query, host, identifier, ps_script, args.timeout): host
                for host in hosts
            }

            for index, future in enumerate(as_completed(future_map), start=1):
                host = future_map[future]
                print(f"[{index}/{len(hosts)}] {host}")
                all_rows.extend(future.result())

    finally:
        try:
            Path(ps_script).unlink(missing_ok=True)
        except OSError:
            pass

    output_path = args.output or default_output_path()
    write_csv(output_path, all_rows)
    print_summary(all_rows)
    print()
    print(f"Saved results to: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
