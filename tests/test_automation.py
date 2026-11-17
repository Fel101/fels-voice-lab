"""Check persistent recipe progress and approval before a provider gets content."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import automation
import connector_profiles
from tests import test_openai_route
from tests.test_browser_flow import BROWSER, DevToolsPage


def profile():
    return {
        "id": "provider-test",
        "base_url": "https://fixture.example/v1",
        "model": "test",
        "capabilities": ["translate", "speak"],
    }


def recipe(*steps):
    return {"schema_version": 1, "name": "Local recipe", "input_kind": "text", "steps": list(steps)}


class RecipeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(mock.patch.dict("os.environ", {"VOICE_LAB_PROVIDER_FILE": ""}))
        self.enterContext(mock.patch.object(connector_profiles, "SETTINGS", self.root / "profiles.json"))
        self.enterContext(mock.patch.object(connector_profiles, "_session_keys", {}))

    def test_two_steps_using_one_provider_disclose_all_fields_and_require_approval(self):
        provider = connector_profiles.save(profile(), "fixture-key")
        self.assertNotIn("fixture-key", (self.root / "profiles.json").read_text())
        self.assertNotIn("session_key", provider)
        store = automation.Store(self.root)
        saved = store.save(
            recipe(
                {
                    "operation": "translate",
                    "settings": {"source_language": "en", "target_language": "es", "provider": provider["id"]},
                },
                {"operation": "speak", "settings": {"voice": "test", "provider": provider["id"]}},
            )
        )
        payload = {"recipe_id": saved["id"], "inputs": ["Fixture words."]}
        [approval] = store.plan(payload)["provider_approvals"]
        self.assertEqual(approval["destination"], "fixture.example")
        self.assertIn("language pair", approval["data"])
        self.assertIn("voice and model", approval["data"])
        with self.assertRaisesRegex(ValueError, "explicitly approve"):
            store.submit(payload)
        run = store.submit({**payload, "approve_provider_revisions": [approval["revision"]]})
        self.assertEqual(run["recipe"]["steps"][0]["connector"]["revision"], approval["revision"])
        connector_profiles.save({**profile(), "model": "different"}, "fixture-key")
        with self.assertRaisesRegex(ValueError, "settings changed"):
            store.submit({**payload, "approve_provider_revisions": [approval["revision"]]})

    def test_reopening_a_recipe_run_preserves_the_completed_step(self):
        store = automation.Store(self.root)
        saved = store.save(
            recipe(
                {"operation": "speak", "settings": {"voice": "test"}},
                {"operation": "transcribe"},
            )
        )
        started = store.submit({"recipe_id": saved["id"], "inputs": ["Read locally."]})
        step = store.claim()
        store.checkpoint(step, "queued", 1, {"kind": "audio", "value": "fixture asset"})
        reopened = automation.Store(self.root)
        runner = automation.Runner(
            reopened,
            lambda _store, action, current, _cancelled: {
                "kind": action["output_kind"],
                "value": current["value"] + " reviewed",
            },
        )
        self.assertTrue(runner.tick())
        item = reopened.get(started["id"])["items"][0]
        self.assertEqual([step["value"] for step in item["steps"]], ["fixture asset", "fixture asset reviewed"])


class ProviderRoutes(test_openai_route.RouteCase):
    def test_saving_and_previewing_a_profile_stay_local_and_connect_requires_approval(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.dict("os.environ", {"VOICE_LAB_PROVIDER_FILE": ""}),
            mock.patch.object(connector_profiles, "SETTINGS", Path(folder) / "profiles.json"),
            mock.patch.object(connector_profiles, "_session_keys", {}),
            mock.patch.object(connector_profiles, "exchange", return_value=b'{"data":[{"id":"test"}]}') as exchange,
        ):
            code, _, body = self.service.request(
                "POST", "/api/automation/providers", {"profile": profile(), "key": "fixture-key"}
            )
            self.assertEqual(code, 201, body)
            saved = json.loads(body)["provider"]
            self.assertEqual(self.service.get_json("/api/automation/providers")["providers"], [saved])
            refused = self.service.request("POST", "/api/automation/providers/check", {"provider": saved["id"]})
            self.assertEqual(refused[0], 400)
            exchange.assert_not_called()
            allowed = self.service.request(
                "POST",
                "/api/automation/providers/check",
                {
                    "provider": saved["id"],
                    "approve_provider_revision": saved["revision"],
                },
            )
            self.assertEqual(allowed[0], 200, allowed[2])
            self.assertTrue(json.loads(allowed[2])["model_visible"])
            exchange.assert_called_once()


@unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
class AutomationBrowser(test_openai_route.RouteCase):
    def test_save_preview_and_run_a_branched_recipe(self):
        page = DevToolsPage(BROWSER)
        self.addCleanup(page.close)
        lab = self.service.server.RequestHandlerClass.lab
        self.addCleanup(lambda: lab._automation.close() if lab._automation else None)
        page.open(self.service.base)
        page.show("automation")
        page.set_value("#automationName", "Two local outputs")
        page.set_value("#automationText", "One local test.")
        page.set_value("#automationOptions", "speak")
        page.click("#automationAdd")
        page.set_value("#automationSteps input[list]", "af_heart")
        page.set_value("#automationSource", "0")
        page.set_value("#automationOptions", "speak")
        page.click("#automationAdd")
        page.set_value("#automationSteps fieldset:nth-child(2) input[list]", "ef_dora")
        page.click("#automationSave")
        page.wait_until("document.querySelector('#automationChoice').selectedOptions[0].text === 'Two local outputs'")
        page.click("#automationPreview")
        page.wait_until("document.querySelectorAll('#automationPlan li').length === 2")
        self.assertTrue(
            page.evaluate(
                "Array.from(document.querySelectorAll('#automationPlan li')).every(li => li.textContent.startsWith('Original input'))"
            )
        )
        page.click("#automationRun")
        page.wait_until("document.querySelector('#automationStatus').textContent === 'Run queued.'")
        page.wait_until("(async () => (await (await fetch('/api/automation/runs')).json()).runs[0].status === 'completed')()")
        page.click("#automationRefresh")
        page.wait_until("document.querySelectorAll('#automationRuns a').length === 2")
        paths = page.evaluate("Array.from(document.querySelectorAll('#automationRuns a'), a => a.getAttribute('href'))")
        for path in paths:
            self.assertEqual(self.service.request("GET", path)[2][:4], b"RIFF")
        self.assertEqual(page.problems, [])
