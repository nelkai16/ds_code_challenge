# SUBMISSION — how to run this

Two scripts, one per section of the brief. Nothing is pre-downloaded and no AWS account or AWS CLI is
needed: the public credentials object is fetched by the script itself.

| file | what it is |
|---|---|
| `1_S3_Data_Extract.py` | Section 1 — extraction from S3 and validation against the contract |
| `2_Postgres_Join.py` | Section 2 — the join, run in a throwaway PostGIS container |
| `schema.yml` | the conformance contract section 1 is scored against |
| `tests/` | unit tests (no container) and integration tests (start their own) |
| `AI_log.md` | record of AI use |

## Prerequisites

- **Python 3.10 or newer** (developed and tested on 3.11 and 3.14).
- **A container runtime**: Docker (Engine or Desktop) or Podman. If both are installed `docker` is
  preferred; override with `CCT_RUNTIME`.
- **At least 2 GB of memory for the container runtime.** The run peaks at about 1.1 GB inside the
  database container; 1 GB fails, 2 GB is comfortable. On macOS and Windows this is the RAM assigned
  to the VM (Docker Desktop, colima, `podman machine`), not the host's.
- **About 0.6 GB of free disk** for the PostGIS image, which is pulled on the first run, pinned by
  digest.
- **Outbound HTTPS** to `cct-ds-code-challenge-input-data.s3.af-south-1.amazonaws.com` and to Docker Hub.
- **Host port 55432 free.** The database is published on `127.0.0.1` only; set `CCT_PORT` to move it.

## Run — Linux and macOS

```
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python 1_S3_Data_Extract.py                        # section 1
.venv/bin/python 2_Postgres_Join.py                          # section 2
.venv/bin/python -m pytest -q tests/                         # 12 passed, 6 skipped
CCT_TEST_CONTAINER=1 .venv/bin/python -m pytest -q tests/    # 18 passed (starts its own container)
```

## Run — Windows (PowerShell)

```
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python 1_S3_Data_Extract.py
.venv\Scripts\python 2_Postgres_Join.py
.venv\Scripts\python -m pytest -q tests/
$env:CCT_TEST_CONTAINER='1'; .venv\Scripts\python -m pytest -q tests/
```

## What a successful run looks like

Section 1 exits 0 and its log ends with:

```
records evaluated 3832, passing 3832, failing 0
score 100.0 against threshold <schema.yml> -> PASS (report: validation_report.json)
identical to validation_results.json: True | exit 0
```

Section 2 exits 0 in about 40 seconds (the first run additionally pulls the image) and its log ends with:

```
counts: {'hexagons': 3832, 'service_requests': 941634, 'oracle_rows': 941634,
         'with_coordinates': 729270, 'without_coordinates': 212364,
         'failed_joins': 3, 'oracle_disagreements': 29}
container cct-section2-pg removed
```

It writes `joined_service_requests.csv` — 24,334,277 bytes, 941,635 lines including the header —
beside the script. Both scripts can be run from any directory, by relative or absolute path; their
outputs always land next to them.

## Environment variables

`2_Postgres_Join.py`:

| variable | default | meaning |
|---|---|---|
| `CCT_RUNTIME` | `docker` if installed, otherwise `podman` | container runtime to use |
| `CCT_KEEP_CONTAINER` | `0` | `1` keeps the database running after the script exits, for inspection |
| `CCT_PORT` | `55432` | host port PostgreSQL is published on |
| `CCT_PASSWORD` | `Yeb026!` | password for the `postgres` user |
| `CCT_FAILURE_THRESHOLD` | `0.001` | share of located records allowed to fail the join before the run stops |
| `CCT_TEST_CONTAINER` | unset | `1` lets the integration tests start their own container |

`1_S3_Data_Extract.py` takes no environment variables.

## Notes

- **The database does not persist.** It is created when a run starts and removed when it ends, its data
  directory lives on tmpfs, and no container or volume is left behind. `CCT_KEEP_CONTAINER=1` keeps it
  alive for interactive work, and the log then prints the connection command:
  `PGPASSWORD=Yeb026! psql -h 127.0.0.1 -p 55432 -U postgres -d cct`. This is public data on a loopback
  port, so the password is a fixed literal rather than a secret.
- **The export is deterministic.** The join is ordered, so two runs produce a byte-identical file:
  `sha256 3a9b46ade0b2a1976377b39905eee484af71c30defbc39173d0e1368dad821a3`.
- **Apple Silicon.** The PostGIS image is published for amd64 only, so it runs under amd64 emulation on
  arm64 hosts (Docker Desktop: "Use Rosetta for x86_64/amd64 emulation"; colima:
  `--vm-type vz --vz-rosetta`). That path has not been tested on arm64 hardware. If emulation
  misbehaves, `docker.io/imresamu/postgis:16-3.5-alpine` is a native multi-arch build, but the counts
  above would need re-verifying, because its GEOS and PostGIS builds differ.
- Section 1 downloads the extract on every run, as the brief requires. `r8_hexagons.geojson` and
  `validation_report.json` are committed as reference artefacts; `validation.log` and
  `validation_results.json` are build products and are ignored.
