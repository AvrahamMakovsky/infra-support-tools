# Legacy Web Forms Endpoint Client

A sanitized Python reference for placing a small command-line interface in front
of a legacy ASP.NET Web Forms application that has no supported API.

The script reproduces the browser workflow for:

- searching an endpoint by MAC address
- registering an endpoint into an allowed VLAN
- deleting an endpoint
- verifying register and delete operations through a separate search

It is intended for Windows environments that use Integrated Windows
Authentication through the logged-in user's SSPI context.

## Why this is useful

Legacy Web Forms applications often depend on hidden state, chained dropdown
postbacks, cookies, redirects, and unreliable partial refresh behavior. A normal
HTTP POST is usually not enough.

This client demonstrates a practical pattern:

1. Open and validate the real page.
2. preserve every hidden form field returned by the server.
3. replay dependent postbacks in order.
4. treat a write response as provisional.
5. read the state back before reporting success or repeating an ambiguous write.

## Safety behavior

- Register and delete require confirmation unless `--yes` is supplied.
- Delete is skipped when a validated search finds no matching endpoint.
- A missing result table on an unexpected page is not interpreted as an empty
  search result.
- An ambiguous registration response is checked through Search before another
  registration POST is allowed.
- An unverified write returns a nonzero exit code.
- TLS verification is enabled by default. Prefer `--ca-bundle` for an internal
  certificate authority instead of `--insecure`.
- HTML dumping is disabled by default because pages may contain operational data.

## Before use

This repository contains placeholder routes, field names, policy values, VLANs,
and result-column aliases. It will not work against a real portal without local
configuration.

Review and replace the configuration block near the top of
`legacy_webforms_endpoint_client.py`. Keep real values in a private local config
or an untracked override rather than committing them.

Use the script only against systems you are authorized to administer.

## Installation

Python 3.10 or newer is required.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

The runtime authentication adapter is Windows-specific. The help screen and
helper tests can still run on another operating system because the SSPI package
is loaded only when a live client is created.

## Usage

```powershell
# Help
py legacy_webforms_endpoint_client.py --help

# Search
py legacy_webforms_endpoint_client.py search AA-BB-CC-DD-EE-FF

# Register, with confirmation
py legacy_webforms_endpoint_client.py register AA-BB-CC-DD-EE-FF

# Register non-interactively
py legacy_webforms_endpoint_client.py register AA-BB-CC-DD-EE-FF `
  --vlan VLAN_HOSTS_A --yes

# Delete, with confirmation
py legacy_webforms_endpoint_client.py delete AA-BB-CC-DD-EE-FF `
  --location SITE-A

# Use a corporate CA bundle
py legacy_webforms_endpoint_client.py --ca-bundle C:\path\corp-ca.pem `
  search AA-BB-CC-DD-EE-FF
```

`--insecure` is available for exceptional cases, but it disables certificate
verification and prints a warning.

## Tests

The tests use Python's standard `unittest` module and mocked HTTP responses.
They do not connect to a real portal.

```powershell
py -m unittest discover -s tests -v
```

The focused tests cover hidden Web Forms state, DOM-based page detection,
false-positive delete prevention, checked registration retries, permanent HTTP
errors, and command-line confirmation options.

## Scope

This is an independent, sanitized reference implementation. It is not an
official tool of any employer or application vendor and contains no real
hostnames, credentials, endpoint data, or organization-specific values.

Author: Avraham Makovsky
License: MIT
