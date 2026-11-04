"""The API reference tells the truth about serve.py.

serve.py answers exactly the rows of api_spec.ROUTES. routes.py builds the service's dispatch from that table and serve.py
registers one handler for each row, so a route cannot be answered without a row nor listed without an answer: make_handler
refuses to build either way. These tests build the request handler on stand-in stores and hold its table to api_spec, check
that a request made from each row's examples reaches that row and only it, and that openapi.json and docs/API.md hold what the
table says.
"""
import ast
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api_spec  # noqa: E402
import routes  # noqa: E402
from tools import build_api_docs  # noqa: E402

SERVE = ROOT / "serve.py"
GUIDE = ROOT / "docs" / "API.md"


def strings_in(path):
    return {node.value for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def built_handler():
    """serve.py's request handler, built the way main() builds it but on stand-in stores in a temporary library."""
    import serve
    from cast_store import CastStore
    from worker_pool import read_catalog

    folder = tempfile.TemporaryDirectory(prefix="voice-lab-api-docs-")
    entries = {entry["id"]: entry for entry in read_catalog()["engines"]}
    engines = SimpleNamespace(entries=entries, api_entries=entries, device="cpu", loaded=[])
    handler = serve.make_handler(engines, 1, CastStore(Path(folder.name)), scene_store=SimpleNamespace(root=Path(folder.name)),
                                 batch_runner=SimpleNamespace(), evaluation_store=SimpleNamespace(),
                                 engine_installer=SimpleNamespace(close=lambda: None))
    return handler, folder


def example(row):
    """A request path for a row, its parameters filled with their documented examples."""
    if row.path.endswith("/*"):
        return row.path[:-1] + "example"
    return row.path.format(**{name: api_spec.PATH_PARAMS[name][2] for name in api_spec.path_names(row.path)})


class RouteTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler, cls.folder = built_handler()
        cls.router = handler.router

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_serve_answers_every_row_and_nothing_else(self):
        self.assertEqual(set(self.router.handlers), {(row.method, row.path) for row in api_spec.ROUTES})
        self.assertGreater(len(self.router.handlers), 100)

    def test_a_request_made_from_each_rows_examples_reaches_that_row(self):
        for row in api_spec.ROUTES:
            with self.subTest(f"{row.method} {row.path}"):
                route, handler, _params = self.router.match(row.method, example(row))
                self.assertIs(route, row)
                self.assertIs(handler, self.router.handlers[(row.method, row.path)])

    def test_a_parameter_that_breaks_its_documented_pattern_reaches_nothing(self):
        for row in api_spec.ROUTES:
            names = api_spec.path_names(row.path)
            if not names:
                continue
            path = row.path.format(**{name: "not a valid one!" for name in names})
            with self.subTest(f"{row.method} {row.path}"):
                found = self.router.match(row.method, path)
                self.assertTrue(found is None or found[0].path.endswith("/*"), found and found[0].path)

    def test_the_parameters_reach_the_handler_by_name(self):
        take = api_spec.PATH_PARAMS["take_id"][2]
        self.assertEqual(self.router.match("GET", f"/api/takes/{take}/audio")[2], {"take_id": take})
        scene, preview = api_spec.PATH_PARAMS["scene_id"][2], api_spec.PATH_PARAMS["preview_file"][2]
        self.assertEqual(self.router.match("GET", f"/api/scenes/{scene}/previews/{preview}.wav")[2],
                         {"scene_id": scene, "preview_file": preview})
        self.assertEqual(self.router.match("GET", "/out/conversions/a/converted.wav")[2], {"rest": "conversions/a/converted.wav"})

    def test_every_path_parameter_is_defined_and_every_definition_is_used(self):
        used = {name for row in api_spec.ROUTES for name in api_spec.path_names(row.path)}
        self.assertEqual(sorted(used - set(api_spec.PATH_PARAMS)), [])
        self.assertEqual(sorted(set(api_spec.PATH_PARAMS) - used), [])
        for name, (doc, pattern, example_value) in api_spec.PATH_PARAMS.items():
            with self.subTest(name):
                self.assertTrue(doc.endswith("."), "a parameter's meaning is a sentence")
                self.assertRegex(example_value, f"^(?:{pattern})$")

    def test_query_fields_and_answer_headers_are_names_serve_uses(self):
        names = strings_in(SERVE)
        for row in api_spec.documented():
            for field in row.query + row.headers:
                with self.subTest(f"{row.method} {row.path} {field.name}"):
                    self.assertIn(field.name, names)

    def test_request_fields_are_names_the_lab_reads(self):
        names = set()
        for path in ROOT.glob("*.py"):
            if path.name != "api_spec.py":
                names |= strings_in(path)
        for row in api_spec.documented():
            for field in row.body:
                with self.subTest(f"{row.method} {row.path} {field.name}"):
                    self.assertIn(field.name, names, "no Lab module reads a field with this name: a typo, or a field that went")


class RouterTests(unittest.TestCase):
    """routes.Router on small tables of its own, and make_handler with a table that has gained a row."""

    @staticmethod
    def rows(*paths):
        return [api_spec.R(method, path, "Service", "A route.") for method, path in paths]

    def test_a_handler_for_a_route_with_no_row_is_refused(self):
        router = routes.Router(self.rows(("GET", "/api/a")))
        with self.assertRaisesRegex(routes.RouteError, r"GET /api/brand-new has no row in api_spec\.ROUTES"):
            router.get("/api/brand-new")(lambda *args: None)

    def test_a_row_with_no_handler_is_refused(self):
        router = routes.Router(self.rows(("GET", "/api/a"), ("POST", "/api/b")))
        router.get("/api/a")(lambda *args: None)
        with self.assertRaisesRegex(routes.RouteError, r"no handler in serve\.py: POST /api/b$"):
            router.check()

    def test_a_row_listed_twice_or_answered_twice_is_refused(self):
        with self.assertRaisesRegex(routes.RouteError, "two rows"):
            routes.Router(self.rows(("GET", "/api/a"), ("GET", "/api/a")))
        router = routes.Router(self.rows(("GET", "/api/a")))
        router.get("/api/a")(lambda *args: None)
        with self.assertRaisesRegex(routes.RouteError, "two handlers"):
            router.get("/api/a")(lambda *args: None)

    def test_a_plain_path_wins_then_the_patterns_in_order_then_the_longest_prefix(self):
        take = api_spec.PATH_PARAMS["take_id"][2]
        router = routes.Router(self.rows(("GET", "/api/x/*"), ("GET", "/api/x/y/*"), ("GET", "/api/x/info"),
                                         ("GET", "/api/x/{take_id}")))
        for path, expected in (("/api/x/info", "/api/x/info"), (f"/api/x/{take}", "/api/x/{take_id}"),
                               ("/api/x/y/z", "/api/x/y/*"), ("/api/x/other", "/api/x/*")):
            with self.subTest(path):
                self.assertEqual(router.match("GET", path)[0].path, expected)
        self.assertIsNone(router.match("POST", "/api/x/info"))
        self.assertIsNone(router.match("GET", "/api/elsewhere"))

    def test_a_literal_part_of_a_path_is_not_read_as_a_pattern(self):
        router = routes.Router(self.rows(("GET", "/api/scenes/{scene_id}/export.json")))
        scene = api_spec.PATH_PARAMS["scene_id"][2]
        self.assertIsNotNone(router.match("GET", f"/api/scenes/{scene}/export.json"))
        self.assertIsNone(router.match("GET", f"/api/scenes/{scene}/exportxjson"))

    def test_make_handler_will_not_build_with_a_row_that_nobody_answers(self):
        extra = api_spec.R("GET", "/api/brand-new", "Service", "Answered by nobody.")
        with mock.patch.object(api_spec, "ROUTES", [*api_spec.ROUTES, extra]):
            with self.assertRaisesRegex(routes.RouteError, "GET /api/brand-new"):
                handler, folder = built_handler()
                folder.cleanup()


class TableTests(unittest.TestCase):
    def test_a_row_is_listed_once(self):
        keys = [api_spec.key(row) for row in api_spec.ROUTES]
        self.assertEqual(sorted(key for key in set(keys) if keys.count(key) > 1), [])
        ids = [api_spec.operation_id(row) for row in api_spec.documented()]
        self.assertEqual(len(ids), len(set(ids)))
        anchors = [api_spec.anchor(row) for row in api_spec.documented()]
        self.assertEqual(len(anchors), len(set(anchors)))

    def test_anchors_and_operation_ids_have_one_fixed_form(self):
        """The guide links to these by hand (#post-api-performance), and scripts keep operation ids."""
        rows = {api_spec.key(row): row for row in api_spec.ROUTES}
        self.assertEqual(api_spec.anchor(rows["POST /api/performance"]), "post-api-performance")
        self.assertEqual(api_spec.anchor(rows["GET /api/takes/{take_id}"]), "get-api-takes-take_id")
        self.assertEqual(api_spec.anchor(rows["GET /api/scenes/jobs/{job_id}"]), "get-api-scenes-jobs-job_id")
        self.assertEqual(api_spec.operation_id(rows["POST /api/performance"]), "post_api_performance")
        self.assertEqual(api_spec.operation_id(rows["GET /api/takes/{take_id}"]), "get_api_takes_take_id")

    def test_documented_and_internal_rows_split_the_table(self):
        documented = {api_spec.key(row) for row in api_spec.documented()}
        internal = {api_spec.key(row) for row in api_spec.internal()}
        self.assertEqual(documented & internal, set())
        self.assertEqual(documented | internal, {api_spec.key(row) for row in api_spec.ROUTES})
        self.assertTrue(all(row.internal for row in api_spec.internal()))
        self.assertFalse(any(row.internal for row in api_spec.documented()))

    def test_every_row_is_complete(self):
        groups = [name for name, _intro in api_spec.GROUPS]
        for row in api_spec.ROUTES:
            with self.subTest(api_spec.key(row)):
                self.assertIn(row.method, ("GET", "POST"))
                self.assertIn(row.group, groups)
                self.assertTrue(row.summary.endswith("."), "a summary is a sentence")
                self.assertIn(row.reply_kind, ("json", "wav", "ndjson", "csv", "audio", "html", "javascript", "file"))
                self.assertIn(row.body_kind, ("json", "audio", "none"))
                self.assertTrue(200 <= row.status < 300)
                self.assertFalse({status for status, _ in row.errors} & {row.status})
                if row.method == "GET":
                    self.assertFalse(row.body, "a GET has no body")
                if row.internal:
                    self.assertIn(row.group, ("Pages", "Page helpers", "Drama"))
                else:
                    self.assertTrue(row.reply, "a documented route says what it answers")
                    self.assertNotIn(row.group, ("Pages", "Page helpers", "Drama"))

    def test_fields_are_well_formed(self):
        for row in api_spec.ROUTES:
            for field in row.query + row.body + row.headers:
                with self.subTest(f"{api_spec.key(row)} {field.name}"):
                    self.assertRegex(field.type, r"^(string|integer|number|boolean|object)(\[\])?$")
                    self.assertTrue(field.doc.endswith((".", "`")), "a field's meaning is a sentence")
            names = [field.name for field in row.body]
            self.assertEqual(len(names), len(set(names)), api_spec.key(row))

    def test_a_cell_cannot_break_its_table_row(self):
        self.assertEqual(api_spec._cell("a|b\nc"), "a\\|b c")
        self.assertEqual(api_spec._table(["A", "B"], [("x|y", 2)]), ["| A | B |", "|---|---|", "| x\\|y | 2 |"])

    def test_the_two_calls_scripts_depend_on_are_documented_and_keep_their_shape(self):
        speak = next(row for row in api_spec.documented() if api_spec.key(row) == "POST /api/speak")
        self.assertEqual(speak.reply_kind, "wav")
        self.assertIn("blocks", speak.reply)
        self.assertEqual([field.name for field in speak.body if field.required], ["text"])
        perform = next(row for row in api_spec.documented() if api_spec.key(row) == "POST /api/performance")
        self.assertEqual(perform.reply_kind, "wav")
        self.assertEqual([field.name for field in perform.body if field.required], ["identity_id", "text", "line_id"])
        self.assertIn("X-Take-Id", [field.name for field in perform.headers])


class OpenApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = api_spec.build_openapi()

    def operations(self):
        for path, methods in self.spec["paths"].items():
            for method, operation in methods.items():
                yield path, method, operation

    def test_it_says_what_it_is(self):
        self.assertEqual(self.spec["openapi"], "3.1.0")
        self.assertEqual(self.spec["info"]["version"], api_spec.VERSION)
        self.assertEqual(self.spec["servers"][0]["url"], "http://127.0.0.1:8765")
        self.assertEqual(self.spec["info"]["license"]["identifier"], "MIT")
        self.assertEqual(self.spec["components"]["schemas"]["Error"],
                         {"type": "object", "required": ["error"],
                          "properties": {"error": {"type": "string", "description": "What went wrong."}}})

    def test_documented_routes_are_paths_and_internal_ones_are_only_listed(self):
        described = {(method.upper(), path) for path, method, _ in self.operations()}
        self.assertEqual(described, {(row.method, row.path) for row in api_spec.documented()})
        self.assertEqual(self.spec["x-internal-routes"], sorted(api_spec.key(row) for row in api_spec.internal()))

    def test_every_operation_is_complete(self):
        tags = {tag["name"] for tag in self.spec["tags"]}
        ids = []
        for path, method, operation in self.operations():
            with self.subTest(f"{method} {path}"):
                ids.append(operation["operationId"])
                self.assertTrue(operation["summary"])
                self.assertTrue(set(operation["tags"]) <= tags)
                self.assertIn("default", operation["responses"])
                successes = [code for code in operation["responses"] if code.isdigit() and code.startswith("2")]
                self.assertEqual(len(successes), 1)
                for code in operation["responses"]:
                    self.assertTrue(code == "default" or re.fullmatch(r"[1-5]\d\d", code), code)
        self.assertEqual(len(ids), len(set(ids)))

    def test_path_parameters_are_declared_exactly_as_the_template_names_them(self):
        for path, method, operation in self.operations():
            with self.subTest(f"{method} {path}"):
                declared = [item["name"] for item in operation.get("parameters", []) if item["in"] == "path"]
                self.assertEqual(declared, api_spec.path_names(path))
                for item in operation.get("parameters", []):
                    if item["in"] == "path":
                        self.assertTrue(item["required"])
                        self.assertRegex(item["example"], item["schema"]["pattern"])
                        self.assertEqual(item["schema"]["pattern"], f'^{api_spec.PATH_PARAMS[item["name"]][1]}$',
                                         "anchored, so a value with anything around the id does not validate")

    def test_tags_are_the_groups_of_the_documented_routes_and_no_others(self):
        used = {tag for _path, _method, operation in self.operations() for tag in operation["tags"]}
        self.assertEqual({tag["name"] for tag in self.spec["tags"]}, used)
        self.assertEqual({tag["name"]: tag["description"] for tag in self.spec["tags"]},
                         dict(api_spec.groups_in_use(api_spec.documented())))
        self.assertTrue(all(tag["description"] for tag in self.spec["tags"]))
        self.assertFalse(used & {"Pages", "Page helpers", "Drama"}, "internal groups are not part of the API")

    def test_each_operation_carries_everything_its_row_says(self):
        media = {"json": "application/json", "wav": "audio/wav", "ndjson": "application/x-ndjson", "csv": "text/csv",
                 "audio": "audio/*", "html": "text/html", "javascript": "text/javascript", "file": "application/octet-stream"}

        def schema_of(kind):
            return {"type": "array", "items": {"type": kind[:-2]}} if kind.endswith("[]") else {"type": kind}

        for row in api_spec.documented():
            operation = self.spec["paths"][row.path][row.method.lower()]
            with self.subTest(api_spec.key(row)):
                self.assertEqual(operation["summary"], row.summary)
                self.assertEqual(operation["tags"], [row.group])
                self.assertEqual(operation.get("description", ""), row.notes)
                parameters = operation.get("parameters", [])
                self.assertEqual([(item["name"], item["required"], item["description"], item["schema"])
                                  for item in parameters if item["in"] == "query"],
                                 [(item.name, item.required, item.doc, schema_of(item.type)) for item in row.query])
                body = operation.get("requestBody")
                if row.body_kind == "json" and row.body:
                    schema = body["content"]["application/json"]["schema"]
                    self.assertEqual(list(schema["properties"]), [item.name for item in row.body])
                    self.assertEqual(schema.get("required", []), [item.name for item in row.body if item.required])
                    for item in row.body:
                        described = dict(schema["properties"][item.name])
                        self.assertEqual(described.pop("description"), item.doc)
                        self.assertEqual(described, schema_of(item.type))
                elif row.body_kind == "audio":
                    self.assertEqual(list(body["content"]), ["application/octet-stream"])
                    self.assertIn("X-Reference-Metadata", [item["name"] for item in parameters if item["in"] == "header"])
                else:
                    self.assertNotIn("requestBody", operation)
                responses = operation["responses"]
                self.assertEqual(set(responses) - {"default"}, {str(row.status)} | {str(status) for status, _ in row.errors})
                self.assertEqual(responses["default"], {"$ref": "#/components/responses/Error"})
                for status, meaning in row.errors:
                    self.assertEqual(responses[str(status)]["description"], meaning)
                    self.assertEqual(responses[str(status)]["content"]["application/json"]["schema"],
                                     {"$ref": "#/components/schemas/Error"})
                success = responses[str(row.status)]
                self.assertEqual(success["description"], row.reply or "The answer.")
                self.assertEqual(success["content"],
                                 {media[row.reply_kind]: {"schema": {"type": "object"} if row.reply_kind == "json"
                                                          else {"type": "string", "format": "binary"}}})
                self.assertEqual({name: item["description"] for name, item in success.get("headers", {}).items()},
                                 {item.name: item.doc for item in row.headers})

    def test_every_reference_resolves(self):
        def refs(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "$ref":
                        yield value
                    else:
                        yield from refs(value)
            elif isinstance(node, list):
                for value in node:
                    yield from refs(value)
        found = list(refs(self.spec))
        self.assertTrue(found)
        for reference in found:
            node = self.spec
            for part in reference.removeprefix("#/").split("/"):
                node = node[part]

    def test_request_bodies_require_only_fields_they_describe(self):
        for path, method, operation in self.operations():
            body = operation.get("requestBody")
            if not body:
                continue
            content = body["content"]
            with self.subTest(f"{method} {path}"):
                if "application/json" in content:
                    schema = content["application/json"]["schema"]
                    self.assertTrue(set(schema.get("required", [])) <= set(schema["properties"]))
                    self.assertEqual(body["required"], bool(schema.get("required")))
                else:
                    self.assertIn("application/octet-stream", content)

    def test_it_is_plain_json_that_a_script_can_read(self):
        self.assertEqual(json.loads(api_spec.openapi_text()), self.spec)
        self.assertTrue(api_spec.openapi_text().endswith("}\n"))


def slug(heading):
    """The anchor GitHub gives a heading."""
    text = re.sub(r"[`*]", "", heading.strip().lower())
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", text))


class GeneratedFilesTests(unittest.TestCase):
    def test_openapi_json_and_the_reference_are_what_the_table_gives(self):
        self.assertEqual((ROOT / "openapi.json").read_text(encoding="utf-8"), api_spec.openapi_text(),
                         "openapi.json is out of date; run tools/build_api_docs.py")
        self.assertIn(api_spec.render_reference(), GUIDE.read_text(encoding="utf-8"),
                      "docs/API.md is out of date; run tools/build_api_docs.py")
        behind = [path.relative_to(ROOT).as_posix() for path in build_api_docs.stale()]
        self.assertEqual(behind, [], "these are out of date; run tools/build_api_docs.py")

    def test_the_guide_has_one_generated_block_and_keeps_the_text_around_it(self):
        text = GUIDE.read_text(encoding="utf-8")
        self.assertEqual(text.count(build_api_docs.BEGIN), 1)
        self.assertEqual(text.count(build_api_docs.END), 1)
        self.assertLess(text.index(build_api_docs.BEGIN), text.index(build_api_docs.END))
        regenerated = build_api_docs.guide_text(text)
        self.assertEqual(regenerated, text)
        before, after = text.split(build_api_docs.BEGIN)[0], text.split(build_api_docs.END)[1]
        self.assertTrue(regenerated.startswith(before) and regenerated.endswith(after))

    def test_the_tool_writes_and_checks(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            guide, spec = Path(temp) / "API.md", Path(temp) / "openapi.json"
            guide.write_text(GUIDE.read_text(encoding="utf-8").replace(api_spec.render_reference(), "stale\n"), encoding="utf-8")
            spec.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(build_api_docs, "GUIDE", guide), mock.patch.object(build_api_docs, "OPENAPI", spec), \
                    contextlib.redirect_stdout(io.StringIO()) as said:
                self.assertEqual(build_api_docs.main(["--check"]), 1)
                self.assertEqual(spec.read_text(encoding="utf-8"), "{}\n", "--check must not write")
                self.assertIn("openapi.json is out of date", said.getvalue())
                self.assertEqual(build_api_docs.main([]), 0)
                self.assertEqual(build_api_docs.main(["--check"]), 0)
                self.assertEqual(spec.read_text(encoding="utf-8"), api_spec.openapi_text())
                self.assertIn("### Internal routes", guide.read_text(encoding="utf-8"))
                guide.write_text("no markers here\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    build_api_docs.main([])

    def test_links_inside_the_guide_point_at_headings_that_exist(self):
        text = GUIDE.read_text(encoding="utf-8")
        anchors = set(re.findall(r'<a id="([^"]+)"></a>', text))
        anchors |= {slug(match) for match in re.findall(r"^#{1,6} (.+)$", text, re.M)}
        for target in re.findall(r"\]\(#([^)\s]+)\)", text):
            with self.subTest(target):
                self.assertIn(target, anchors)

    def test_a_guide_that_has_lost_its_markers_is_refused_not_guessed_at(self):
        text = GUIDE.read_text(encoding="utf-8")
        begin, end = build_api_docs.BEGIN, build_api_docs.END
        for name, broken in (("no end", text.replace(end, "")), ("no begin", text.replace(begin, "")),
                             ("end first", text.replace(begin, "@@").replace(end, begin).replace("@@", end))):
            with self.subTest(name), self.assertRaises(ValueError):
                build_api_docs.guide_text(broken)

    def test_the_files_are_written_with_plain_line_ends_and_a_missing_one_is_out_of_date(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            guide, spec = Path(temp) / "API.md", Path(temp) / "openapi.json"
            guide.write_text(GUIDE.read_text(encoding="utf-8").replace(api_spec.render_reference(), "stale\n"), encoding="utf-8")
            with mock.patch.object(build_api_docs, "GUIDE", guide), mock.patch.object(build_api_docs, "OPENAPI", spec), \
                    contextlib.redirect_stdout(io.StringIO()) as said:
                self.assertEqual(build_api_docs.main(["--check"]), 1)
                self.assertIn("openapi.json is out of date", said.getvalue())
                self.assertIn("API.md is out of date", said.getvalue())
                self.assertFalse(spec.exists(), "--check does not write")
                self.assertEqual(sorted(path.name for path in build_api_docs.stale()), ["API.md", "openapi.json"])
                self.assertEqual(build_api_docs.main([]), 0)
                self.assertEqual(build_api_docs.stale(), [])
            for path in (guide, spec):
                self.assertNotIn(b"\r", path.read_bytes(), f"{path.name} is written with LF line ends on every system")
            self.assertEqual(guide.read_text(encoding="utf-8"), GUIDE.read_text(encoding="utf-8"))

    def sections(self):
        """Each documented route's key -> (the group heading above its section, the section's text)."""
        text = GUIDE.read_text(encoding="utf-8")
        found = {}
        for row in api_spec.documented():
            marker = f'<a id="{api_spec.anchor(row)}"></a>'
            self.assertEqual(text.count(marker), 1, f"{api_spec.key(row)} must have exactly one section")
            start = text.index(marker)
            later = re.search(r'<a id="|^### ', text[start + 1:], re.M)
            section = text[start:start + 1 + later.start()] if later else text[start:]
            found[api_spec.key(row)] = (re.findall(r"^### (.+)$", text[:start], re.M)[-1], section)
        return found

    def test_every_documented_route_has_its_section(self):
        text = GUIDE.read_text(encoding="utf-8")
        for row in api_spec.documented():
            with self.subTest(api_spec.key(row)):
                self.assertEqual(text.count(f"#### `{row.method} {row.path}`"), 1)
        self.assertEqual(len(self.sections()), len(api_spec.documented()))

    def test_each_section_sits_in_its_group_and_says_everything_its_row_says(self):
        sections = self.sections()
        kinds = {"json": "JSON", "wav": "`audio/wav`", "ndjson": "`application/x-ndjson`", "csv": "`text/csv`",
                 "audio": "audio", "html": "HTML", "javascript": "JavaScript", "file": "a file"}
        for row in api_spec.documented():
            heading, section = sections[api_spec.key(row)]
            with self.subTest(api_spec.key(row)):
                self.assertEqual(heading, row.group)
                self.assertIn(f"#### `{row.method} {row.path}`", section)
                self.assertIn(row.summary, section)
                self.assertIn(f"**Answer:** `{row.status}`, {kinds[row.reply_kind]}.", section)
                for name in api_spec.path_names(row.path):
                    self.assertIn(f"| `{name}` |", section)
                for field in row.query + row.body:
                    self.assertIn(f"| `{field.name}`" + (" (required)" if field.required else " |"), section)
                for field in row.headers:
                    self.assertIn(f"| `{field.name}` |", section)
                for status, _meaning in row.errors:
                    self.assertIn(f"| `{status}` |", section)
                self.assertIn(row.reply, section)
                self.assertIn(row.notes, section)

    def test_each_group_has_its_introduction_and_a_table_of_its_routes(self):
        text = GUIDE.read_text(encoding="utf-8")
        for name, intro in api_spec.groups_in_use(api_spec.documented()):
            with self.subTest(name):
                heading = f"### {name}\n\n{intro}\n\n| Method | Path | What it does |\n"
                self.assertEqual(text.count(heading), 1)
                table = text.split(heading)[1].split("\n\n")[0]
                rows = [row for row in api_spec.documented() if row.group == name]
                self.assertEqual(table.splitlines()[1:], [
                    f"| {row.method} | [`{row.path}`](#{api_spec.anchor(row)}) | {row.summary} |" for row in rows])

    def test_every_table_row_has_as_many_cells_as_its_header(self):
        """A | inside a cell, unescaped, splits it: GitHub then shows a ragged table."""
        block = []
        for line in GUIDE.read_text(encoding="utf-8").splitlines() + [""]:
            if line.startswith("|"):
                block.append(line)
            elif block:
                widths = {len(re.findall(r"(?<!\\)\|", row)) for row in block}
                self.assertEqual(len(widths), 1, f"a ragged table: {block[0]}")
                block = []

    def test_the_internal_routes_are_listed_apart_from_the_documented_ones(self):
        text = GUIDE.read_text(encoding="utf-8")
        _, found, internal = text.partition("### Internal routes")
        self.assertTrue(found)
        internal = internal.split(build_api_docs.END)[0]
        for row in api_spec.internal():
            with self.subTest(api_spec.key(row)):
                self.assertIn(f"| {row.method} | `{row.path}` |", internal)
                self.assertNotIn(f'<a id="{api_spec.anchor(row)}"></a>', text)
        for row in api_spec.documented():
            with self.subTest(api_spec.key(row)):
                self.assertNotIn(f"| {row.method} | `{row.path}` |", internal)

    def test_the_readme_and_user_guide_point_at_the_api_page(self):
        for name in ("README.md", "docs/USER_GUIDE.md"):
            with self.subTest(name):
                self.assertIn("API.md", (ROOT / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
