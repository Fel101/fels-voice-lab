"""Read the routes serve.py answers out of its source, without running it.

serve.py chooses a handler with a chain of tests on the request path inside do_GET and do_POST: `path == "/api/health"`,
`path in (...)`, `re.fullmatch(r"/api/takes/[a-f0-9]{32}", path)`, `path.startswith("/out/")` and
`path.lstrip("/") in PAGE_SCRIPTS`. scan() finds those and returns the routes they stand for, so that test_api_docs.py can hold
the route table in api_spec.py to what the service really answers. Nothing in serve.py was rewritten for this.

It is a static scan, so what it cannot read it must not miss quietly: every string in the two functions that starts with /api/
has to be a route the scan found, or be named in a NOT_ROUTES list the test keeps. A route written in some new way still
shows up there, as long as its path is spelled out.
"""
import ast
import re
from dataclasses import dataclass

HANDLERS = ("do_GET", "do_POST")


@dataclass(frozen=True)
class Found:
    method: str        # GET or POST
    kind: str          # exact, pattern or prefix
    text: str          # the path, the regular expression, or the prefix followed by *
    routes: tuple      # what it stands for: (shape, fragments) pairs, a parameter written {} in the shape
    regex: str = ""    # for a pattern, the whole regular expression a request path is matched against


@dataclass(frozen=True)
class Scan:
    found: tuple
    strings: frozenset    # every string in the handlers that starts with /api/

    def shapes(self):
        """{(method, shape): [fragments, ...]}: each route the source answers, with the patterns of its parameters."""
        result = {}
        for item in self.found:
            for shape, fragments in item.routes:
                result.setdefault((item.method, shape), []).append(fragments)
        return result

    def unexplained(self, not_routes=()):
        """Strings that start with /api/ and are neither a route the scan found nor one the caller named."""
        explained = set(not_routes)
        for item in self.found:
            explained.add(item.text[:-1] if item.kind == "prefix" else item.text)
        return sorted(self.strings - explained)


# ----------------------------------------------------------------------------------------------------- path regular expressions

def _class_end(text, start):
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == "]":
            return index + 1
        else:
            index += 1
    raise ValueError(f"unclosed character class in {text!r}")


def _quantifier_end(text, index):
    if index < len(text) and text[index] in "+*?":
        return index + 1
    match = re.compile(r"\{\d+(?:,\d*)?\}").match(text, index)
    return match.end() if match else index


def _group_end(text, start):
    depth, index = 0, start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            index = _class_end(text, index)
            continue
        depth += (char == "(") - (char == ")")
        if depth == 0:
            return index
        index += 1
    raise ValueError(f"unclosed group in {text!r}")


def _split_alternatives(text):
    parts, depth, index, start = [], 0, 0, 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            index = _class_end(text, index)
            continue
        depth += (char == "(") - (char == ")")
        if char == "|" and depth == 0:
            parts.append(text[start:index])
            start = index + 1
        index += 1
    parts.append(text[start:])
    return parts


def _sequence(text):
    results = [("", ())]
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            if index + 1 >= len(text) or text[index + 1].isalnum():
                raise ValueError(f"cannot read the escape in {text!r}")
            options, index = [(text[index + 1], ())], index + 2        # \. is a dot
        elif char == "[":
            end = _quantifier_end(text, _class_end(text, index))
            options, index = [("{}", (text[index:end],))], end
        elif char == "(":
            end = _group_end(text, index)
            body = text[index + 1:end]
            index = end + 1
            if body.startswith("?:"):
                body = body[2:]
            if re.search(r"\[|\\[dws]", body):                          # holds a character class: a parameter
                options = [("{}", (body,))]
            else:                                                       # literal words, maybe nested: choices
                options = [pair for part in _split_alternatives(body) for pair in _sequence(part)]
                if index < len(text) and text[index] == "?":
                    options, index = [("", ())] + options, index + 1
        elif char in "|{}+*?^$.":
            raise ValueError(f"cannot read {char!r} in {text!r}")
        else:
            options, index = [(char, ())], index + 1
        results = [(first + second, before + after) for first, before in results for second, after in options]
    return results


def expand(pattern):
    """The routes a path regular expression stands for, as (shape, fragments): a group of plain alternatives gives one route
    for each, an optional group gives two, and any other group or character class is a parameter, written {} in the shape
    with the pattern it matches kept in fragments."""
    return _sequence(pattern)


# ----------------------------------------------------------------------------------------------------- the source

def _module_strings(tree):
    """{NAME: strings} for module-level NAME = frozenset({"a", "b"}) and the like."""
    result = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.args:
                value = value.args[0]
            if isinstance(value, (ast.Set, ast.Tuple, ast.List)) and all(
                    isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.elts):
                result[node.targets[0].id] = tuple(item.value for item in value.elts)
    return result


def _is_path(node):
    if isinstance(node, ast.Name):
        return node.id == "path"
    return (isinstance(node, ast.Attribute) and node.attr == "path"
            and isinstance(node.value, ast.Name) and node.value.id in ("self", "parsed"))


def _remainder_of(node):
    """The prefix when node is path[len("/prefix/"):], the rest of the path after a prefix."""
    if (isinstance(node, ast.Subscript) and _is_path(node.value) and isinstance(node.slice, ast.Slice)
            and node.slice.upper is None and isinstance(node.slice.lower, ast.Call)
            and isinstance(node.slice.lower.func, ast.Name) and node.slice.lower.func.id == "len"
            and len(node.slice.lower.args) == 1 and isinstance(node.slice.lower.args[0], ast.Constant)):
        return node.slice.lower.args[0].value
    return None


def _subject(node, remainders):
    """(prefix, stripped) when node is the request path, or the rest of it after a prefix, else None. stripped is true for
    path.lstrip('/'), whose answers have lost their leading slash."""
    if _is_path(node):
        return "", False
    if isinstance(node, ast.Name) and node.id in remainders:
        return remainders[node.id], False
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("lstrip", "strip")
            and _is_path(node.func.value) and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "/"):
        return "", True
    return None


def _strings(node, named):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
    if isinstance(node, ast.Name):
        return list(named.get(node.id, ()))
    return []


def _scan_handler(function, method, named, found, strings):
    remainders, in_test = {}, set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            prefix = _remainder_of(node.value)
            if prefix:
                remainders[node.targets[0].id] = prefix
        if isinstance(node, ast.If):
            in_test.update(id(part) for part in ast.walk(node.test))
    for node in ast.walk(function):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/api/"):
            strings.add(node.value)
        if isinstance(node, ast.Compare):
            subject = _subject(node.left, remainders)
            if subject is None:
                continue
            prefix, stripped = subject
            for operator, comparator in zip(node.ops, node.comparators):
                if isinstance(operator, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
                    for value in _strings(comparator, named):
                        path = prefix + ("/" if stripped else "") + value
                        found.append(Found(method, "exact", path, ((path, ()),)))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            argument = node.args[0]
            if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
                continue
            if node.func.attr in ("fullmatch", "match") and len(node.args) >= 2:
                subject = _subject(node.args[1], remainders)
                if subject is not None:
                    prefix = subject[0]
                    routes = tuple((prefix + shape, fragments) for shape, fragments in expand(argument.value))
                    found.append(Found(method, "pattern", prefix + argument.value, routes,
                                       re.escape(prefix) + argument.value))
            elif node.func.attr == "startswith" and id(node) in in_test:
                subject = _subject(node.func.value, remainders)
                if subject is not None:
                    text = subject[0] + argument.value + "*"
                    found.append(Found(method, "prefix", text, ((text, ()),)))


def scan(source):
    """The routes do_GET and do_POST in this source answer, and every /api/ string they hold."""
    tree = ast.parse(source)
    named = _module_strings(tree)
    found, strings = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in HANDLERS:
            _scan_handler(node, node.name[3:], named, found, strings)
    return Scan(tuple(found), frozenset(strings))
