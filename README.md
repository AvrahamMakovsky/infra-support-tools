# infra-support-tools

Small tools for infrastructure support, endpoint operations, and everyday troubleshooting.

These started as personal utilities for day-to-day tasks I wanted to make faster, clearer, or easier to repeat. I cleaned them up and shared them publicly in case they are useful to others.

## Structure

```text
tools/
├─ workstation-tools/
├─ remote-endpoint-executions/
├─ local-endpoint-executions/
└─ automation-runners/
```

## Tool groups

### workstation-tools

Tools I usually run from my own laptop/workstation.

These are mostly helpers for preparing input, opening quick diagnostic sessions, parsing text, or automating local workflow steps.

### remote-endpoint-executions

Tools used from my workstation against authorized remote endpoints.

These are useful when checking several machines, collecting support evidence, or running same-domain administrative checks.

### local-endpoint-executions

Tools intended to run directly on the target machine.

These are usually local maintenance helpers, health checks, or user-facing notifications.

### automation-runners

Tools for recurring local automation, scheduled script runs, dashboards, and run history.

These are useful when a task is more than a one-shot script, but still should stay local/internal rather than becoming an internet-facing service.

## Tools

| Tool | Group | Purpose |
|---|---|---|
| `bulk-ping-launcher.cmd` | workstation-tools | Open multiple ping sessions from a pasted host list. |
| `bulk-vnc-launcher.cmd` | workstation-tools | Open multiple VNC sessions from a pasted host list. |
| `hostname_extractor.py` | workstation-tools | Extract hostnames from copied text, logs, or ticket titles. |
| `spreadsheet_work_item_flow.py` | workstation-tools | Turn spreadsheet rows into reviewed work-item proposals. |
| `Update-OfflineEndpointIdentity.ps1` | workstation-tools | Update an offline Windows hostname identity in the registry and optional EFI file. |
| `remote_pnp_device_search.py` | remote-endpoint-executions | Check present Plug and Play devices on remote Windows hosts. |
| `remote_registry_value_reader.py` | remote-endpoint-executions | Read one registry value from multiple remote Windows hosts. |
| `Show-RebuildReminder.ps1` | local-endpoint-executions | Show a clear fullscreen reminder before maintenance. |
| `Get-SSDHealth.ps1` | local-endpoint-executions | Show local SSD health information using smartctl. |
| `reset-network-and-reboot.cmd` | local-endpoint-executions | Reset local network state and reboot. Use carefully. |
| `local_task_runner.py` | automation-runners | Local dashboard for recurring script runs and run history. |

## Notes

These are personal projects, not official tools of any employer.

Public examples use synthetic data. Workplace-specific names, domains, tickets, logs, exports, and screenshots are removed or generalized.

## Related repository

- [google-drive-ai-vault](https://github.com/AvrahamMakovsky/google-drive-ai-vault) - a ChatGPT + Google Drive workflow template for keeping project context, decisions, review loops, and reusable prompts organized across long-running work.

This repository contains the practical tooling examples. The AI vault repository contains the broader workflow/context layer for managing projects like this with ChatGPT.
