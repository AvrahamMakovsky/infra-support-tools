# workstation-tools

Tools I run from my own laptop/workstation.

These are small helpers for day-to-day support work: opening quick sessions, preparing host lists, parsing copied text, or making a manual workflow easier to repeat.

## Included tools

### bulk-ping-launcher.cmd

Opens continuous ping sessions for multiple hosts from a pasted list.

Paste hostnames or IP addresses into Notepad, save and close it, and the script opens one ping session per host. If Windows Terminal is installed, it uses tabs. Otherwise, it opens separate CMD windows.

### bulk-vnc-launcher.cmd

Opens multiple remote viewer sessions from a pasted host list.

Update the viewer path inside the script if your remote viewer is installed in a different location.

### hostname_extractor.py

Extracts possible hostnames from copied text, logs, ticket titles, or notes.

Useful when copied operational text contains many hostnames and you need a clean host list without manually scanning every line.

```bash
python hostname_extractor.py notes.txt --prefix LAB-PC-
```

### spreadsheet_work_item_flow.py

Turns spreadsheet rows into reviewed work-item proposals.

The public version uses a mocked creation flow, so it can demonstrate the workflow without connecting to a real ticketing system.

### Update-OfflineEndpointIdentity.ps1

Updates an offline Windows installation identity in two places:

- hostname-related values in the offline Windows `SYSTEM` registry hive
- an optional small hostname file on the EFI System Partition

This can be useful in lab or imaging workflows where a machine identity may need to survive rebuilds, PXE flows, or partition replacement.

```powershell
.\Update-OfflineEndpointIdentity.ps1 `
    -ComputerName "LAB-PC-001" `
    -WindowsRoot "E:\Windows" `
    -EfiRoot "S:\"
```

### legacy-webforms-client

Provides a sanitized Python command-line client for automating authorized
workflows in legacy ASP.NET Web Forms administration portals.

It handles hidden form state, chained postbacks, Windows Integrated
Authentication, and read-after-write verification for search, register,
and delete operations.

See [`legacy-webforms-client/README.md`](legacy-webforms-client/README.md)
for setup, safety notes, and usage.

## Notes

These are personal tools shared for free use.

Examples use fake hostnames. Replace them with hosts you are authorized to access.
