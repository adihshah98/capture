"""Project administration from the command line.

Run as `python -m capture.cli <command>`.

This used to live at the bottom of server.py behind hand-rolled argv index checks,
which meant `add-project` imported FastAPI, registered 27 routes and built every
middleware in order to insert one row — and a typo printed the usage block as
though nothing were wrong. Nothing here imports the web app.
"""

import argparse
import random
import shutil
import string
import sys
import urllib.error
import urllib.parse

from . import db, projects, workos
from .projects import ProjectsUnavailable
from .snippet import PLACEHOLDER_HOST, integration_snippet, public_base_url


def find_project(handle: str):
    """Accepts the project handle or any of its ingest tokens."""
    return projects.by_handle(handle) or projects.by_token(handle)


def _require(handle: str):
    project = find_project(handle)
    if project is None:
        raise SystemExit(f"no such project: {handle}")
    return project


def add_project(name: str) -> None:
    token = "cap_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=24))
    project = projects.create(name, token)
    project.pool.mkdir(parents=True, exist_ok=True)
    base = public_base_url()
    print(f"project '{name}' created — token: {token}")
    print("\nAdd this snippet to the brand's site, on every page:\n")
    print(integration_snippet(base, token))
    if base == PLACEHOLDER_HOST:
        print(
            "\n  (placeholder host — set CAPTURE_PUBLIC_URL or run ./setup.sh "
            "to record your real one)"
        )
    print(
        "\nOptional per-project recording config: "
        f"data/projects/{token}/config.json (falls back to data/config.json)"
    )


def list_projects() -> None:
    known = projects.all_projects(fresh=True)
    if not known:
        print("no projects configured — run add-project, or reindex to import existing data")
        return
    for project in known:
        count, used = projects.usage(project)
        org = project.org_id or "no org — operators only"
        tokens = ", ".join(project.tokens) or "no live token"
        label = project.handle or "(default)"
        print(
            f"{label}  {project.name}  —  {count} session(s), {used / 1024**2:.1f} MB"
            f"  —  {org}  —  {tokens}"
        )


def set_org(handle: str, org_id: str) -> None:
    project = _require(handle)
    projects.set_org(project, org_id)
    print(f"'{project.name}' is now readable by members of WorkOS organization {org_id}")


def set_domains(handle: str, domains: list) -> None:
    project = _require(handle)
    cleaned = [
        host.lower()
        for d in domains
        if (host := urllib.parse.urlparse(d if "://" in d else f"//{d}").hostname)
    ]
    projects.set_domains(project, cleaned)
    if cleaned:
        print(
            f"'{project.name}' now accepts recordings only from "
            f"{', '.join(cleaned)} (and subdomains)"
        )
    else:
        print(f"'{project.name}' now accepts recordings from any domain")


def invite(email: str, target: str) -> None:
    if not workos.enabled():
        raise SystemExit("invites need WORKOS_API_KEY and WORKOS_CLIENT_ID in the environment")
    project = find_project(target)
    org_id = project.org_id if project else target
    if not (org_id or "").startswith("org_"):
        raise SystemExit(f"'{target}' has no WorkOS org — pass an org_… id or run set-org first")
    try:
        invitation = workos.call(
            "/user_management/invitations",
            {"email": email, "organization_id": org_id},
            bearer=True,
        )
    except urllib.error.HTTPError as err:
        raise SystemExit(f"invite failed: {err.read().decode(errors='replace')}") from err
    expires = invitation.get("expires_at") or "?"
    print(f"invited {email} to {org_id} — they'll get an email link, valid until {expires}")


def remove_project(handle: str, purge: bool) -> None:
    project = _require(handle)
    count, used = projects.usage(project)
    for token in project.tokens:
        projects.revoke_token(token)
    print(f"revoked '{project.name}' — batches sent with its tokens are now rejected")

    if purge:
        shutil.rmtree(project.pool.parent, ignore_errors=True)
        projects.delete(project)
        print(f"deleted {count} stored session(s), {used / 1024**2:.1f} MB")
    elif count:
        print(f"kept {count} session(s), {used / 1024**2:.1f} MB — re-run with --purge to erase")


def reindex() -> None:
    from . import backfill

    db.migrate()
    result = backfill.backfill()
    print(
        f"{result['sessions']} session(s) across {result['projects']} project(s);"
        f" {result['skipped']} empty dir(s) skipped"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m capture.cli",
        description="Administer capture projects. Run the server with: uvicorn capture.server:app",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-project", help="create a project and print its snippet")
    add.add_argument("name", help="display name, e.g. 'Acme'")

    sub.add_parser("list-projects", help="every project with its usage and tokens")

    remove = sub.add_parser("remove-project", help="revoke a project's tokens")
    remove.add_argument("handle", help="project handle or ingest token")
    remove.add_argument(
        "--purge", action="store_true", help="also delete its recordings — irreversible"
    )

    org = sub.add_parser("set-org", help="grant a WorkOS org read access")
    org.add_argument("handle")
    org.add_argument("org_id", help="a WorkOS id, org_…")

    domains = sub.add_parser("set-domains", help="restrict which sites may record")
    domains.add_argument("handle")
    domains.add_argument("domains", nargs="*", help="bare hosts; none means any domain")

    inv = sub.add_parser("invite", help="invite a user to a project's org")
    inv.add_argument("email")
    inv.add_argument("target", help="project handle, token, or an org_… id")

    sub.add_parser("reindex", help="migrate, then import on-disk sessions into Postgres")
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "add-project":
            add_project(args.name)
        elif args.command == "list-projects":
            list_projects()
        elif args.command == "remove-project":
            remove_project(args.handle, args.purge)
        elif args.command == "set-org":
            set_org(args.handle, args.org_id)
        elif args.command == "set-domains":
            set_domains(args.handle, args.domains)
        elif args.command == "invite":
            invite(args.email, args.target)
        elif args.command == "reindex":
            reindex()
    except ProjectsUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "the projects table is unreadable — refusing to guess at tenant routing",
            file=sys.stderr,
        )
        return 1
    finally:
        # Without this the pool's worker threads outlive the command and print
        # shutdown warnings over its output.
        db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
