from __future__ import annotations

import importlib.util
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import requests

MODULE_PATH = Path(__file__).parents[1] / "legacy_webforms_endpoint_client.py"
SPEC = importlib.util.spec_from_file_location("legacy_webforms_endpoint_client", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class FakeResponse:
    def __init__(self, text: str, url: str, status_code: int = 200):
        self.text = text
        self.url = url
        self.status_code = status_code
        self.history: list[FakeResponse] = []

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}",
                response=self,
            )


SEARCH_HTML = f"""
<html><body><form>
<input type='hidden' name='__VIEWSTATE' value='state'>
<input id='{module.SEARCH_MAC_TEXTBOX_ID}'
       name='{module.SEARCH_MAC_TEXTBOX_NAME}'>
<input type='submit' name='{module.SEARCH_BUTTON_NAME}' value='Search'>
</form></body></html>
"""

ADD_HTML = f"""
<html><body><form>
<input type='hidden' name='__VIEWSTATE' value='state'>
<select id='{module.ADD_LOCATION_ID}'></select>
<select id='{module.ADD_BU_ID}'></select>
<select id='{module.ADD_RESTRICT_ID}'></select>
</form></body></html>
"""


class HelperTests(unittest.TestCase):
    def test_normalize_mac_accepts_common_formats(self) -> None:
        expected = "AA-BB-CC-DD-EE-FF"
        for value in (
            "AABBCCDDEEFF",
            "AA:BB:CC:DD:EE:FF",
            "aa-bb-cc-dd-ee-ff",
            "AABB.CCDD.EEFF",
        ):
            with self.subTest(value=value):
                self.assertEqual(module.normalize_mac(value), expected)

    def test_extract_hidden_fields_preserves_split_and_custom_state(self) -> None:
        html = """
        <form>
          <input type="hidden" name="__VIEWSTATEFIELDCOUNT" value="2">
          <input type="hidden" name="__VIEWSTATE" value="part-0">
          <input type="hidden" name="__VIEWSTATE1" value="part-1">
          <input type="hidden" name="customToken" value="abc">
          <input type="text" name="visible" value="ignore-me">
        </form>
        """
        self.assertEqual(
            module.extract_hidden_fields(html),
            {
                "__VIEWSTATEFIELDCOUNT": "2",
                "__VIEWSTATE": "part-0",
                "__VIEWSTATE1": "part-1",
                "customToken": "abc",
            },
        )

    def test_result_header_aliases_are_supported(self) -> None:
        rows = [{"MAC Address": "AA:BB:CC:DD:EE:FF", "VLAN": "VLAN_HOSTS_A"}]
        self.assertEqual(module.matching_rows_for_mac("aabb.ccdd.eeff", rows), rows)
        self.assertEqual(
            module.get_row_value(rows[0], module.SEARCH_VLAN_COLUMN_NAMES),
            "VLAN_HOSTS_A",
        )


class ClientSafetyTests(unittest.TestCase):
    def make_client(self) -> module.LegacyWebFormsClient:
        config = module.ClientConfig(
            base_url="https://legacy-webforms.example.internal",
            verify_tls=True,
        )
        with patch.object(module, "build_windows_auth", return_value=object()):
            return module.LegacyWebFormsClient(config)

    def test_dom_detection_does_not_depend_on_quote_style(self) -> None:
        client = self.make_client()
        self.assertTrue(
            client.is_real_search_page(
                SEARCH_HTML,
                "https://legacy-webforms.example.internal/endpoint-admin/Search",
            )
        )

    def test_search_rejects_home_page_after_post(self) -> None:
        client = self.make_client()
        client.get_page_with_retry = Mock(
            return_value=(
                SEARCH_HTML,
                "https://legacy-webforms.example.internal/endpoint-admin/Search",
            )
        )
        client.session.post = Mock(
            return_value=FakeResponse(
                "<html><body>Home</body></html>",
                "https://legacy-webforms.example.internal/endpoint-admin/",
            )
        )

        with self.assertRaisesRegex(RuntimeError, "validated Search page"):
            client.search_single_mac("AA-BB-CC-DD-EE-FF")

    def test_delete_verification_does_not_treat_search_failure_as_absence(self) -> None:
        client = self.make_client()
        client.search_single_mac = Mock(side_effect=RuntimeError("redirected home"))

        verified, message, *_ = client.verify_deletion_by_search(
            "AA-BB-CC-DD-EE-FF",
            max_attempts=1,
            delay_sec=0,
        )

        self.assertFalse(verified)
        self.assertIn("could not be confirmed", message)

    def test_ambiguous_register_is_read_back_before_any_retry(self) -> None:
        client = self.make_client()
        add_url = "https://legacy-webforms.example.internal/endpoint-admin/Add"
        client.rebuild_add_state = Mock(return_value=(ADD_HTML, add_url))
        client.session.post = Mock(
            return_value=FakeResponse(ADD_HTML, add_url)
        )
        client.search_single_mac = Mock(
            return_value=(
                "Found",
                [{"MAC Address": "AA-BB-CC-DD-EE-FF", "VLAN": "VLAN_HOSTS_A"}],
                SEARCH_HTML,
            )
        )

        status, results, _ = client.submit_register_with_retry(
            mac="AA-BB-CC-DD-EE-FF",
            location="SITE-A",
            business_unit="BU-EXAMPLE",
            restrict_level="STANDARD",
            vlan="VLAN_HOSTS_A",
            max_attempts=3,
            delay_sec=0,
        )

        self.assertEqual(client.session.post.call_count, 1)
        self.assertIn("ambiguous response", status)
        self.assertEqual(len(results), 1)


    def test_ambiguous_register_is_not_repeated_when_readback_fails(self) -> None:
        client = self.make_client()
        add_url = "https://legacy-webforms.example.internal/endpoint-admin/Add"
        client.rebuild_add_state = Mock(return_value=(ADD_HTML, add_url))
        client.session.post = Mock(return_value=FakeResponse(ADD_HTML, add_url))
        client.search_single_mac = Mock(side_effect=RuntimeError("search unavailable"))

        with self.assertRaisesRegex(RuntimeError, "Refusing to repeat the write"):
            client.submit_register_with_retry(
                mac="AA-BB-CC-DD-EE-FF",
                location="SITE-A",
                business_unit="BU-EXAMPLE",
                restrict_level="STANDARD",
                vlan="VLAN_HOSTS_A",
                max_attempts=3,
                delay_sec=0,
            )

        self.assertEqual(client.session.post.call_count, 1)

    def test_permanent_http_error_is_not_retried(self) -> None:
        client = self.make_client()
        client.warm_up_home = Mock()
        client.session.get = Mock(
            return_value=FakeResponse(
                "Forbidden",
                "https://legacy-webforms.example.internal/endpoint-admin/Search",
                status_code=403,
            )
        )

        with self.assertRaises(requests.HTTPError):
            client.get_page_with_retry(
                client.config.search_url,
                referer=client.config.app_root_url,
                detector=client.is_real_search_page,
                label="SEARCH",
                max_attempts=5,
                delay_sec=0,
            )

        self.assertEqual(client.session.get.call_count, 1)


    def test_delete_confirmation_cancels_before_write(self) -> None:
        client = self.make_client()
        client.search_single_mac = Mock(
            return_value=(
                "Found",
                [{"MAC Address": "AA-BB-CC-DD-EE-FF", "VLAN": "VLAN_HOSTS_A"}],
                SEARCH_HTML,
            )
        )
        client.delete_mac = Mock()

        with patch("builtins.input", return_value="n"), redirect_stdout(StringIO()):
            status, _, _ = client.delete_mac_interactive(
                mac="AA-BB-CC-DD-EE-FF",
                location="SITE-A",
                assume_yes=False,
            )

        self.assertEqual(status, "Cancelled")
        client.delete_mac.assert_not_called()

    def test_unverified_registration_raises_error(self) -> None:
        client = self.make_client()
        add_html = f"""
        <html><body><form>
          <select id='{module.ADD_LOCATION_ID}'></select>
          <select id='{module.ADD_BU_ID}'></select>
          <select id='{module.ADD_RESTRICT_ID}'></select>
          <select id='{module.ADD_VLAN_ID}'>
            <option value='VLAN_HOSTS_A'>Hosts</option>
          </select>
        </form></body></html>
        """
        add_url = "https://legacy-webforms.example.internal/endpoint-admin/Add"
        client.get_current_vlan_for_mac = Mock(return_value=None)
        client.perform_add_step_with_retry = Mock(return_value=(add_html, add_url))
        client.submit_register_with_retry = Mock(
            return_value=("Submitted", [], add_html)
        )
        client.verify_registration_by_search = Mock(
            return_value=(False, "Not verified", "", [], "")
        )

        with (
            self.assertRaisesRegex(RuntimeError, "could not be verified"),
            redirect_stdout(StringIO()),
        ):
            client.register_mac_interactive(
                mac="AA-BB-CC-DD-EE-FF",
                vlan="VLAN_HOSTS_A",
                assume_yes=True,
            )

    def test_delete_yes_option_is_available(self) -> None:
        args = module.build_parser().parse_args(
            ["delete", "AA-BB-CC-DD-EE-FF", "--yes"]
        )
        self.assertTrue(args.yes)


if __name__ == "__main__":
    unittest.main()
