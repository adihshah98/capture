# Setting up RDS for Capture

One-time infrastructure setup, then the cutover. Do staging first.

The cutover is ordered so the old build keeps serving while the database is
populated — the old code never touches Postgres, so migrating and backfilling
underneath it is safe and takes the downtime to roughly one restart.

---

## 1. Note where the EC2 box lives

**EC2 → Instances →** find the instance (prod is `35.167.249.40`, staging is
`34.214.222.44`). From the **Details** and **Security** tabs write down:

- VPC ID (`vpc-…`)
- Availability Zone (e.g. `us-west-2a`)
- Security group ID (`sg-…`)

The database must land in the same VPC, and the same AZ to avoid cross-AZ
latency and transfer charges.

## 2. Create the database

**RDS → Create database.**

| Field | Value |
|---|---|
| Creation method | Standard create |
| Engine | PostgreSQL |
| Version | Latest 16.x |
| Template | Dev/Test |
| Availability | **Single-AZ DB instance** |
| DB instance identifier | `capture-prod` |
| Master username | `capture_admin` |
| Credentials management | Self managed — set your own password, save it in a password manager |
| Instance class | Burstable → **db.t4g.micro** |
| Storage type | gp3 |
| Allocated storage | 20 GiB |
| Storage autoscaling | Enabled, max 100 GiB |
| Compute resource | **Connect to an EC2 compute resource** → pick the capture instance |
| Public access | **No** |
| Availability Zone | Same as the EC2 box |
| Database authentication | Password authentication |

Choosing *Connect to an EC2 compute resource* makes the console create the
security groups for you: inbound 5432 on the database restricted to the
instance's security group. That is exactly the rule you want, and it fills in
the VPC and subnet group to match.

Under **Additional configuration**:

| Field | Value |
|---|---|
| Initial database name | `capture` — **easy to miss, and nothing is created without it** |
| Backup retention | 7 days |
| Encryption | Enabled |
| Deletion protection | **Enabled** |
| Minor version auto upgrade | Enabled |
| Maintenance window | A low-traffic hour |

Create it, wait for status **Available** (5–10 minutes), then copy the
**Endpoint** from the Connectivity & security tab.

### Staging

Don't create a second instance. Add a `capture_staging` database on this one
(step 3 creates it) and add the staging box's security group to the database's
inbound rule under **EC2 → Security Groups → the RDS group → Edit inbound
rules**. Saves the second instance's monthly cost.

## 3. Create the application role

RDS is private, so run this from the EC2 box.

```bash
ssh -i ~/Downloads/capture-key.pem ec2-user@<host>
sudo dnf install -y postgresql16
psql "postgresql://capture_admin:<ADMIN_PASSWORD>@<ENDPOINT>:5432/capture?sslmode=require"
```

The app must connect as a role that is **not** a superuser — row level security
does not bind a superuser, and tenant isolation would silently stop working.

```sql
CREATE ROLE capture_app LOGIN PASSWORD '<APP_PASSWORD>' NOSUPERUSER NOBYPASSRLS;
GRANT CONNECT ON DATABASE capture TO capture_app;
GRANT USAGE ON SCHEMA public TO capture_app;

-- staging, if you are sharing the instance
CREATE DATABASE capture_staging;
```

Leave the table grants until after migrations — the tables and the `compat`
schema do not exist yet.

## 4. Point the box at it

```bash
sudo tee -a /etc/capture.env >/dev/null <<'ENV'
CAPTURE_DATABASE_URL=postgresql://capture_app:<APP_PASSWORD>@<ENDPOINT>:5432/capture?sslmode=require
CAPTURE_DATABASE_ADMIN_URL=postgresql://capture_admin:<ADMIN_PASSWORD>@<ENDPOINT>:5432/capture?sslmode=require
ENV
sudo chmod 600 /etc/capture.env
```

Remove any placeholder `CAPTURE_DATABASE_URL=` line `setup.sh` wrote, so the
real one is not shadowed by an empty value.

`CAPTURE_DB_APP_ROLE` is not needed on PostgreSQL 16 — it only exists for
PostgreSQL 14, which lacks `security_invoker` views.

## Production shape (measured 2026-08-10)

- **24 GB** of recordings, **31,810 sessions**, 64 GB volume with 36 GB free
- Two projects, both org-scoped: `Shopify Test` (5 sessions) and `Root Labs` (31,805)
- `data/sessions` is empty — there is no legacy pool, so **no default project is
  created** and a tokenless ingest correctly gets a 401
- `data/index.db` is 38 MB and holds 31,808 of the 31,810 sessions

Two consequences, both already handled in the code:

- `deploy.sh` **renames** `data/` to `/var/lib/capture` rather than copying it.
  A 24 GB copy would hold the service down for minutes and briefly need twice
  the disk. Same filesystem, so the rename is instant.
- `backfill.py` reads `index.db` directly instead of re-deriving from chunk
  files. Re-parsing 24 GB of JSON would take the better part of an hour; the
  index already has every column the new schema needs. Only sessions missing
  from the index fall back to parsing.

## 5. Migrate and backfill, with the old build still serving

From your laptop:

```bash
git pull
scp -i ~/Downloads/capture-key.pem db.py backfill.py projects.py sessionstore.py \
  compat.py fsutil.py ec2-user@<host>:/opt/capture/
scp -i ~/Downloads/capture-key.pem -r migrations ec2-user@<host>:/opt/capture/
```

On the box:

```bash
cd /opt/capture
.venv/bin/pip install -q 'psycopg[binary]==3.2.3' 'psycopg-pool==3.2.4'
set -a; . /etc/capture.env; set +a
.venv/bin/python db.py        # applies migrations
.venv/bin/python -m capture.backfill  # imports existing sessions
```

The running server is still on the old build and does not know Postgres exists,
so this is safe under live traffic. Backfill only reads the session directories.

Then grant the app role its tables, now that they exist:

```sql
GRANT USAGE ON SCHEMA public, compat TO capture_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public, compat TO capture_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO capture_app;
```

Check the import before cutting over:

```sql
SELECT name, is_default, storage_prefix FROM projects;
SELECT count(*) FROM sessions;
```

Every existing project token must appear in `project_tokens`, and the session
count should match what the replay UI shows today.

## 6. Deploy

```bash
./scripts/deploy-staging.sh   # soak here first
./scripts/deploy.sh
```

`deploy.sh` re-runs migrations (a no-op now), then restarts. It refuses to
deploy if `CAPTURE_DATABASE_URL` is missing from `/etc/capture.env`.

Immediately after, run the backfill once more to pick up any session recorded
between the first backfill and the restart:

```bash
cd /opt/capture && set -a && . /etc/capture.env && set +a && .venv/bin/python -m capture.backfill
```

It is idempotent, so re-running costs nothing.

## 7. Verify

```bash
curl -s https://d1lt8dgxjrizij.cloudfront.net/healthz
```

`projects_readable` must be `true` and status `ok`. Then in the replay UI:
the session list loads, the checkout/purchased/rage filters return sensible
counts, search works, pagination advances, and a session replays.

## Rolling back

The old build still works: `data/index.db` is untouched and chunks are still
written to the same directories. To roll back, `cp -a .rollback/. /opt/capture/`,
restart, then run the **old** `server.py reindex` to rebuild the SQLite index
from disk.

Sessions recorded while Postgres was live will come back with clicks, rage and
device missing, because those sidecar files are no longer written. The
recordings themselves are intact.

**Do not delete `data/index.db`** until you have run on Postgres long enough to
be confident. It is the rollback path.
