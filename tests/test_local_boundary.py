"""What the service lets a browser do. Another website may open the Lab's pages in a tab and make no other request, a path in a
request is checked before Windows is asked about it, and a route that fails in a way it does not handle still answers. The real
handler runs on a temporary library with fake engines."""
import pathlib
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import export_panel
import reference_catalog
from api_service import Service

# What a browser sends for an <audio> on another website, for a link on another website, and for the Lab's own page.
AUDIO_ON_ANOTHER_SITE = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "audio"}
LINK_ON_ANOTHER_SITE = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
FROM_THE_LAB = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
NETWORK_PATH = "\\\\attacker.example\\share\\x.wav"


def network_path_guard(asked):
    """A stand-in for Path.resolve: a path that names a network share is added to `asked` and refused, so a test never contacts
    one, and every other path resolves as usual."""
    real = pathlib.Path.resolve

    def resolve(path, strict=False):
        if str(path).startswith(("\\\\", "//")):
            asked.append(str(path))
            raise OSError("a test may not contact a network share")
        return real(path, strict=strict)
    return resolve


class LocalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.service = Service(self)
        self.addCleanup(self.service.server.RequestHandlerClass.close_drama)

    def status(self, path, headers=None, method="GET", body=None):
        return self.service.request(method, path, body=body, headers=headers)[0]

    def test_another_website_may_open_the_labs_pages_and_make_no_other_request(self):
        self.assertEqual(self.status("/api/health", AUDIO_ON_ANOTHER_SITE), 403)
        self.assertEqual(self.status("/api/health", {**AUDIO_ON_ANOTHER_SITE, "Sec-Fetch-Site": "same-site"}), 403)
        self.assertEqual(self.status("/api/health", LINK_ON_ANOTHER_SITE), 403)
        self.assertEqual(self.status("/", {**LINK_ON_ANOTHER_SITE, "Sec-Fetch-Dest": "iframe"}), 403)
        for page in ("/", "/compare"):
            with self.subTest(page=page):
                self.assertEqual(self.status(page, LINK_ON_ANOTHER_SITE), 200)

    def test_the_labs_own_pages_a_typed_address_and_a_script_still_get_through(self):
        typed = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        for headers in (FROM_THE_LAB, typed, {}):
            with self.subTest(headers=headers):
                self.assertEqual(self.status("/api/health", headers), 200)

    def test_every_answer_says_it_may_not_be_framed_or_read_as_another_type(self):
        for path in ("/", "/api/health", "/api/nothing-here"):
            with self.subTest(path=path):
                _status, headers, _body = self.service.request("GET", path)
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertEqual(headers["Content-Security-Policy"], "frame-ancestors 'none'")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_a_network_path_in_a_request_is_refused_before_windows_is_asked_about_it(self):
        asked = []
        with mock.patch.object(pathlib.Path, "resolve", network_path_guard(asked)):
            self.assertEqual(self.status("/out///attacker.example/share/x.wav"), 404)
            self.assertEqual(self.status("/api/reference-audio?path=%5C%5Cattacker.example%5Cshare%5Cx.wav"), 404)
            with self.assertRaises(ValueError):
                reference_catalog.resolve_reference_audio(NETWORK_PATH, ROOT, self.service.cast)
        self.assertEqual(asked, [])

    def test_out_serves_only_plain_names_below_it(self):
        for path in ("/out/../serve.py", "/out/a\\..\\..\\serve.py", "/out/C:/Windows/win.ini", "/out//x.wav"):
            with self.subTest(path=path):
                self.assertEqual(self.status(path), 404)

    def test_a_route_that_fails_in_a_way_it_does_not_handle_still_answers(self):
        status, _headers, body = self.service.request("POST", "/api/drama3/jobs/" + "0" * 32 + "/cancel", body={})
        self.assertEqual(status, 404)
        self.assertIn("Drama job not found", body.decode("utf-8"))
        with mock.patch.object(self.service.cast, "list_cast", side_effect=RuntimeError("database is locked")):
            status, _headers, body = self.service.request("GET", "/api/cast")
        self.assertEqual((status, body), (500, b'{"error": "RuntimeError: database is locked"}'))
        with mock.patch.object(export_panel, "run", side_effect=OSError("the disk is full")):
            status, _headers, body = self.service.request("POST", "/api/exports/preview", body={})
        self.assertEqual((status, body), (500, b'{"error": "OSError: the disk is full"}'))


if __name__ == "__main__":
    unittest.main()
