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

# hostname_extractor.py

Small utility for extracting possible hostnames from copied text, logs, ticket titles, or notes.

I built this for cases where I had a lot of copied operational text and needed a clean host list without manually scanning every line.

## What it does

- Reads text from a file, stdin, or a temporary Notepad window on Windows.
- Extracts possible hostnames.
- Removes duplicates.
- Prints a clean sorted list.
- Can filter by a specific hostname prefix.
- Can write results to a text file.

## Usage

Read from a file:

```bash
python hostname_extractor.py notes.txt
```

Paste through Notepad on Windows:

```bash
python hostname_extractor.py --notepad
```

Filter by prefix:

```bash
python hostname_extractor.py notes.txt --prefix LAB-PC-
```

Save to a file:

```bash
python hostname_extractor.py notes.txt --output hosts.txt
```

Use a custom regex:

```bash
python hostname_extractor.py notes.txt --regex "\bLAB-[A-Z]+-\d{3}\b"
```

## Notes

This version is intentionally generic. It does not contain workplace-specific hostname prefixes or patterns.

Examples use fake hostnames only.
