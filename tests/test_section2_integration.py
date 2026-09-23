"""Tests for the parts of 2_Postgres_Join.py that need a real database.

They start their own throwaway container, on a different name and port from the script's, so they can
run alongside a kept-alive development container without touching it.

Run with: CCT_TEST_CONTAINER=1 python -m pytest -q tests/test_section2_integration.py
Without CCT_TEST_CONTAINER=1 they skip, so the default test run stays fast and offline.

The tests that matter and cannot be unit tested: that the image really gives us PostGIS, that the
schema is what the join will query, that COPY maps a blank coordinate to NULL, that the point
geometry is built, and that a row of the wrong width is rejected instead of silently shifting every
value one column to the left.
"""

import gzip
import io
import os

import pytest

TEST_CONTAINER = "cct-section2-test"
TEST_PORT = 55433
TEST_PASSWORD = "Yeb026!"  # public data, loopback port: a fixed literal keeps runs reproducible


@pytest.fixture(scope="module")
def database(join):
    if os.environ.get("CCT_TEST_CONTAINER") != "1":
        pytest.skip("set CCT_TEST_CONTAINER=1 to run the tests that start a container")
    try:
        join.containerRuntime()
    except RuntimeError:
        pytest.skip("no container runtime on PATH")

    # Every container function reads these two module globals, so pointing them at the test container
    # is all it takes to keep the tests away from a development container on the script's own port.
    join.container = TEST_CONTAINER
    join.hostPort = TEST_PORT

    join.startContainer(TEST_PASSWORD)
    try:
        with join.connect(TEST_PASSWORD) as conn:
            with conn.cursor() as cur:
                cur.execute(join.DDL)
            yield conn
    finally:
        join.removeContainer()


def gzipped(rows: list[list[str]]) -> bytes:
    """Build what sr.csv.gz looks like: gzipped CSV, no header, newline terminated."""
    buffer = io.BytesIO()
    with gzip.open(buffer, "wt", encoding="utf-8", newline="") as handle:
        for row in rows:
            handle.write(",".join(row) + "\n")
    buffer.seek(0)
    return buffer.getvalue()


def requestRow(index: str, notification: str, suburb: str, latitude: str, longitude: str) -> list[str]:
    """One row of sr.csv.gz: the unnamed index column, then the fifteen SR_COLUMNS in order."""
    return [
        index,
        notification,
        "REF" + notification,
        "2020/01/01 00:00:00",
        "2020/01/02 00:00:00",
        "DIRECTORATE",
        "DEPARTMENT",
        "BRANCH",
        "SECTION",
        "CODE_GROUP",
        "CODE",
        "CAUSE_GROUP",
        "CAUSE_CODE",
        suburb,
        latitude,
        longitude,
    ]


def test_the_container_gives_us_postgis(database):
    with database.cursor() as cur:
        cur.execute("SELECT postgis_version()")
        version = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pg_extension WHERE extname = 'postgis'")
        installed = cur.fetchone()[0]

    assert version.startswith("3.")
    assert installed == 1


def test_the_schema_matches_what_the_join_will_query(database):
    with database.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        tables = [row[0] for row in cur.fetchall()]

        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
            "AND indexdef LIKE '%USING gist%' ORDER BY indexname"
        )
        gist = [row[0] for row in cur.fetchall()]

        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'service_request' ORDER BY ordinal_position"
        )
        columns = [row[0] for row in cur.fetchall()]

    # CREATE EXTENSION postgis creates spatial_ref_sys as well, so the three tables are checked as a
    # subset of the schema rather than the whole of it.
    assert {"hexagon", "service_request", "sr_hex_oracle"} <= set(tables)
    assert {"hexagon_geom_idx", "service_request_geom_idx"} <= set(gist)
    assert columns[:3] == ["notification_number", "reference_number", "creation_timestamp"]
    assert columns[-2:] == ["longitude", "geom"]


def test_the_loader_absorbs_a_stream_and_maps_blank_coordinates_to_null(join, database):
    rows = [
        requestRow("0", "210545626", "MONTAGUE GARDENS", "-33.9", "18.5"),
        requestRow("1", "210545627", "", "", ""),
    ]
    assert all(len(row) == len(join.SR_COLUMNS) + 1 for row in rows)

    # A header line, then the rows: the same shape as the real file.
    stream = io.BytesIO(gzipped([list(join.SR_COLUMNS), *rows]))

    written = join.loadCsvStream(
        database, stream, "service_request", join.SR_COLUMNS, join.projectServiceRequestRow
    )

    with database.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(latitude), count(longitude), count(official_suburb) "
            "FROM service_request"
        )
        total, latitudes, longitudes, suburbs = cur.fetchone()

    assert written == 2
    assert (total, latitudes, longitudes, suburbs) == (2, 1, 1, 1)


def test_points_are_built_only_where_there_are_coordinates(join, database):
    points = join.buildPointGeometry(database)

    with database.cursor() as cur:
        cur.execute("SELECT count(*) FROM service_request WHERE geom IS NOT NULL")
        stored = cur.fetchone()[0]
        cur.execute(
            "SELECT ST_AsText(geom) FROM service_request WHERE notification_number = '210545626'"
        )
        point = cur.fetchone()[0]

    assert points == 1
    assert stored == 1
    assert point == "POINT(18.5 -33.9)"


def test_a_hexagon_covers_the_point_inside_it_and_not_the_row_without_one(join, database):
    square = {
        "type": "Polygon",
        "coordinates": [
            [[18.49, -33.91], [18.51, -33.91], [18.51, -33.89], [18.49, -33.89], [18.49, -33.91]]
        ],
    }

    inserted = join.loadHexagons(database, [{"index": "88ad3612a9fffff", "geometry": square}])

    with database.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM hexagon h JOIN service_request s ON ST_Covers(h.geom, s.geom) "
            "WHERE s.notification_number = '210545626'"
        )
        covered = cur.fetchone()[0]

        cur.execute("SELECT count(*) FROM service_request WHERE geom IS NULL")
        withoutGeometry = cur.fetchone()[0]

    assert inserted == 1
    assert covered == 1
    assert withoutGeometry == 1


def test_a_row_of_the_wrong_width_is_rejected_rather_than_shifted(join, database):
    # Four fields where sixteen are expected: exactly what a wrong projection looks like.
    stream = io.BytesIO(gzipped([["header"], ["0", "210545628", "-33.9", "18.5"]]))

    with pytest.raises(ValueError, match="expected 15"):
        join.loadCsvStream(
            database, stream, "service_request", join.SR_COLUMNS, join.projectServiceRequestRow
        )

    # The failed COPY must have left the table exactly as it was.
    with database.cursor() as cur:
        cur.execute("SELECT count(*) FROM service_request")
        remaining = cur.fetchone()[0]

    assert remaining == 2
