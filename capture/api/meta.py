"""Operational endpoints: the load balancer's health probe and the recorder's config.

Neither is authenticated — /healthz is what decides whether this box takes
traffic, and /config is fetched by the visitor's browser before anything else.
"""

import json

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .. import analysis_port as ap
from .. import config, paths, projects, workos
from ..fsutil import atomic_write_text
from ..projects import ProjectsUnavailable

router = APIRouter()

# Reported but not health-gating: analysis is an optional extra, and a box with
# no model key or no browser is still a perfectly healthy recorder.
INFORMATIONAL = (
    "replay_auth_enabled",
    "read_only",
    "analysis_configured",
    "analysis_runtime_present",
)


@router.get("/healthz")
def healthz():
    checks = {}

    try:
        probe = paths.DATA_ROOT / ".healthz"
        atomic_write_text(probe, "ok")
        probe.unlink()
        checks["storage_writable"] = True
    except Exception:
        checks["storage_writable"] = False

    try:
        import shutil

        free = shutil.disk_usage(paths.DATA_ROOT).free
        checks["disk_above_floor"] = (
            not config.MIN_FREE_DISK_BYTES or free >= config.MIN_FREE_DISK_BYTES
        )
    except Exception:
        checks["disk_above_floor"] = False

    try:
        # Called for the exception, not the value: a registry that will not answer
        # must show as unhealthy rather than let this box keep taking traffic.
        projects.all_projects()
        checks["projects_readable"] = True
    except ProjectsUnavailable:
        checks["projects_readable"] = False

    checks["bundles_present"] = all(
        (paths.BASE_DIR / "public" / name).is_file()
        for name in ("capture.js", "recorder.bundle.js", "replay.bundle.js")
    )
    checks["replay_auth_enabled"] = (
        bool(config.REPLAY_PASSWORD or workos.enabled()) and not config.REPLAY_AUTH_DISABLED
    )
    checks["read_only"] = config.READ_ONLY
    checks["analysis_configured"] = ap.enabled()
    checks["analysis_runtime_present"] = ap.runtime_present()

    healthy = all(v for k, v in checks.items() if k not in INFORMATIONAL)
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status_code=200 if healthy else 503,
    )


@router.get("/config")
def remote_config(token: str | None = Query(None)):
    """Recording settings for one project, falling back to the shared file.

    Always no-cache: this is how a masking or sampling change reaches live
    visitors without waiting for a bundle to expire.
    """
    project = projects.by_token(token) if token else None
    if project is not None and project.config:
        response = JSONResponse(project.config)
        response.headers["Cache-Control"] = "no-cache"
        return response

    config_path = paths.CONFIG_PATH
    if project is not None and project.storage_prefix:
        project_config = paths.PROJECTS_DIR / project.storage_prefix / "config.json"
        if project_config.is_file():
            config_path = project_config
    try:
        cfg = json.loads(config_path.read_text())
        if not isinstance(cfg, dict):
            raise ValueError("config.json must be a JSON object")
    except FileNotFoundError:
        cfg = {}
    except Exception as exc:
        print(f"config: serving defaults, {config_path.name} unusable: {exc}")
        cfg = {}
    response = JSONResponse(cfg)
    response.headers["Cache-Control"] = "no-cache"
    return response
