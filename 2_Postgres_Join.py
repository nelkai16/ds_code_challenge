"""Section 2 - join every service request to the H3 resolution 8 hexagon that contains it, in a
throwaway PostgreSQL/PostGIS container, validate the join against the provided oracle, and export it.

The container is one shot. It is created when this script starts and removed when it ends, and its
data directory lives on tmpfs, so no database survives the run and no volume is left behind. Set
CCT_KEEP_CONTAINER=1 to keep it alive for interactive work instead of removing it.

What it does:
  1. S3 SELECT the resolution 8 hexagons (index and geometry) out of city-hex-polygons-8-10.geojson.
  2. Start PostGIS, with the data directory on tmpfs.
  3. Create the schema: hexagon, service_request, sr_hex_oracle.
  4. Stream both CSVs straight from S3 into their tables with COPY. No temporary files, no row by row.
  5. Build and index a point geometry for every service request.
  6. Count the join failures, stop if they are above the threshold, and compare with the oracle.
  7. Export the join to joined_service_requests.csv and remove the container.

Environment variables:
  CCT_RUNTIME            container runtime to use (default: docker when installed, otherwise podman)
  CCT_KEEP_CONTAINER     1 keeps the container when the script exits, 0 removes it (default: remove)
  CCT_PORT               host port to publish PostgreSQL on (default: 55432)
  CCT_PASSWORD           password for the postgres user (default: the fixed literal below)
  CCT_FAILURE_THRESHOLD  share of records allowed to fail the join (default: 0.001)
"""

#The join runs in the database because that is where it belongs: one SQL statement against a table of
#hexagons and a table of service requests. Materialising both sides in Python would be slower to read
#and maintain, would put the assignment logic in application code, and is not how the query would run
#in production, where the data already lives in a database and this is a once-off query against it.
#The container is demonstration scaffolding around that query: it exists so the query can be run and
#validated end to end on any machine, and it is removed when the run ends. Its start up and the bulk
#loads are therefore reported separately from the query itself.
#Loading is done with COPY over a stream downloaded from S3: no staging files, no pandas frames, and
#the geometry work happens in PostGIS rather than in Python.

import csv
import gzip
import io
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Iterable
from urllib.parse import quote

import boto3
import psycopg
import requests
import urllib3  # already installed with requests and boto3; named here for its stream exceptions
from psycopg import sql

# --- dataset (the public challenge bucket; the credentials object holds no privileges) ---

bucket_name = "cct-ds-code-challenge-input-data"
keys = "ds_code_challenge_creds.json"
url = "https://cct-ds-code-challenge-input-data.s3.af-south-1.amazonaws.com/"
region = "af-south-1"

resFile = "city-hex-polygons-8-10.geojson"
srFile = "sr.csv.gz"
srHexFile = "sr_hex.csv.gz"

# The join needs the polygon as well as the index, so geometry is part of the projection.
hexQuery = "SELECT s.properties.index, s.geometry FROM S3Object[*].features[*] s where s.properties.resolution = 8"

# --- container ---

# Pinned by the digest of the tag's index (not of the local amd64 image), so every machine pulls
# exactly the image the recorded counts came from. postgis/postgis publishes amd64 only, for every
# tag, so the platform is requested explicitly: an arm64 host (Apple Silicon) then runs it under
# emulation instead of failing to find a matching manifest. On amd64 the flag changes nothing.
image = (
    "docker.io/postgis/postgis:16-3.5-alpine"
    "@sha256:47e961a569fd52ff31f0fe205ed91eeab17d9f5fff6722e6d7ea6b588748b293"
)
platform = "linux/amd64"
container = "cct-section2-pg"
dbName = "cct"
dbUser = "postgres"
# The database holds public data on a loopback port and is thrown away with the container, so the
# password is a fixed literal rather than a per-run secret: a reviewer can reproduce a run, connect to
# it by hand, and read the same command. Override it with CCT_PASSWORD.
DB_PASSWORD = "Yeb026!"
hostPort = int(os.environ.get("CCT_PORT", "55432"))
KEEP_CONTAINER = os.environ.get("CCT_KEEP_CONTAINER", "0") != "0"
DATA_DIR_SIZE = "3g"  # tmpfs holding the whole database; nothing survives the container

# --- outputs ---

# Anchored to the script rather than the working directory, so a reviewer who runs this from
# anywhere still gets the log and the export next to the code.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOG = os.path.join(BASE_DIR, "join.log")
JOINED_CSV = os.path.join(BASE_DIR, "joined_service_requests.csv")  # a build product, so it is ignored in git

logger = logging.getLogger("join")

# Populated by main() so the run's counts and timings are in one place for the log and the report.
RUN: dict[str, Any] = {"timings": {}, "counts": {}}

# sr.csv.gz is a pandas export: the first field of every row is an unnamed index column, so the
# source columns are one position to the right of where their names suggest.
SR_COLUMNS = (
    "notification_number",
    "reference_number",
    "creation_timestamp",
    "completion_timestamp",
    "directorate",
    "department",
    "branch",
    "section",
    "code_group",
    "code",
    "cause_code_group",
    "cause_code",
    "official_suburb",
    "latitude",
    "longitude",
)
ORACLE_COLUMNS = ("notification_number", "h3_level8_index")


def startLogging() -> None:
    """Console at INFO, join.log at DEBUG, appended so runs accumulate."""
    logger.setLevel(logging.DEBUG)

    # %Z so the log carries the machine's timezone: the timings are meaningless to a reviewer on
    # another machine without it.
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S %Z")

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    fileHandler = logging.FileHandler(LOG, mode="a", encoding="utf-8")
    fileHandler.setLevel(logging.DEBUG)
    fileHandler.setFormatter(formatter)

    logger.handlers.clear()
    logger.addHandler(console)
    logger.addHandler(fileHandler)

    logger.info("-" * 72)
    logger.info("run start")


# --- container runtime ---


def containerRuntime() -> str:
    """docker when it is installed, otherwise podman. Overridable with CCT_RUNTIME.

    Both accept the same arguments here, and the script only uses run, exec, inspect, logs, rm and
    volume rm, which behave the same in either.
    """
    override = os.environ.get("CCT_RUNTIME")
    if override:
        return override

    for candidate in ("docker", "podman"):
        if shutil.which(candidate):
            return candidate

    raise RuntimeError(
        "neither docker nor podman is on PATH. This script needs a container runtime; install one, "
        "or point CCT_RUNTIME at the binary you have."
    )


def runRuntime(arguments: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run one container runtime command, keeping its output out of the console.

    The output is decoded as UTF-8 whatever the host's locale is: the runtime writes UTF-8, and a
    cp1252 console on Windows would otherwise fail on the first byte it cannot map.
    """
    runtime = containerRuntime()
    result = subprocess.run(
        [runtime, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{runtime} {' '.join(arguments)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result


def removeContainer() -> bool:
    """Remove the container, and any volume the image attached to its data directory.

    The postgis image declares VOLUME /var/lib/postgresql/data. On tmpfs that declaration is
    overridden and nothing is left behind, but if the fallback (no tmpfs) is ever used, the anonymous
    volume would outlive the container and break the non-persistence guarantee, so it is removed
    explicitly rather than assumed away.

    Returns whether the container is gone afterwards, so "removed" is logged as a check, not a claim.
    """
    mounts = runRuntime(
        ["inspect", container, "--format", "{{range .Mounts}}{{.Type}}:{{.Name}}\n{{end}}"],
        check=False,
    )
    volumes = [
        entry.split(":", 1)[1]
        for entry in mounts.stdout.splitlines()
        if entry.startswith("volume:") and entry.split(":", 1)[1].strip()
    ]

    runRuntime(["rm", "--force", container], check=False)

    for volume in volumes:
        logger.debug("removing volume %s left by %s", volume, container)
        runRuntime(["volume", "rm", "--force", volume], check=False)

    # rm is unchecked because the container may never have existed; whether it is gone is checked here.
    return runRuntime(["inspect", "--type", "container", container], check=False).returncode != 0


def waitUntilReady(timeout: float = 180.0) -> None:
    """Wait for the server to accept TCP connections.

    The image's entrypoint runs initdb behind a temporary server that listens on the Unix socket
    only, and pg_isready answers there while the init scripts are still running. Probing over TCP
    waits for the real server instead, so nothing can race those scripts. That race is not
    theoretical: issuing CREATE EXTENSION during init made the image's own CREATE EXTENSION fail on a
    duplicate key and the container exited with status 3.
    """
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        state = runRuntime(["inspect", container, "--format", "{{.State.Status}}"], check=False)
        if state.stdout.strip() != "running":
            logTail = runRuntime(["logs", "--tail", "20", container], check=False).stdout
            raise RuntimeError(
                f"{container} is {state.stdout.strip() or 'gone'} instead of running:\n{logTail}"
            )

        probe = runRuntime(
            [
                "exec",
                container,
                "pg_isready",
                "--host",
                "127.0.0.1",
                "--port",
                "5432",
                "--username",
                dbUser,
                "--dbname",
                dbName,
            ],
            check=False,
        )
        if probe.returncode == 0:
            return

        time.sleep(0.5)

    raise TimeoutError(f"{container} did not accept TCP connections within {timeout:.0f}s")


def startContainer(password: str) -> str:
    """Start the container and return a description of where its data directory lives.

    tmpfs first, so the database cannot outlive the container. If the host refuses that (rootless
    podman without the right delegation, for instance), fall back to the anonymous volume the image
    declares for its data directory: on disk rather than in RAM, and removed by removeContainer().
    """
    removeContainer()

    base = [
        "run",
        "--detach",
        "--rm",
        "--platform",
        platform,
        "--name",
        container,
        "--env",
        f"POSTGRES_USER={dbUser}",
        "--env",
        f"POSTGRES_DB={dbName}",
        "--env",
        f"POSTGRES_PASSWORD={password}",
        "--publish",
        f"127.0.0.1:{hostPort}:5432",
    ]

    attempts = (
        ("tmpfs", ["--tmpfs", f"/var/lib/postgresql/data:rw,size={DATA_DIR_SIZE}"]),
        ("anonymous volume", []),
    )

    lastError = ""
    for where, extra in attempts:
        removeContainer()

        started = runRuntime(base + extra + [image], check=False)
        if started.returncode != 0:
            lastError = started.stderr.strip()
            logger.warning("could not start the container with %s: %s", where, lastError)
            continue

        try:
            waitUntilReady()
        except (TimeoutError, RuntimeError) as error:
            lastError = str(error)
            logger.warning("container not usable with %s: %s", where, error)
            continue

        logger.info(
            "container %s ready on 127.0.0.1:%d, data directory: %s",
            container,
            hostPort,
            where,
        )
        return where

    raise RuntimeError(
        f"could not start {container}: {lastError}\n"
        f"If port {hostPort} is already in use, set CCT_PORT to a free one. On an arm64 host the "
        f"{platform} image needs emulation enabled in the container runtime."
    )


def dsn(password: str) -> str:
    """Connection string for the published port. The password is URL-quoted, so the '!' in the
    default, or any character in CCT_PASSWORD, reaches the server unchanged."""
    return (
        f"postgresql://{dbUser}:{quote(password, safe='')}"
        f"@127.0.0.1:{hostPort}/{dbName}"
    )


def connect(password: str) -> psycopg.Connection:
    """Autocommit, so COPY and DDL are each their own statement and a retry cannot duplicate rows.

    sslmode is set here because the container has no TLS: a PGSSLMODE=require the reviewer exports
    for their own databases would otherwise refuse this loopback connection (verified).
    """
    return psycopg.connect(dsn(password), autocommit=True, sslmode="disable")


# --- input ---


def credentials() -> tuple[str, str]:
    """The public credentials object. Values are never logged and never written anywhere."""
    response = requests.get(url + keys, timeout=30)
    response.raise_for_status()
    shared = response.json()["s3"]
    return shared["access_key"], shared["secret_key"]


def selectJson(query: str) -> tuple[list[dict], dict]:
    """Run one S3 SELECT query and return the parsed records plus the scan statistics.

    S3 splits Records events at arbitrary byte offsets, so the payload is accumulated and only then
    split on newlines; parsing each event on its own truncates records mid JSON.
    """
    accessKey, secretKey = credentials()
    client = boto3.client(
        "s3",
        aws_access_key_id=accessKey,
        aws_secret_access_key=secretKey,
        region_name=region,
    )

    response = client.select_object_content(
        Bucket=bucket_name,
        Key=resFile,
        ExpressionType="SQL",
        Expression=query,
        InputSerialization={"JSON": {"Type": "DOCUMENT"}, "CompressionType": "NONE"},
        OutputSerialization={"JSON": {"RecordDelimiter": "\n"}},
    )

    payload = bytearray()
    stats: dict = {}
    ended = False
    for event in response["Payload"]:
        if "Records" in event:
            payload += event["Records"]["Payload"]
        elif "Stats" in event:
            stats = event["Stats"]["Details"]
        elif "End" in event:
            ended = True

    # A stream cut at a record boundary parses cleanly, so only the End event proves nothing is missing.
    if not ended:
        raise RuntimeError(f"S3 SELECT on {resFile} ended without an End event: the result is incomplete")

    lines = [line for line in payload.decode("utf-8").split("\n") if line.strip()]
    return [json.loads(line) for line in lines], stats


# --- schema ---


# UNLOGGED because the database is thrown away with the container, so crash safety buys nothing,
# and write-ahead logging the bulk loads nearly doubles the tmpfs the run needs (measured: 1.0 GB
# of WAL next to 0.7 GB of tables), which is memory a 2 GB Docker Desktop or podman machine VM
# does not have.
DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE UNLOGGED TABLE hexagon (
    h3_index text PRIMARY KEY,
    geom geometry(Polygon, 4326) NOT NULL
);

CREATE UNLOGGED TABLE service_request (
    notification_number text PRIMARY KEY,
    reference_number text,
    creation_timestamp timestamptz,
    completion_timestamp timestamptz,
    directorate text,
    department text,
    branch text,
    section text,
    code_group text,
    code text,
    cause_code_group text,
    cause_code text,
    official_suburb text,
    latitude double precision,
    longitude double precision,
    geom geometry(Point, 4326)
);

CREATE UNLOGGED TABLE sr_hex_oracle (
    notification_number text PRIMARY KEY,
    h3_level8_index text NOT NULL
);

CREATE INDEX hexagon_geom_idx ON hexagon USING GIST (geom);
CREATE INDEX service_request_geom_idx ON service_request USING GIST (geom);
CREATE INDEX service_request_geom_present_idx ON service_request (notification_number)
    WHERE geom IS NOT NULL;
"""

POINT_GEOMETRY = """
UPDATE service_request
SET geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)
WHERE latitude IS NOT NULL AND longitude IS NOT NULL
"""

ANALYZE = "ANALYZE hexagon; ANALYZE service_request; ANALYZE sr_hex_oracle;"

# The join assigns every request to the hexagon that covers its point. It is a LEFT JOIN so the
# requests with no coordinates survive it, and the brief wants an index of '0' on those rows rather
# than a NULL or a dropped row. The ORDER BY is what makes two runs byte-identical: the plan is a
# parallel scan, and without it the rows come back in whatever order the workers finish (measured:
# three runs, three different files). COLLATE "C" keeps the order independent of the image's locale.
JOIN_SERVICE_REQUESTS = """
SELECT
    s.notification_number,
    COALESCE(h.h3_index, '0') AS h3_index
FROM service_request s
LEFT JOIN hexagon h ON ST_Covers(h.geom, s.geom)
ORDER BY s.notification_number COLLATE "C"
"""

JOIN_TO_CSV = "COPY (" + JOIN_SERVICE_REQUESTS.strip() + ") TO STDOUT WITH (FORMAT csv, HEADER)"

# '0' covers two different situations, so they are counted apart: a request with no coordinates is
# expected and is what the brief describes, while a request whose point falls outside every hexagon
# is a join failure, and failures are what the threshold is about.
COUNT_UNLOCATED_REQUESTS = """
SELECT count(*)
FROM service_request s
WHERE s.latitude IS NULL AND s.longitude IS NULL
"""

COUNT_FAILED_JOINS = """
SELECT count(*)
FROM service_request s
LEFT JOIN hexagon h ON ST_Covers(h.geom, s.geom)
WHERE s.geom IS NOT NULL AND h.h3_index IS NULL
"""

# The oracle is the provided answer for this join, so every row where our index differs from its
# index is a disagreement -- a wider measurement than the failure count above, because it also
# catches a record that joined to the wrong hexagon.
COUNT_ORACLE_DISAGREEMENTS = """
SELECT count(*)
FROM service_request s
LEFT JOIN hexagon h ON ST_Covers(h.geom, s.geom)
JOIN sr_hex_oracle o ON o.notification_number = s.notification_number
WHERE COALESCE(h.h3_index, '0') IS DISTINCT FROM o.h3_level8_index
"""

# Above this share of records failing to join, the run errors out instead of exporting.
#
# Motivation, measured on this dataset: 3 of 941,634 records fail to join (0.0003%). All three are
# points inside the two resolution 8 cells that the provided geojson does not contain, so they are a
# property of the input rather than a bug in the join, and a threshold below them would fail every
# correct run. 0.1% sits about 300x above that floor: quiet on healthy data, but still catching the
# failures that matter -- a hexagon set that came back empty or doubled, a coordinate column read out
# of position, or a predicate that has stopped matching. Each of those moves the count into the
# thousands, not the tens.
JOIN_FAILURE_THRESHOLD = float(os.environ.get("CCT_FAILURE_THRESHOLD", "0.001"))

# --- loading ---


def projectServiceRequestRow(row: list[str]) -> list[str | None]:
    """Drop the unnamed index column and turn empty fields into None so COPY writes SQL NULL.

    Empty is how sr.csv.gz spells a missing latitude or longitude; NULL is what the join tests.
    """
    return [field if field != "" else None for field in row[1:]]


def projectOracleRow(row: list[str]) -> list[str | None]:
    """Keep the notification number and the h3 index from sr_hex.csv.gz."""
    return [row[0], row[15]]


def loadCsvStream(
    conn: psycopg.Connection,
    raw: Any,
    table: str,
    columns: tuple[str, ...],
    projector: Callable[[list[str]], Iterable[str | None]],
) -> int:
    """COPY one gzipped CSV from an open binary stream into a table.

    COPY is the only reasonable way to move a million rows: parsing happens in csv, the write happens
    in 1 MiB chunks, and nothing is buffered whole. NULL '' maps an empty unquoted field to SQL NULL,
    which is how a missing coordinate arrives. The width check guards the pandas index column: if the
    projection is ever wrong, the row count will not match the column list and it fails here rather
    than shifting every value one position.
    """
    statement = sql.SQL("COPY {table} ({columns}) FROM STDIN WITH (FORMAT csv, NULL '')").format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
    )
    written = 0

    with gzip.open(raw, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        next(reader)  # header

        with conn.cursor() as cur, cur.copy(statement) as copy:
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")

            for row in reader:
                projected = list(projector(row))
                if len(projected) != len(columns):
                    raise ValueError(
                        f"{table}: row {written + 2} has {len(projected)} fields, "
                        f"expected {len(columns)}"
                    )
                writer.writerow(projected)
                written += 1

                if buffer.tell() >= 1 << 20:
                    copy.write(buffer.getvalue())
                    buffer.seek(0)
                    buffer.truncate(0)

            if buffer.tell():
                copy.write(buffer.getvalue())

    return written


def loadCsv(
    conn: psycopg.Connection,
    sourceUrl: str,
    table: str,
    columns: tuple[str, ...],
    projector: Callable[[list[str]], Iterable[str | None]],
) -> int:
    """Download a gzipped CSV over HTTPS and COPY it into a table in one pass.

    The download is retried once, and only for a flaky failure: a dropped connection 30 MB into a
    37 MB object would otherwise waste the whole stage. The retry is safe because COPY is a single
    statement, so a failed one leaves no rows behind to duplicate. A row of the wrong width is a data
    problem and is raised on the spot rather than downloaded again.
    """
    for attempt in (1, 2):
        try:
            with requests.get(sourceUrl, stream=True, timeout=600) as response:
                response.raise_for_status()
                return loadCsvStream(conn, response.raw, table, columns, projector)
        # Reading response.raw raises urllib3's own errors (a dropped connection is a ProtocolError),
        # which are neither requests nor OS errors, so without them the retry never fires mid stream.
        except (requests.RequestException, urllib3.exceptions.HTTPError, OSError, EOFError) as error:
            if attempt == 2:
                raise
            logger.warning("%s: %s; retrying once", sourceUrl, error)
            time.sleep(2)

    raise AssertionError("unreachable")


def loadHexagons(conn: psycopg.Connection, records: list[dict]) -> int:
    """Insert the resolution 8 cells. 3,832 rows: pipelined executemany is fast enough here."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO hexagon (h3_index, geom) "
            "VALUES (%s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))",
            [(record["index"], json.dumps(record["geometry"])) for record in records],
        )
    return len(records)


def buildPointGeometry(conn: psycopg.Connection) -> int:
    """Turn the latitude and longitude columns into an indexed point geometry."""
    with conn.cursor() as cur:
        cur.execute(POINT_GEOMETRY)
        points = cur.rowcount
        cur.execute(ANALYZE)
    return points


def loadAll(conn: psycopg.Connection, records: list[dict]) -> dict[str, Any]:
    """Create the schema, absorb all three inputs and finish with the counts reconciled.

    The two service request sources are loaded independently on purpose: sr.csv.gz is the input and
    sr_hex.csv.gz is the oracle the join will be validated against. Deriving the oracle from the
    input, or the other way round, would make the validation circular.
    """
    started = time.monotonic()

    with conn.cursor() as cur:
        cur.execute(DDL)
    logger.info("schema created")

    t0 = time.monotonic()
    hexagons = loadHexagons(conn, records)
    RUN["timings"]["load_hexagons"] = round(time.monotonic() - t0, 3)
    logger.info("absorbed %d resolution 8 hexagons from %s", hexagons, resFile)

    t0 = time.monotonic()
    requestsLoaded = loadCsv(conn, url + srFile, "service_request", SR_COLUMNS, projectServiceRequestRow)
    RUN["timings"]["load_service_requests"] = round(time.monotonic() - t0, 3)
    logger.info("absorbed %d service requests from %s", requestsLoaded, srFile)

    t0 = time.monotonic()
    oracleLoaded = loadCsv(conn, url + srHexFile, "sr_hex_oracle", ORACLE_COLUMNS, projectOracleRow)
    RUN["timings"]["load_oracle"] = round(time.monotonic() - t0, 3)
    logger.info("absorbed %d oracle rows from %s", oracleLoaded, srHexFile)

    t0 = time.monotonic()
    points = buildPointGeometry(conn)
    RUN["timings"]["build_point_geometry"] = round(time.monotonic() - t0, 3)
    logger.info("built %d point geometries; %d rows have no coordinates", points, requestsLoaded - points)

    counts = {
        "hexagons": hexagons,
        "service_requests": requestsLoaded,
        "oracle_rows": oracleLoaded,
        "with_coordinates": points,
        "without_coordinates": requestsLoaded - points,
    }

    # Hard checks: an empty table would make every later comparison vacuously true.
    if hexagons <= 0 or requestsLoaded <= 0:
        raise ValueError(f"nothing was loaded: {counts}")
    if requestsLoaded != oracleLoaded:
        raise ValueError(
            f"the input and the oracle disagree on row count: "
            f"{requestsLoaded} vs {oracleLoaded}. The join's validation would be meaningless."
        )

    RUN["timings"]["load_total"] = round(time.monotonic() - started, 3)
    return counts


def joinCount(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    """Count the requests with no coordinates: the rows the join can only answer with '0'."""
    with conn.cursor() as cur:
        cur.execute(COUNT_UNLOCATED_REQUESTS)
        return cur.fetchall()


def joinFailures(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    """Count the requests that have coordinates but found no hexagon: the join failures.

    These are the rows a failure threshold is about. The 212,364 without coordinates are not
    failures, they are the rows the brief asks to be marked '0'.
    """
    with conn.cursor() as cur:
        cur.execute(COUNT_FAILED_JOINS)
        return cur.fetchall()


def exportJoin(conn: psycopg.Connection, path: str) -> int:
    """Write the joined rows to `path` as CSV, straight out of the database.

    COPY streams them, so a million rows never sit in Python memory. The return value is the file's
    size, which is enough for the log to tell an empty export from a real one.
    """
    with conn.cursor() as cur, open(path, "wb") as handle:
        with cur.copy(JOIN_TO_CSV) as copy:
            for block in copy:
                handle.write(bytes(block))
    return os.path.getsize(path)


def oracleDisagreements(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    """Count the rows where the join's index differs from the oracle's.

    Wider than the failure count: it also sees a record that joined to the wrong hexagon.
    """
    with conn.cursor() as cur:
        cur.execute(COUNT_ORACLE_DISAGREEMENTS)
        return cur.fetchall()


def checkJoinThreshold(failures: int, total: int) -> float:
    """Error out when too many records failed to join; return the failure rate either way."""
    rate = failures / total if total else 0.0
    if rate > JOIN_FAILURE_THRESHOLD:
        raise ValueError(
            f"join failure rate {rate:.5%} is above the {JOIN_FAILURE_THRESHOLD:.3%} threshold: "
            f"{failures} of {total} records found no hexagon"
        )
    return rate


def handover(runtime: str, password: str) -> None:
    """Tell the executor how to get at the database, when CCT_KEEP_CONTAINER has kept it."""
    logger.info("query it with: %s exec -it %s psql -U %s -d %s", runtime, container, dbUser, dbName)
    logger.info(
        "or from the host: PGPASSWORD=%s psql -h 127.0.0.1 -p %d -U %s -d %s",
        password,
        hostPort,
        dbUser,
        dbName,
    )


def main() -> int:
    startLogging()
    started = time.monotonic()

    # By default SIGTERM ends Python without running finally, which would leave the container behind.
    # Turned into SystemExit, it goes through the same teardown as an error or Ctrl-C.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))

    runtime = containerRuntime()
    # `or`, not a get() default: an empty CCT_PASSWORD would start a server the image refuses to init.
    password = os.environ.get("CCT_PASSWORD") or DB_PASSWORD
    RUN["container"] = {
        "runtime": runtime,
        "image": image,
        "name": container,
        "host_port": hostPort,
        "one_shot": not KEEP_CONTAINER,
    }

    connected = False
    try:
        t0 = time.monotonic()
        records, stats = selectJson(hexQuery)
        RUN["timings"]["fetch_hexagons"] = round(time.monotonic() - t0, 3)
        RUN["bytes"] = {
            "hexagons_scanned": stats.get("BytesScanned"),
            "hexagons_returned": stats.get("BytesReturned"),
        }
        logger.info(
            "fetched %d resolution 8 hexagons from %s (scanned %s B, returned %s B)",
            len(records),
            resFile,
            stats.get("BytesScanned"),
            stats.get("BytesReturned"),
        )

        t0 = time.monotonic()
        RUN["container"]["data_directory"] = startContainer(password)
        RUN["timings"]["container_start"] = round(time.monotonic() - t0, 3)

        with connect(password) as conn:
            connected = True
            RUN["counts"] = loadAll(conn, records)

            total = RUN["counts"]["service_requests"]
            failures = joinFailures(conn)[0][0]
            disagreements = oracleDisagreements(conn)[0][0]
            RUN["counts"]["failed_joins"] = failures
            RUN["counts"]["oracle_disagreements"] = disagreements

            # Raises before anything is exported if the failures are above the threshold.
            rate = checkJoinThreshold(failures, total)
            logger.info(
                "join: %s records, %s with no coordinates marked '0', %s failed to join (%.5f%%)",
                total,
                joinCount(conn)[0][0],
                failures,
                100 * rate,
            )
            logger.info(
                "oracle validation: %s of %s rows disagree (%.5f%% of the dataset)",
                disagreements,
                total,
                100 * disagreements / total if total else 0.0,
            )

            t0 = time.monotonic()
            RUN["bytes"]["joined_csv"] = exportJoin(conn, JOINED_CSV)
            RUN["timings"]["export_join"] = round(time.monotonic() - t0, 3)
            logger.info("wrote %s B of joined rows to %s", RUN["bytes"]["joined_csv"], JOINED_CSV)

        if KEEP_CONTAINER:
            handover(runtime, password)

    except BaseException as error:
        # The traceback reaches the console on its own; this puts the failure in join.log as well.
        logger.error("run failed: %r", error)
        logger.debug("traceback", exc_info=True)
        # A connection that drops mid run almost always means the kernel killed a postgres process
        # for memory, and nothing else says so, so name it rather than leave only "server closed the
        # connection". Measured: the run peaks at 1.1 GB in the container, fails at a 1 GB limit and
        # completes at 1.25 GB.
        # A failure to connect in the first place is left to its own libpq message.
        if connected and isinstance(error, psycopg.OperationalError):
            logger.error(
                "the database connection was lost. The usual cause is the container running out of "
                "memory: the run needs about 1.5 GB free in the container runtime (the Docker "
                "Desktop, podman machine or colima VM on macOS and Windows)."
            )
        raise

    finally:
        if KEEP_CONTAINER:
            logger.info(
                "CCT_KEEP_CONTAINER is on: %s is still running. Remove it with: %s rm --force %s",
                container,
                runtime,
                container,
            )
        else:
            t0 = time.monotonic()
            removed = removeContainer()
            RUN["timings"]["container_remove"] = round(time.monotonic() - t0, 3)
            if removed:
                logger.info("container %s removed", container)
            else:
                logger.error(
                    "container %s is still there. Remove it with: %s rm --force %s",
                    container,
                    runtime,
                    container,
                )

    RUN["timings"]["total"] = round(time.monotonic() - started, 3)
    logger.info("timings in seconds: %s", RUN["timings"])
    logger.info("counts: %s", RUN["counts"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
