from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from unsplash_stats.dashboard import create_app


OLD_PROGRESS_OUTPUT_KEY = (
    "..action-status.children...collect-button.disabled...collect-button.children..."
    "progress-summary.children...progress-percent-text.children..."
    "progress-bar-fill.style...progress-calls-text.children..."
    "progress-endpoint-text.children...progress-updated-text.children..."
    "collection-refresh-token.data.."
)


class DashboardCallbackRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test.sqlite"
        self.app = create_app(self.db_path)
        self.client = self.app.server.test_client()
        self.card_callback_key, self.card_callback_meta = self._find_card_callback()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _find_card_callback(self) -> tuple[str, dict[str, Any]]:
        pattern_id = '{"photo_id":["ALL"],"type":"photo-card"}'
        for key, meta in self.app.callback_map.items():
            inputs = meta.get("inputs", [])
            if len(inputs) != 1:
                continue
            first_input = inputs[0]
            if first_input.get("id") != pattern_id:
                continue
            if first_input.get("property") != "n_clicks":
                continue
            return key, meta
        self.fail("Could not find photo-card selection callback.")

    def _post_old_progress_signature(self) -> tuple[int, str]:
        payload = {
            "output": OLD_PROGRESS_OUTPUT_KEY,
            "outputs": [
                {"id": "action-status", "property": "children"},
                {"id": "collect-button", "property": "disabled"},
                {"id": "collect-button", "property": "children"},
                {"id": "progress-summary", "property": "children"},
                {"id": "progress-percent-text", "property": "children"},
                {"id": "progress-bar-fill", "property": "style"},
                {"id": "progress-calls-text", "property": "children"},
                {"id": "progress-endpoint-text", "property": "children"},
                {"id": "progress-updated-text", "property": "children"},
                {"id": "collection-refresh-token", "property": "data"},
            ],
            "inputs": [
                {"id": "collect-button", "property": "n_clicks", "value": 0},
                {"id": "progress-interval", "property": "n_intervals", "value": 0},
            ],
            "state": [
                {"id": "collection-refresh-token", "property": "data", "value": 0}
            ],
            "changedPropIds": ["progress-interval.n_intervals"],
        }
        response = self.client.post(
            "/_dash-update-component",
            data=json.dumps(payload),
            content_type="application/json",
        )
        return response.status_code, response.get_data(as_text=True)

    def _post_card_callback(self, n_clicks_values: list[int]) -> tuple[int, str]:
        payload = {
            "output": self.card_callback_key,
            "outputs": {"id": "photo-dropdown", "property": "value"},
            "inputs": [
                {
                    "id": '{"photo_id":["ALL"],"type":"photo-card"}',
                    "property": "n_clicks",
                    "value": n_clicks_values,
                }
            ],
            "state": [
                {"id": "photo-dropdown", "property": "value", "value": "cmq8ghCppak"}
            ],
            "changedPropIds": ['{"type":"photo-card","photo_id":"fHiVjP-CsR0"}.n_clicks'],
        }
        response = self.client.post(
            "/_dash-update-component",
            data=json.dumps(payload),
            content_type="application/json",
        )
        return response.status_code, response.get_data(as_text=True)

    def test_photo_card_callback_signature_has_single_state(self) -> None:
        state = self.card_callback_meta.get("state", [])
        self.assertEqual(len(state), 1)
        self.assertEqual(state[0].get("id"), "photo-dropdown")
        self.assertEqual(state[0].get("property"), "value")

    def test_photo_card_callback_zero_click_payload_does_not_500(self) -> None:
        status_code, _body = self._post_card_callback([0, 0, 0])
        self.assertIn(status_code, (200, 204))

    def test_photo_card_callback_positive_click_payload_does_not_500(self) -> None:
        status_code, _body = self._post_card_callback([0, 1, 0])
        self.assertIn(status_code, (200, 204))

    def test_old_progress_signature_key_exists(self) -> None:
        self.assertIn(OLD_PROGRESS_OUTPUT_KEY, self.app.callback_map)

    def test_old_progress_signature_payload_does_not_500(self) -> None:
        status_code, _body = self._post_old_progress_signature()
        self.assertIn(status_code, (200, 204))

    def test_create_app_normalizes_ingress_path_prefix(self) -> None:
        ingress_prefix = "api/hassio_ingress/demo-token"
        app = create_app(self.db_path, requests_pathname_prefix=ingress_prefix)
        self.assertEqual(
            app.config.requests_pathname_prefix,
            "/api/hassio_ingress/demo-token/",
        )
        self.assertEqual(
            app.config.routes_pathname_prefix,
            "/api/hassio_ingress/demo-token/",
        )

        client = app.server.test_client()
        response = client.get("/api/hassio_ingress/demo-token/")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
