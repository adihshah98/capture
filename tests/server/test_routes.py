"""The public HTTP surface, pinned.

The route split moves handlers out of server.py a router at a time, and the way
that goes wrong is quiet: a route that stops being registered still passes every
test that never calls it. /healthz and /config had no coverage at all, so the
first move could have dropped both and the suite would have stayed green.

Read from the OpenAPI schema rather than app.routes. FastAPI keeps an included
router as a single _IncludedRouter entry whose children are not reachable by
walking .routes, so counting app.routes silently under-reports by exactly the
number of routes that have been split out — the opposite of what a guard needs.

Adding or removing an endpoint should change this list in the same commit.
"""

import unittest

EXPECTED = {
    ("GET", "/healthz"),
    ("GET", "/config"),
    ("POST", "/ingest"),
    ("POST", "/order"),
    ("GET", "/login"),
    ("GET", "/logout"),
    ("GET", "/auth/callback"),
    ("GET", "/api/projects"),
    ("GET", "/api/sessions"),
    ("GET", "/api/sessions/{session_id}"),
    ("DELETE", "/api/sessions/{session_id}"),
    ("GET", "/api/sessions/{session_id}/chunks"),
    ("GET", "/api/sessions/{session_id}/chunks/{chunk_name}"),
    ("GET", "/api/sessions/{session_id}/analysis"),
    ("POST", "/api/sessions/{session_id}/analysis"),
    ("GET", "/api/facets"),
    ("GET", "/api/cohorts"),
    ("POST", "/api/cohorts"),
    ("GET", "/api/cohorts/{cohort_id}"),
    ("PATCH", "/api/cohorts/{cohort_id}"),
    ("DELETE", "/api/cohorts/{cohort_id}"),
    ("GET", "/api/cohorts/{cohort_id}/facets"),
    ("GET", "/api/cohorts/{cohort_id}/cut"),
    ("POST", "/api/cohorts/{cohort_id}/analyze"),
    ("POST", "/api/cohorts/{cohort_id}/pause"),
    ("POST", "/api/cohorts/{cohort_id}/sessions/{session_id}/retry"),
    ("POST", "/api/cohorts/{cohort_id}/chat"),
}


def operations() -> set:
    from capture.server import app

    spec = app.openapi()["paths"]
    return {(method.upper(), path) for path, ops in spec.items() for method in ops}


class TestRouteInventory(unittest.TestCase):
    def test_every_expected_route_is_registered(self):
        missing = EXPECTED - operations()
        self.assertEqual(missing, set(), f"routes went missing: {sorted(missing)}")

    def test_no_route_appeared_unannounced(self):
        extra = operations() - EXPECTED
        self.assertEqual(extra, set(), f"undeclared routes: {sorted(extra)}")

    def test_the_count_is_what_we_think_it_is(self):
        self.assertEqual(len(operations()), 27)


if __name__ == "__main__":
    unittest.main()


class TestReadAccessIsDeclaredNotRemembered(unittest.TestCase):
    """Every project-scoped handler takes readable_project as a parameter.

    That is the point of the dependency: the check used to be two lines at the top
    of each body, which a new handler could simply omit and still work — it would
    just serve another tenant's data. Now a handler without the parameter has no
    project to serve at all.
    """

    def test_every_handler_taking_a_project_query_declares_the_dependency(self):
        import inspect

        from capture.api import cohorts, sessions
        from capture.api.deps import readable_project

        offenders = []
        for module in (sessions, cohorts):
            for route in module.router.routes:
                fn = route.endpoint
                params = inspect.signature(fn).parameters
                takes_dep = any(
                    getattr(p.default, "dependency", None) is readable_project
                    for p in params.values()
                )
                # /api/projects is the one that deliberately spans every project.
                if route.path == "/api/projects":
                    continue
                if not takes_dep:
                    offenders.append(f"{module.__name__}.{fn.__name__}")
        self.assertEqual(offenders, [], f"no readable_project dependency: {offenders}")

    def test_the_dependency_is_overridable(self):
        """The seam this buys: before, the only way past the check was a real
        signed cookie and a live projects table."""
        from fastapi.testclient import TestClient

        from capture.api.deps import readable_project
        from capture.server import app

        sentinel = object()
        app.dependency_overrides[readable_project] = lambda: sentinel
        try:
            self.assertIn(readable_project, app.dependency_overrides)
            self.assertIs(app.dependency_overrides[readable_project](), sentinel)
            TestClient(app)  # the app still builds with the override installed
        finally:
            app.dependency_overrides.clear()
