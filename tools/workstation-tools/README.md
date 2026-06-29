# workstation-tools

Tools I run from my own laptop/workstation.

These are small helpers for day-to-day support work: opening quick sessions, preparing host lists, parsing copied text, or making a manual workflow easier to repeat.

## Included tools

### bulk-ping-launcher.cmd

Opens continuous ping sessions for multiple hosts from a pasted list.

Paste hostnames or IP addresses into Notepad, save and close it, and the script opens one ping session per host. If Windows Terminal is installed, it uses tabs. Otherwise, it opens separate CMD windows.

### bulk-vnc-launcher.cmd

Opens multiple RealVNC Viewer sessions from a pasted host list.

Update the `VNC_PATH` inside the script if RealVNC Viewer is installed in a different location.

## Notes

These are personal tools shared for free use.

Examples use fake hostnames. Replace them with hosts you are authorized to access.
