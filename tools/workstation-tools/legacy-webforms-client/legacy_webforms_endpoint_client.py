#!/usr/bin/env python3
"""
Sanitized reference CLI for automating a legacy ASP.NET Web Forms endpoint portal.

The target pattern has no supported API, uses Windows Integrated Authentication,
and requires browser-like ASP.NET postbacks. The client supports search, register,
and delete operations, then verifies writes through a separate Search workflow.

This is an independent reference implementation, not an official tool of any
employer or application vendor. Replace every placeholder before use and run it
only against systems you are authorized to administer.

Author: Avraham Makovsky
License: MIT
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

def build_windows_auth() -> Any:
    """Create Windows Integrated Authentication only when the client is used."""
    try:
        from requests_negotiate_sspi import HttpNegotiateAuth
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: requests-negotiate-sspi. Install it with "
            "'pip install requests-negotiate-sspi'. This authentication adapter "
            "is intended for Windows SSPI environments."
        ) from exc

    return HttpNegotiateAuth()


# =============================================================================
# Site-specific configuration
# =============================================================================
#
# Replace these placeholder values with your internal application's values.
#
# For a public GitHub repository, keep this section generic. If you need real
# values internally, put them in a private config file, environment variables,
# or a separate untracked local override.
# =============================================================================

DEFAULT_BASE_URL = "https://legacy-webforms.example.internal"

APP_ROOT_PATH = "/endpoint-admin/"
HOME_PATH = "/endpoint-admin/"
SEARCH_PATH = "/endpoint-admin/Search"
ADD_PATH = "/endpoint-admin/Add"
DELETE_PATH = "/endpoint-admin/Delete"

# Fixed values submitted during registration.
# These are examples only.
REGISTER_LOCATION = "SITE-A"
REGISTER_BUSINESS_UNIT = "BU-EXAMPLE"
REGISTER_RESTRICT_LEVEL = "STANDARD"

# Delete may require a location. These are examples only.
DELETE_LOCATION_OPTIONS = ["SITE-A", "SITE-B"]

# Placeholder VLAN allowlist.
#
# The real implementation should contain only values that are safe for the
# operator to choose. During runtime we also intersect this list with the live
# options returned by the server, so stale or removed options are not shown.
REGISTER_VLAN_ALLOWLIST = [
    {
        "value": "VLAN_INFRA_A",
        "description": "Infrastructure devices",
        "example": "device-infra-001",
    },
    {
        "value": "VLAN_KVM_A",
        "description": "KVM / remote console devices",
        "example": "kvm-room-001",
    },
    {
        "value": "VLAN_HOSTS_A",
        "description": "General lab hosts",
        "example": "host-lab-001",
    },
    {
        "value": "VLAN_TARGETS_A",
        "description": "Test targets / systems under test",
        "example": "sut-lab-001",
    },
]

# ASP.NET control IDs/names.
#
# These are intentionally grouped here because Web Forms control names are
# generated from the server-side control tree. If the portal changes, this is
# usually the first section you need to update.
SEARCH_MAC_TEXTBOX_NAME = "ctl00$MainContent$txt_single_mac"
SEARCH_MAC_TEXTBOX_ID = "MainContent_txt_single_mac"
SEARCH_BUTTON_NAME = "ctl00$MainContent$btn_search"
SEARCH_BUTTON_VALUE = "Search"
SEARCH_MODE_RADIO_NAME = "ctl00$MainContent$rdbgrp_search"
SEARCH_MODE_SINGLE_MAC_VALUE = "rdb_search_single_mac"
SEARCH_RESULTS_TABLE_ID = "MainContent_grd_search_results"

ADD_LOCATION_NAME = "ctl00$MainContent$drp_location"
ADD_LOCATION_ID = "MainContent_drp_location"
ADD_BU_NAME = "ctl00$MainContent$drp_bu"
ADD_BU_ID = "MainContent_drp_bu"
ADD_RESTRICT_NAME = "ctl00$MainContent$drp_restrict_level"
ADD_RESTRICT_ID = "MainContent_drp_restrict_level"
ADD_VLAN_NAME = "ctl00$MainContent$drp_vlan"
ADD_VLAN_ID = "MainContent_drp_vlan"
ADD_UPLOAD_METHOD_NAME = "ctl00$MainContent$rdbgrp_upload_method"
ADD_UPLOAD_METHOD_PASTE_VALUE = "rdb_paste_mac"
ADD_PASTE_MAC_NAME = "ctl00$MainContent$txt_paste_mac"
ADD_REGISTER_BUTTON_NAME = "ctl00$MainContent$btn_register"
ADD_REGISTER_BUTTON_VALUE = "Register"
ADD_SUCCESS_TABLE_ID = "MainContent_grd_successful"

DELETE_UPLOAD_METHOD_NAME = "ctl00$MainContent$rdbgrp_upload_method"
DELETE_UPLOAD_METHOD_PASTE_VALUE = "rdb_paste_mac"
DELETE_PASTE_MAC_NAME = "ctl00$MainContent$txt_paste_mac"
DELETE_PASTE_MAC_ID = "MainContent_txt_paste_mac"
DELETE_LOCATION_NAME = "ctl00$MainContent$drp_location"
DELETE_LOCATION_ID = "MainContent_drp_location"
DELETE_BUTTON_NAME = "ctl00$MainContent$btn_delete"
DELETE_BUTTON_VALUE = "Delete"
DELETE_RESULTS_TABLE_ID = "MainContent_grd_deleted_macs"

STATUS_LABEL_ID = "MainContent_lbl_status"

# Search result headers vary between Web Forms deployments and versions.
SEARCH_MAC_COLUMN_NAMES = ("mac_address", "MAC Address", "MAC", "Mac Address")
SEARCH_VLAN_COLUMN_NAMES = ("Endpoint VLAN", "VLAN", "Vlan")

RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

PLEASE_SELECT = "Please Select..."


# =============================================================================
# Data/config objects
# =============================================================================

@dataclass
class ClientConfig:
    base_url: str
    verify_tls: bool | str
    debug: bool = False
    dump_html_dir: Path | None = None

    @property
    def home_url(self) -> str:
        return self.base_url.rstrip("/") + HOME_PATH

    @property
    def search_url(self) -> str:
        return self.base_url.rstrip("/") + SEARCH_PATH

    @property
    def add_url(self) -> str:
        return self.base_url.rstrip("/") + ADD_PATH

    @property
    def delete_url(self) -> str:
        return self.base_url.rstrip("/") + DELETE_PATH

    @property
    def app_root_url(self) -> str:
        return self.base_url.rstrip("/") + APP_ROOT_PATH


# =============================================================================
# Generic helpers
# =============================================================================

def normalize_mac(mac: str) -> str:
    """
    Normalize MAC input to XX-XX-XX-XX-XX-XX.

    Accepts common formats:
        AABBCCDDEEFF
        AA:BB:CC:DD:EE:FF
        AA-BB-CC-DD-EE-FF
        AABB.CCDD.EEFF
    """
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", mac or "")
    if len(cleaned) != 12:
        raise ValueError(f"Invalid MAC address: {mac!r}")
    return "-".join(cleaned[i:i + 2] for i in range(0, 12, 2)).upper()


def compact_mac(mac: str) -> str:
    """
    Convert any MAC representation into lowercase 12-hex format for comparison.
    """
    return re.sub(r"[^0-9A-Fa-f]", "", mac or "").lower()


def extract_hidden_fields(html: str) -> dict[str, str]:
    """Extract every named hidden input needed for the next Web Forms postback."""
    soup = BeautifulSoup(html, "lxml")
    fields: dict[str, str] = {}

    for element in soup.select('input[type="hidden"][name]'):
        name = str(element.get("name", "")).strip()
        if name:
            fields[name] = str(element.get("value", ""))

    return fields


def parse_table_by_id(html: str, table_id: str) -> list[dict[str, str]]:
    """
    Parse an HTML table into a list of dictionaries.

    The first row is treated as the header row. This matches many ASP.NET GridView
    outputs.
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id=table_id)

    if not table:
        return []

    rows = table.find_all("tr")
    if not rows:
        return []

    headers = [cell.get_text(" ", strip=True) for cell in rows[0].find_all(["th", "td"])]

    results: list[dict[str, str]] = []
    for row in rows[1:]:
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
        if cells:
            results.append(dict(zip(headers, cells)))

    return results


def get_status_text(html: str, status_id: str = STATUS_LABEL_ID) -> str:
    """
    Extract a status message from a known ASP.NET Label control.
    """
    soup = BeautifulSoup(html, "lxml")
    el = soup.find(id=status_id)
    return el.get_text(" ", strip=True) if el else ""


def parse_select_options(html: str, select_id: str) -> list[dict[str, Any]]:
    """
    Parse options from a dropdown/select element.
    """
    soup = BeautifulSoup(html, "lxml")
    select = soup.find("select", id=select_id)
    if not select:
        return []

    options: list[dict[str, Any]] = []
    for opt in select.find_all("option"):
        value = opt.get("value", "").strip()
        text = opt.get_text(" ", strip=True)
        if value:
            options.append(
                {
                    "value": value,
                    "text": text,
                    "selected": opt.has_attr("selected"),
                }
            )

    return options


def get_row_value(row: dict[str, str], names: tuple[str, ...]) -> str:
    """Return the first non-empty value found under one of several headers."""
    for name in names:
        value = row.get(name, "").strip()
        if value:
            return value
    return ""


def matching_rows_for_mac(
    mac: str,
    results: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return result rows that match a MAC address across configured headers."""
    wanted = compact_mac(mac)
    return [
        row
        for row in results
        if compact_mac(get_row_value(row, SEARCH_MAC_COLUMN_NAMES)) == wanted
    ]


def print_results_table(results: list[dict[str, Any]]) -> None:
    """
    Print list-of-dicts results as a simple ASCII table.
    """
    if not results:
        print("No results.")
        return

    headers = list(results[0].keys())
    widths = {
        h: max(len(h), max(len(str(row.get(h, ""))) for row in results))
        for h in headers
    }

    sep = "+-" + "-+-".join("-" * widths[h] for h in headers) + "-+"

    print(sep)
    print("| " + " | ".join(h.ljust(widths[h]) for h in headers) + " |")
    print(sep)

    for row in results:
        print("| " + " | ".join(str(row.get(h, "")).ljust(widths[h]) for h in headers) + " |")

    print(sep)


def prompt_if_missing(
    current_value: str | None,
    prompt_text: str,
    normalizer: Callable[[str], str] | None = None,
) -> str:
    """
    Prompt for a required value if it was not supplied by CLI.
    """
    if current_value:
        return normalizer(current_value) if normalizer else current_value

    while True:
        value = input(f"{prompt_text}: ").strip()
        if not value:
            print("Value is required.")
            continue

        try:
            return normalizer(value) if normalizer else value
        except Exception as exc:
            print(f"Invalid input: {exc}")


def choose_command_interactive() -> str:
    commands = ["search", "register", "delete"]

    print("Choose command:")
    for index, command in enumerate(commands, 1):
        print(f"  {index}. {command}")

    while True:
        choice = input("Enter number: ").strip()
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(commands):
                return commands[idx - 1]

        print("Invalid selection, try again.")


def choose_location_interactive() -> str:
    """
    Delete may require location context. For sanitized publishing these are
    placeholder values.
    """
    print("\nChoose location:")
    for index, loc in enumerate(DELETE_LOCATION_OPTIONS, 1):
        print(f"  {index}. {loc}")

    while True:
        choice = input("Enter number or text for location: ").strip()

        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(DELETE_LOCATION_OPTIONS):
                return DELETE_LOCATION_OPTIONS[idx - 1]

        matches = [
            loc for loc in DELETE_LOCATION_OPTIONS
            if choice.lower() == loc.lower()
        ]

        if matches:
            return matches[0]

        print("Invalid selection, try again.")


def choose_vlan_interactive(server_options: list[dict[str, Any]]) -> str:
    """
    Present a safe VLAN allowlist to the operator.

    Important:
        We do not blindly show every VLAN returned by the server. We intersect
        the live server dropdown with our local allowlist. That gives us both:
            - protection from stale local config
            - protection from exposing unintended choices to operators
    """
    server_values = {opt["value"] for opt in server_options}

    available = [
        entry for entry in REGISTER_VLAN_ALLOWLIST
        if entry["value"] in server_values
    ]

    if not available:
        raise RuntimeError(
            "None of the locally allowed VLANs were returned by the server. "
            f"Server returned: {', '.join(sorted(server_values))}"
        )

    idx_w = max(len("#"), len(str(len(available))))
    vlan_w = max(len("VLAN"), max(len(e["value"]) for e in available))
    desc_w = max(len("Description"), max(len(e["description"]) for e in available))
    ex_w = max(len("Example"), max(len(e["example"]) for e in available))

    sep = (
        "+-" + "-" * idx_w +
        "-+-" + "-" * vlan_w +
        "-+-" + "-" * desc_w +
        "-+-" + "-" * ex_w +
        "-+"
    )

    def row(idx: str, vlan: str, desc: str, example: str) -> str:
        return (
            "| " + idx.ljust(idx_w) +
            " | " + vlan.ljust(vlan_w) +
            " | " + desc.ljust(desc_w) +
            " | " + example.ljust(ex_w) +
            " |"
        )

    print("\nChoose VLAN:")
    print(sep)
    print(row("#", "VLAN", "Description", "Example"))
    print(sep)

    for index, entry in enumerate(available, 1):
        print(row(str(index), entry["value"], entry["description"], entry["example"]))

    print(sep)

    while True:
        choice = input("Enter number or text for VLAN: ").strip()

        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(available):
                return available[idx - 1]["value"]

        lowered = choice.lower()
        matches = [
            entry for entry in available
            if lowered in entry["value"].lower()
            or lowered in entry["description"].lower()
            or lowered in entry["example"].lower()
        ]

        if len(matches) == 1:
            return matches[0]["value"]

        if len(matches) > 1:
            print("Multiple matches:")
            for index, entry in enumerate(matches, 1):
                print(f"  {index}. {entry['value']}")
            continue

        print("Invalid selection, try again.")


# =============================================================================
# Legacy WebForms client
# =============================================================================

class LegacyWebFormsClient:
    """
    API-like client around a legacy ASP.NET Web Forms UI.

    This class intentionally hides browser workflow details behind Python methods:

        client.search_single_mac(...)
        client.register_mac_interactive(...)
        client.delete_mac(...)

    From the user's perspective it feels like an API. Internally, it performs the
    full browser-like WebForms postback flow.
    """

    def __init__(self, config: ClientConfig):
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)

        self.session = requests.Session()

        # Enterprise SSO:
        #
        # Browsers automatically perform Integrated Windows Authentication in
        # many enterprise environments. Python requests does not. HttpNegotiateAuth
        # uses the logged-in Windows user's SSPI context so the script can access
        # the same internal site the user can access in a browser.
        self.session.auth = build_windows_auth()

        # Prefer certificate verification. For internal sites, either:
        #   - pass --ca-bundle path/to/corp-ca.pem
        #   - or, only when unavoidable, pass --insecure
        self.session.verify = config.verify_tls

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "legacy-webforms-endpoint-client/1.0"
                ),
            }
        )

    # -------------------------------------------------------------------------
    # Diagnostics
    # -------------------------------------------------------------------------

    def dump_html(self, label: str, html: str) -> None:
        """
        Optionally dump HTML for troubleshooting.

        Disabled by default because result pages can contain sensitive operational
        data. If enabled, write only to a local/private directory.
        """
        if not self.config.dump_html_dir:
            return

        self.config.dump_html_dir.mkdir(parents=True, exist_ok=True)
        path = self.config.dump_html_dir / f"{label}.html"
        path.write_text(html, encoding="utf-8")
        self.log.debug("Wrote HTML dump: %s", path)

    # -------------------------------------------------------------------------
    # Page detection
    # -------------------------------------------------------------------------

    @staticmethod
    def _has_dom_elements(
        html: str,
        *,
        ids: tuple[str, ...],
        names: tuple[str, ...] = (),
    ) -> bool:
        soup = BeautifulSoup(html, "lxml")
        return all(soup.find(id=element_id) is not None for element_id in ids) and all(
            soup.find(attrs={"name": name}) is not None for name in names
        )

    @staticmethod
    def _url_path_matches(final_url: str, expected_path: str) -> bool:
        actual = urlparse(final_url).path.rstrip("/").lower()
        expected = expected_path.rstrip("/").lower()
        return actual == expected

    def is_real_search_page(self, html: str, final_url: str) -> bool:
        """Detect Search by route and parsed DOM signatures, not HTTP 200 alone."""
        return (
            self._url_path_matches(final_url, SEARCH_PATH)
            and self._has_dom_elements(
                html,
                ids=(SEARCH_MAC_TEXTBOX_ID,),
                names=(SEARCH_BUTTON_NAME,),
            )
        )

    def is_real_add_page(self, html: str, final_url: str) -> bool:
        return (
            self._url_path_matches(final_url, ADD_PATH)
            and self._has_dom_elements(
                html,
                ids=(ADD_LOCATION_ID, ADD_BU_ID, ADD_RESTRICT_ID),
            )
        )

    def is_real_delete_page(self, html: str, final_url: str) -> bool:
        return (
            self._url_path_matches(final_url, DELETE_PATH)
            and self._has_dom_elements(
                html,
                ids=(DELETE_PASTE_MAC_ID, DELETE_LOCATION_ID),
            )
        )

    # -------------------------------------------------------------------------
    # Navigation/retry helpers
    # -------------------------------------------------------------------------

    def warm_up_home(self) -> requests.Response:
        """
        Warm up the application session.

        Some legacy WebForms apps initialize session state, auth context, cookies,
        or menu state on the home page. Navigating directly to Add/Search/Delete
        can fail or redirect until that initialization has happened.
        """
        response = self.session.get(
            self.config.home_url,
            headers={"Referer": self.config.base_url},
            timeout=30,
            allow_redirects=True,
        )

        self.log.debug(
            "HOME status=%s final_url=%s history=%s",
            response.status_code,
            response.url,
            [(h.status_code, h.url) for h in response.history],
        )

        response.raise_for_status()
        return response

    def get_page_with_retry(
        self,
        url: str,
        referer: str,
        detector: Callable[[str, str], bool],
        label: str,
        max_attempts: int = 10,
        delay_sec: float = 1.5,
    ) -> tuple[str, str]:
        """Acquire a validated Web Forms page, retrying only recoverable failures."""
        self.warm_up_home()

        last_html = ""
        last_url = ""
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = self.session.get(
                    url,
                    headers={"Referer": referer},
                    timeout=30,
                    allow_redirects=True,
                )

                last_html = response.text
                last_url = response.url

                self.log.debug(
                    "%s GET attempt=%s status=%s final_url=%s history=%s",
                    label,
                    attempt,
                    response.status_code,
                    response.url,
                    [(h.status_code, h.url) for h in response.history],
                )

                if response.status_code == 200 and detector(response.text, response.url):
                    self.log.debug("Acquired real %s page", label)
                    self.dump_html(f"{label.lower()}_page", response.text)
                    return response.text, response.url

                if (
                    response.status_code >= 400
                    and response.status_code not in RETRYABLE_HTTP_STATUS_CODES
                ):
                    response.raise_for_status()

                last_error = RuntimeError(
                    f"Received an unexpected {label} page at {response.url} "
                    f"with HTTP {response.status_code}"
                )

            except requests.RequestException as exc:
                last_error = exc
                self.log.debug("%s GET attempt failed: %s", label, exc)

                response = getattr(exc, "response", None)
                if (
                    response is not None
                    and response.status_code not in RETRYABLE_HTTP_STATUS_CODES
                ):
                    raise

            if attempt < max_attempts:
                time.sleep(delay_sec)

        self.dump_html(f"{label.lower()}_last_failed_page", last_html)
        raise RuntimeError(
            f"Could not reach real {label} page after {max_attempts} attempts. "
            f"Last URL: {last_url or 'unavailable'}. Last error: {last_error}"
        )

    def get_add_page(self) -> tuple[str, str]:
        return self.get_page_with_retry(
            self.config.add_url,
            referer=self.config.app_root_url,
            detector=self.is_real_add_page,
            label="ADD",
        )

    # -------------------------------------------------------------------------
    # Search
    # -------------------------------------------------------------------------

    def search_single_mac(self, mac: str) -> tuple[str, list[dict[str, str]], str]:
        """Search for one MAC and reject redirects or unrelated 200 responses."""
        mac = normalize_mac(mac)
        search_html, search_url = self.get_page_with_retry(
            self.config.search_url,
            referer=self.config.app_root_url,
            detector=self.is_real_search_page,
            label="SEARCH",
        )

        payload = extract_hidden_fields(search_html)
        payload.update(
            {
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                SEARCH_MODE_RADIO_NAME: SEARCH_MODE_SINGLE_MAC_VALUE,
                SEARCH_MAC_TEXTBOX_NAME: mac,
                SEARCH_BUTTON_NAME: SEARCH_BUTTON_VALUE,
            }
        )

        response = self.session.post(
            search_url,
            data=payload,
            headers={"Origin": self.config.base_url, "Referer": search_url},
            timeout=30,
            allow_redirects=True,
        )

        self.log.debug(
            "SEARCH POST status=%s final_url=%s history=%s",
            response.status_code,
            response.url,
            [(h.status_code, h.url) for h in response.history],
        )
        response.raise_for_status()

        html = response.text
        self.dump_html("search_result", html)

        if not self.is_real_search_page(html, response.url):
            raise RuntimeError(
                "Search POST did not return the validated Search page; refusing "
                "to interpret a missing results table as an empty result."
            )

        return (
            get_status_text(html),
            parse_table_by_id(html, SEARCH_RESULTS_TABLE_ID),
            html,
        )

    def get_current_vlan_for_mac(self, mac: str) -> str | None:
        """Lookup the current VLAN through a validated Search response."""
        _, results, _ = self.search_single_mac(mac)
        rows = matching_rows_for_mac(mac, results)
        if rows:
            return get_row_value(rows[0], SEARCH_VLAN_COLUMN_NAMES) or None
        return None

    @staticmethod
    def search_result_matches(
        mac: str,
        expected_vlan: str | None,
        results: list[dict[str, str]],
    ) -> tuple[bool, str]:
        rows = matching_rows_for_mac(mac, results)

        if not rows:
            if expected_vlan:
                return False, f"Search did not confirm VLAN {expected_vlan} for MAC {mac}"
            return False, f"Search did not confirm MAC {mac}"

        if expected_vlan:
            for row in rows:
                row_vlan = get_row_value(row, SEARCH_VLAN_COLUMN_NAMES)
                if row_vlan == expected_vlan:
                    return True, f"Verified by search: MAC found in VLAN {row_vlan}"
            return False, f"Search did not confirm VLAN {expected_vlan} for MAC {mac}"

        return True, "Verified by search: MAC found"

    def verify_registration_by_search(
        self,
        mac: str,
        expected_vlan: str | None = None,
        max_attempts: int = 4,
        delay_sec: float = 2.0,
    ) -> tuple[bool, str, str, list[dict[str, str]], str]:
        """
        Verify registration through the Search workflow.

        This is intentionally separate from the Register response. For a flaky
        WebForms app, a postback result page is less trustworthy than reading the
        state back through another workflow.
        """
        last_status = ""
        last_results: list[dict[str, str]] = []

        for attempt in range(1, max_attempts + 1):
            try:
                self.log.debug("Registration verification attempt %s", attempt)

                status, results, html = self.search_single_mac(mac)
                last_status = status
                last_results = results

                ok, message = self.search_result_matches(mac, expected_vlan, results)
                if ok:
                    return True, message, status, results, html

                self.log.debug(message)

            except Exception as exc:
                self.log.debug("Registration verification failed: %s", exc)

            time.sleep(delay_sec)

        return (
            False,
            "Registration could not be confirmed by search",
            last_status,
            last_results,
            "",
        )

    def verify_deletion_by_search(
        self,
        mac: str,
        max_attempts: int = 4,
        delay_sec: float = 2.0,
    ) -> tuple[bool, str, str, list[dict[str, str]], str]:
        """Verify deletion only after a validated Search response."""
        mac = normalize_mac(mac)
        last_status = ""
        last_results: list[dict[str, str]] = []

        for attempt in range(1, max_attempts + 1):
            try:
                self.log.debug("Deletion verification attempt %s", attempt)
                status, results, html = self.search_single_mac(mac)
                last_status = status
                last_results = results

                if not matching_rows_for_mac(mac, results):
                    return (
                        True,
                        "Verified by search: MAC no longer found",
                        status,
                        results,
                        html,
                    )

                self.log.debug("MAC still appears in search results")
            except Exception as exc:
                self.log.debug("Deletion verification failed: %s", exc)

            if attempt < max_attempts:
                time.sleep(delay_sec)

        return (
            False,
            "Deletion could not be confirmed by a validated search",
            last_status,
            last_results,
            "",
        )

    # -------------------------------------------------------------------------
    # Delete
    # -------------------------------------------------------------------------

    def delete_mac(self, mac: str, location: str) -> tuple[str, list[dict[str, str]], str]:
        """Submit one delete operation. Callers must confirm and verify it."""
        mac = normalize_mac(mac)
        delete_html, delete_url = self.get_page_with_retry(
            self.config.delete_url,
            referer=self.config.app_root_url,
            detector=self.is_real_delete_page,
            label="DELETE",
        )

        payload = extract_hidden_fields(delete_html)
        payload.update(
            {
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                DELETE_UPLOAD_METHOD_NAME: DELETE_UPLOAD_METHOD_PASTE_VALUE,
                DELETE_PASTE_MAC_NAME: f" {mac}",
                DELETE_LOCATION_NAME: location,
                DELETE_BUTTON_NAME: DELETE_BUTTON_VALUE,
            }
        )

        response = self.session.post(
            delete_url,
            data=payload,
            headers={"Origin": self.config.base_url, "Referer": delete_url},
            timeout=30,
            allow_redirects=True,
        )
        self.log.debug(
            "DELETE POST status=%s final_url=%s history=%s",
            response.status_code,
            response.url,
            [(h.status_code, h.url) for h in response.history],
        )
        response.raise_for_status()

        html = response.text
        self.dump_html("delete_result", html)

        if not self.is_real_delete_page(html, response.url):
            self.log.warning(
                "Delete response page was ambiguous; final state must be established "
                "only through Search verification."
            )

        return (
            get_status_text(html),
            parse_table_by_id(html, DELETE_RESULTS_TABLE_ID),
            html,
        )

    def delete_mac_interactive(
        self,
        mac: str | None = None,
        location: str | None = None,
        assume_yes: bool = False,
    ) -> tuple[str, list[dict[str, str]], str]:
        """Show current state, request confirmation, delete, and verify."""
        mac = prompt_if_missing(mac, "Enter MAC", normalize_mac)
        location = location or choose_location_interactive()

        print("\nChecking current registration status...")
        search_status, search_results, search_html = self.search_single_mac(mac)
        current_rows = matching_rows_for_mac(mac, search_results)

        if not current_rows:
            message = "No matching endpoint found; no delete was submitted"
            print(f"  {message}.")
            return message, [], search_html

        print_results_table(current_rows)
        print("\nDeletion summary:")
        print(f"  MAC:      {mac}")
        print(f"  Location: {location}")

        if not assume_yes:
            confirm = input("Proceed with deletion? [y/N]: ").strip().lower()
            if confirm != "y":
                print("Deletion cancelled.")
                return "Cancelled", current_rows, search_html

        status, results, html = self.delete_mac(mac, location)
        verified, message, _, verify_results, verify_html = (
            self.verify_deletion_by_search(mac)
        )
        print("\nVerification:", message)

        if not verified:
            self.dump_html("delete_unverified_result", html)
            raise RuntimeError(
                f"Delete was submitted but could not be verified. Portal status: "
                f"{status or search_status or 'unavailable'}"
            )

        if verify_html:
            self.dump_html("delete_verification", verify_html)
        return f"{status} | {message}", verify_results or results, verify_html or html

    # -------------------------------------------------------------------------
    # Register: WebForms step logic
    # -------------------------------------------------------------------------

    def post_add_step(
        self,
        current_html: str,
        add_url: str,
        event_target: str,
        location: str,
        business_unit: str,
        restrict_level: str,
        vlan: str,
        paste_mac: str = "",
    ) -> tuple[str, str]:
        """
        Execute one Add-page postback.

        WebForms dropdowns often cause server-side postbacks. The server uses
        those postbacks to populate dependent dropdowns. For example:

            Location selected -> BU list becomes valid
            BU selected       -> restriction list becomes valid
            Restriction       -> VLAN list becomes valid

        A human sees this as dropdowns refreshing in the browser. In HTTP, it is
        a sequence of POST requests with __EVENTTARGET set to the changed control.

        AJAX note:
            Some WebForms apps use ScriptManager/UpdatePanel partial postbacks.
            If your browser network trace includes fields like __ASYNCPOST or
            ScriptManager values, add them here. Many apps still accept full
            postbacks like this, which are simpler and more reliable.
        """
        payload = extract_hidden_fields(current_html)
        payload.update(
            {
                "__EVENTTARGET": event_target,
                "__EVENTARGUMENT": "",
                "__LASTFOCUS": "",
                ADD_LOCATION_NAME: location,
                ADD_BU_NAME: business_unit,
                ADD_RESTRICT_NAME: restrict_level,
                ADD_VLAN_NAME: vlan,
                ADD_UPLOAD_METHOD_NAME: ADD_UPLOAD_METHOD_PASTE_VALUE,
                ADD_PASTE_MAC_NAME: paste_mac,
            }
        )

        response = self.session.post(
            add_url,
            data=payload,
            headers={"Origin": self.config.base_url, "Referer": add_url},
            timeout=30,
            allow_redirects=True,
        )

        self.log.debug(
            "ADD step event_target=%s status=%s final_url=%s history=%s",
            event_target,
            response.status_code,
            response.url,
            [(h.status_code, h.url) for h in response.history],
        )

        response.raise_for_status()

        return response.text, response.url

    def rebuild_add_state(
        self,
        location: str | None = None,
        business_unit: str | None = None,
        restrict_level: str | None = None,
        vlan: str | None = None,
    ) -> tuple[str, str]:
        """
        Reconstruct the Add page state from scratch.

        This is the core reliability trick.

        If a dropdown postback randomly redirects to Home/Default or returns stale
        state, the safest recovery is not to continue with a corrupted page. Instead:

            1. reopen Add
            2. replay Location if needed
            3. replay BU if needed
            4. replay Restriction if needed
            5. replay VLAN if needed

        That gives us a fresh, server-approved set of hidden fields before the
        final Register submit.
        """
        add_html, add_url = self.get_add_page()

        if location:
            add_html, add_url = self.post_add_step(
                add_html,
                add_url,
                event_target=ADD_LOCATION_NAME,
                location=location,
                business_unit=PLEASE_SELECT,
                restrict_level=PLEASE_SELECT,
                vlan=PLEASE_SELECT,
            )
            if not self.is_real_add_page(add_html, add_url):
                raise RuntimeError("Failed while rebuilding Add state after location")

        if business_unit:
            add_html, add_url = self.post_add_step(
                add_html,
                add_url,
                event_target=ADD_BU_NAME,
                location=location or PLEASE_SELECT,
                business_unit=business_unit,
                restrict_level=PLEASE_SELECT,
                vlan=PLEASE_SELECT,
            )
            if not self.is_real_add_page(add_html, add_url):
                raise RuntimeError("Failed while rebuilding Add state after business unit")

        if restrict_level:
            add_html, add_url = self.post_add_step(
                add_html,
                add_url,
                event_target=ADD_RESTRICT_NAME,
                location=location or PLEASE_SELECT,
                business_unit=business_unit or PLEASE_SELECT,
                restrict_level=restrict_level,
                vlan=PLEASE_SELECT,
            )
            if not self.is_real_add_page(add_html, add_url):
                raise RuntimeError("Failed while rebuilding Add state after restriction level")

        if vlan:
            add_html, add_url = self.post_add_step(
                add_html,
                add_url,
                event_target=ADD_VLAN_NAME,
                location=location or PLEASE_SELECT,
                business_unit=business_unit or PLEASE_SELECT,
                restrict_level=restrict_level or PLEASE_SELECT,
                vlan=vlan,
            )
            if not self.is_real_add_page(add_html, add_url):
                raise RuntimeError("Failed while rebuilding Add state after VLAN")

        return add_html, add_url

    def perform_add_step_with_retry(
        self,
        event_target: str,
        location: str,
        business_unit: str,
        restrict_level: str,
        vlan: str,
        rebuild_before_step: dict[str, str],
        max_attempts: int = 5,
        delay_sec: float = 1.5,
    ) -> tuple[str, str]:
        """
        Perform one Add dropdown postback with retries.

        The function rebuilds the page to the state immediately before the target
        step, then executes the step. If the portal redirects away or returns an
        invalid page, retry from a clean state.
        """
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                self.log.debug("ADD step attempt=%s event_target=%s", attempt, event_target)

                add_html, add_url = self.rebuild_add_state(**rebuild_before_step)

                step_html, step_url = self.post_add_step(
                    add_html,
                    add_url,
                    event_target=event_target,
                    location=location,
                    business_unit=business_unit,
                    restrict_level=restrict_level,
                    vlan=vlan,
                )

                if self.is_real_add_page(step_html, step_url):
                    return step_html, step_url

                last_error = RuntimeError(
                    f"Step {event_target} redirected away from Add page to {step_url}"
                )

            except Exception as exc:
                last_error = exc
                self.log.debug("ADD step failed: %s", exc)

            time.sleep(delay_sec)

        raise RuntimeError(
            f"Failed Add step {event_target} after {max_attempts} attempts: {last_error}"
        )

    def submit_register_with_retry(
        self,
        mac: str,
        location: str,
        business_unit: str,
        restrict_level: str,
        vlan: str,
        max_attempts: int = 5,
        delay_sec: float = 1.5,
    ) -> tuple[str, list[dict[str, str]], str]:
        """Submit registration without blindly repeating an ambiguous write."""
        mac = normalize_mac(mac)
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            status = ""
            html = ""
            results: list[dict[str, str]] = []

            try:
                self.log.debug("Final register attempt=%s", attempt)
                add_html, add_url = self.rebuild_add_state(
                    location=location,
                    business_unit=business_unit,
                    restrict_level=restrict_level,
                    vlan=vlan,
                )

                payload = extract_hidden_fields(add_html)
                payload.update(
                    {
                        "__EVENTTARGET": "",
                        "__EVENTARGUMENT": "",
                        "__LASTFOCUS": "",
                        ADD_LOCATION_NAME: location,
                        ADD_BU_NAME: business_unit,
                        ADD_RESTRICT_NAME: restrict_level,
                        ADD_VLAN_NAME: vlan,
                        ADD_UPLOAD_METHOD_NAME: ADD_UPLOAD_METHOD_PASTE_VALUE,
                        ADD_PASTE_MAC_NAME: f" {mac}",
                        ADD_REGISTER_BUTTON_NAME: ADD_REGISTER_BUTTON_VALUE,
                    }
                )

                response = self.session.post(
                    add_url,
                    data=payload,
                    headers={"Origin": self.config.base_url, "Referer": add_url},
                    timeout=30,
                    allow_redirects=True,
                )
                self.log.debug(
                    "REGISTER POST status=%s final_url=%s history=%s",
                    response.status_code,
                    response.url,
                    [(h.status_code, h.url) for h in response.history],
                )
                response.raise_for_status()

                html = response.text
                self.dump_html("register_result", html)
                status = get_status_text(html)
                results = parse_table_by_id(html, ADD_SUCCESS_TABLE_ID)

                if results or "complete" in status.lower() or "success" in status.lower():
                    return status, results, html

                last_error = RuntimeError(
                    "Register returned no recognizable success signal"
                )

            except Exception as exc:
                last_error = exc
                self.log.debug("Final register attempt failed or was ambiguous: %s", exc)

            # A timeout, redirect, or unfamiliar result page may happen after the
            # server applied the write. A repeat POST is allowed only after a
            # validated Search response proves the desired state is still absent.
            verify_error: Exception | None = None
            verify_status = ""
            verify_results: list[dict[str, str]] = []
            verify_html = ""

            for verify_attempt in range(1, 3):
                try:
                    verify_status, verify_results, verify_html = (
                        self.search_single_mac(mac)
                    )
                    verify_error = None
                    break
                except Exception as exc:
                    verify_error = exc
                    self.log.debug(
                        "Post-write read-back attempt %s failed: %s",
                        verify_attempt,
                        exc,
                    )
                    if verify_attempt < 2:
                        time.sleep(0.5)

            if verify_error is not None:
                raise RuntimeError(
                    "Registration response was ambiguous and backend state could "
                    "not be read safely. Refusing to repeat the write."
                ) from verify_error

            verified, message = self.search_result_matches(
                mac,
                vlan,
                verify_results,
            )
            if verified:
                resolved_status = status or verify_status or "Registration applied"
                return (
                    f"{resolved_status} | {message} after ambiguous response",
                    verify_results or results,
                    verify_html or html,
                )

            if attempt < max_attempts:
                self.log.warning(
                    "Registration attempt %s was not confirmed; retrying only after "
                    "a validated Search showed the desired state was absent.",
                    attempt,
                )
                time.sleep(delay_sec)

        raise RuntimeError(
            f"Register submit failed after {max_attempts} checked attempts: {last_error}"
        )

    def register_mac_interactive(
        self,
        mac: str | None = None,
        vlan: str | None = None,
        assume_yes: bool = False,
    ) -> tuple[str, list[dict[str, str]], str]:
        """
        Full interactive registration workflow.

        The fixed fields are deliberately not exposed as CLI options here because
        the sanitized reference assumes the operational policy is encoded in the
        tool:
            - fixed location
            - fixed business unit
            - fixed restriction level
            - limited VLAN allowlist

        Adapt this if your environment needs a different policy.
        """
        location = REGISTER_LOCATION
        business_unit = REGISTER_BUSINESS_UNIT
        restrict_level = REGISTER_RESTRICT_LEVEL

        mac = prompt_if_missing(mac, "Enter MAC", normalize_mac)

        print("\nChecking current registration status...")
        current_vlan = self.get_current_vlan_for_mac(mac)

        if current_vlan:
            print(f"  Current VLAN: {current_vlan}")
        else:
            print("  Current VLAN: not registered")

        # Walk dependent dropdowns to obtain server-generated VLAN options.
        add_html, add_url = self.perform_add_step_with_retry(
            event_target=ADD_LOCATION_NAME,
            location=location,
            business_unit=PLEASE_SELECT,
            restrict_level=PLEASE_SELECT,
            vlan=PLEASE_SELECT,
            rebuild_before_step={},
        )

        add_html, add_url = self.perform_add_step_with_retry(
            event_target=ADD_BU_NAME,
            location=location,
            business_unit=business_unit,
            restrict_level=PLEASE_SELECT,
            vlan=PLEASE_SELECT,
            rebuild_before_step={"location": location},
        )

        add_html, add_url = self.perform_add_step_with_retry(
            event_target=ADD_RESTRICT_NAME,
            location=location,
            business_unit=business_unit,
            restrict_level=restrict_level,
            vlan=PLEASE_SELECT,
            rebuild_before_step={
                "location": location,
                "business_unit": business_unit,
            },
        )

        server_vlan_options = parse_select_options(add_html, ADD_VLAN_ID)

        if vlan:
            server_values = {opt["value"] for opt in server_vlan_options}
            allowed_values = {entry["value"] for entry in REGISTER_VLAN_ALLOWLIST}

            if vlan not in allowed_values:
                raise RuntimeError(f"VLAN {vlan!r} is not in the local allowlist")

            if vlan not in server_values:
                raise RuntimeError(f"VLAN {vlan!r} was not returned by the server")

        else:
            vlan = choose_vlan_interactive(server_vlan_options)

        add_html, add_url = self.perform_add_step_with_retry(
            event_target=ADD_VLAN_NAME,
            location=location,
            business_unit=business_unit,
            restrict_level=restrict_level,
            vlan=vlan,
            rebuild_before_step={
                "location": location,
                "business_unit": business_unit,
                "restrict_level": restrict_level,
            },
        )

        print("\nRegistration summary:")
        print(f"  MAC:               {mac}")
        print(f"  Previous VLAN:     {current_vlan if current_vlan else 'none'}")
        print(f"  New VLAN:          {vlan}")
        print(f"  Location:          {location}")
        print(f"  Business Unit:     {business_unit}")
        print(f"  Restriction Level: {restrict_level}")

        if current_vlan == vlan:
            message = f"Already registered in VLAN {vlan}; no write was submitted"
            print(f"\n{message}.")
            verified, verify_message, _, verify_results, verify_html = (
                self.verify_registration_by_search(mac, expected_vlan=vlan)
            )
            if not verified:
                raise RuntimeError(verify_message)
            return f"{message} | {verify_message}", verify_results, verify_html

        if not assume_yes:
            confirm = input("Proceed with registration? [y/N]: ").strip().lower()
            if confirm != "y":
                print("Registration cancelled.")
                return "Cancelled", [], add_html

        status, results, html = self.submit_register_with_retry(
            mac=mac,
            location=location,
            business_unit=business_unit,
            restrict_level=restrict_level,
            vlan=vlan,
        )

        verified, verify_message, verify_status, verify_results, verify_html = (
            self.verify_registration_by_search(
                mac=mac,
                expected_vlan=vlan,
            )
        )

        print("\nVerification:", verify_message)

        if not verified:
            raise RuntimeError(
                f"Registration was submitted but could not be verified. "
                f"Portal status: {status or verify_status or 'unavailable'}"
            )

        return f"{status} | {verify_message}", verify_results, verify_html or html


# =============================================================================
# CLI
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    examples = """
Examples:
  python legacy_webforms_endpoint_client.py
  python legacy_webforms_endpoint_client.py --debug search AA-BB-CC-DD-EE-FF

  python legacy_webforms_endpoint_client.py search AA-BB-CC-DD-EE-FF

  python legacy_webforms_endpoint_client.py delete AA-BB-CC-DD-EE-FF
  python legacy_webforms_endpoint_client.py delete AA-BB-CC-DD-EE-FF --location SITE-A --yes

  python legacy_webforms_endpoint_client.py register AA-BB-CC-DD-EE-FF
  python legacy_webforms_endpoint_client.py register AA-BB-CC-DD-EE-FF --vlan VLAN_HOSTS_A
  python legacy_webforms_endpoint_client.py register AA-BB-CC-DD-EE-FF --vlan VLAN_HOSTS_A --yes

TLS:
  python legacy_webforms_endpoint_client.py --ca-bundle C:\\path\\corp-ca.pem
    search AA-BB-CC-DD-EE-FF
  python legacy_webforms_endpoint_client.py --insecure search AA-BB-CC-DD-EE-FF

HTML dumps:
  python legacy_webforms_endpoint_client.py --dump-html-dir ./debug-html --debug
    search AA-BB-CC-DD-EE-FF
""".strip()

    parser = argparse.ArgumentParser(
        description=(
            "API-like CLI wrapper for a legacy ASP.NET Web Forms endpoint portal"
        ),
        epilog=examples,
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Base URL of the legacy portal. Default: {DEFAULT_BASE_URL}",
    )

    parser.add_argument(
        "--ca-bundle",
        help="Path to corporate CA bundle PEM file. Preferred for internal TLS.",
    )

    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification. Use only when unavoidable.",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging.",
    )

    parser.add_argument(
        "--dump-html-dir",
        type=Path,
        help=(
            "Optional directory for HTML dumps. Disabled by default because pages "
            "may contain sensitive data."
        ),
    )

    sub = parser.add_subparsers(dest="command")

    search = sub.add_parser("search", help="Search endpoint by MAC")
    search.add_argument("mac", nargs="?")

    delete = sub.add_parser("delete", help="Delete endpoint by MAC")
    delete.add_argument("mac", nargs="?")
    delete.add_argument("--location", help="Delete location/context")
    delete.add_argument(
        "--yes",
        action="store_true",
        help="Do not prompt for final deletion confirmation",
    )

    register = sub.add_parser("register", help="Register endpoint by MAC")
    register.add_argument("mac", nargs="?")
    register.add_argument("--vlan", help="VLAN value to register")
    register.add_argument(
        "--yes",
        action="store_true",
        help="Do not prompt for final registration confirmation",
    )

    return parser


def configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )


def build_config_from_args(args: argparse.Namespace) -> ClientConfig:
    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        verify_tls: bool | str = False
    elif args.ca_bundle:
        verify_tls = args.ca_bundle
    else:
        verify_tls = True

    return ClientConfig(
        base_url=args.base_url.rstrip("/"),
        verify_tls=verify_tls,
        debug=args.debug,
        dump_html_dir=args.dump_html_dir,
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.debug)

    try:
        command = args.command or choose_command_interactive()
        config = build_config_from_args(args)

        if config.verify_tls is False:
            logging.warning(
                "TLS certificate verification is disabled. Credentials and "
                "operational data may be exposed to interception."
            )

        client = LegacyWebFormsClient(config)

        if command == "search":
            mac = prompt_if_missing(getattr(args, "mac", None), "Enter MAC", normalize_mac)
            status, results, _ = client.search_single_mac(mac)

        elif command == "delete":
            status, results, _ = client.delete_mac_interactive(
                mac=getattr(args, "mac", None),
                location=getattr(args, "location", None),
                assume_yes=getattr(args, "yes", False),
            )

        elif command == "register":
            status, results, _ = client.register_mac_interactive(
                mac=getattr(args, "mac", None),
                vlan=getattr(args, "vlan", None),
                assume_yes=getattr(args, "yes", False),
            )

        else:
            raise RuntimeError(f"Unknown command: {command}")

        print("\nSTATUS:", status)
        print("RESULT COUNT:", len(results))
        print_results_table(results)
        return 0

    except Exception as exc:
        print(f"\nERROR: {exc}")
        if args.debug:
            raise
        print("Tip: re-run with --debug for more details.")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())