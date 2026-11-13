"""Which function answers a request: the rows of api_spec.ROUTES, each matched to the handler registered for it.

The route groups (route_groups/) register one function per route with @router.get(path) or @router.post(path), naming the path
exactly as api_spec.py writes it, {take_id} and all; one function may answer several rows. A request path is matched against the patterns
api_spec.PATH_PARAMS gives each parameter: a path with no parameter first, then the patterns in the table's order, then the
prefixes (a path that ends in /*). The handler receives the parameters by name.

The table is the single list of what the service answers. register() refuses a handler for a route the table does not have,
and check() refuses a table with a route nobody answers, so serve.py cannot start with one documented and not answered, or the
other way round. Standard library only, like api_spec.py.
"""

import re

import api_spec


class RouteError(LookupError):
    """The routes and the handlers disagree: a handler for a route with no row, a row with no handler, or a row twice."""


def compile_path(path):
    """A regular expression for a route's path, with a named group for each {parameter}."""
    pattern, position = "", 0
    for match in re.finditer(r"\{([a-z_]+)\}", path):
        pattern += re.escape(path[position : match.start()])
        pattern += f"(?P<{match.group(1)}>{api_spec.PATH_PARAMS[match.group(1)][1]})"
        position = match.end()
    return re.compile(pattern + re.escape(path[position:]))


class Router:
    def __init__(self, routes=None):
        self.routes = {}
        for route in api_spec.ROUTES if routes is None else routes:
            key = (route.method, route.path)
            if key in self.routes:
                raise RouteError(f"{route.method} {route.path} has two rows in the route table.")
            self.routes[key] = route
        self.handlers = {}
        self._exact = {}
        self._patterns = []
        self._prefixes = []
        for key, route in self.routes.items():
            if route.path.endswith("/*"):
                self._prefixes.append((key, route.path[:-1]))
            elif "{" in route.path:
                self._patterns.append((key, compile_path(route.path)))
            else:
                self._exact[key] = route
        self._prefixes.sort(key=lambda item: len(item[1]), reverse=True)

    def register(self, method, *paths):
        """A decorator: the function answers these rows of the table."""

        def add(function):
            for path in paths:
                key = (method, path)
                if key not in self.routes:
                    raise RouteError(
                        f"{method} {path} has no row in api_spec.ROUTES. Add one, documented or with "
                        "internal=True, then run tools/build_api_docs.py."
                    )
                if key in self.handlers:
                    raise RouteError(f"{method} {path} has two handlers.")
                self.handlers[key] = function
            return function

        return add

    def get(self, *paths):
        return self.register("GET", *paths)

    def post(self, *paths):
        return self.register("POST", *paths)

    def check(self):
        """Raise RouteError unless every row of the table has a handler."""
        missing = sorted(f"{method} {path}" for method, path in self.routes if (method, path) not in self.handlers)
        if missing:
            raise RouteError("These rows of api_spec.ROUTES have no handler in serve.py: " + ", ".join(missing))

    def match(self, method, path):
        """(route, handler, parameters) for a request, or None when no row answers it."""
        key = (method, path)
        if key in self._exact:
            return self._exact[key], self.handlers.get(key), {}
        for key, pattern in self._patterns:
            if key[0] == method:
                found = pattern.fullmatch(path)
                if found:
                    return self.routes[key], self.handlers.get(key), found.groupdict()
        for key, prefix in self._prefixes:
            if key[0] == method and path.startswith(prefix):
                return self.routes[key], self.handlers.get(key), {"rest": path[len(prefix) :]}
        return None
