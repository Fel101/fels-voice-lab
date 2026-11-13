"""The service's routes, in groups. Each module's register(router, lab) gives the router (routes.py) a function for each of
its rows of api_spec.ROUTES, and serve.make_handler registers every group in GROUPS for each service it builds.

A route function is called with the request handler, the parsed URL, the JSON body of a POST (None for a GET and for a
recording's upload) and the path's parameters by name. It answers through the handler (serve.Handler: _send, _json,
_stream_line and the body readers), and finds what the routes share in the Lab (serve.Lab): the model pool, the stores,
the background task and the service log. serve.py's own names, such as LAB and OUT, it reads as serve.NAME when it runs,
so that a test which points serve.LAB elsewhere moves the routes too."""

from route_groups import drama, evaluation, pages, system

GROUPS = (pages, evaluation, system, drama)
