"""Unit tests for the setup stage of 2_Postgres_Join.py. No container, no network.

Run with: .venv/bin/pytest -q tests/test_section2.py
"""

import pytest


def test_service_request_projection_drops_the_pandas_index_column(join):
    row = ["7"] + [f"value{position}" for position in range(1, 16)]

    projected = join.projectServiceRequestRow(row)

    assert projected == [f"value{position}" for position in range(1, 16)]
    assert len(projected) == len(join.SR_COLUMNS)


def test_service_request_projection_turns_empty_fields_into_null(join):
    # A blank latitude and longitude is how sr.csv.gz spells "no coordinates". 16 source fields: the
    # unnamed index column, then the 15 columns of SR_COLUMNS.
    row = ["0", "210545626"] + [""] * 11 + ["MONTAGUE GARDENS", "", ""]

    projected = join.projectServiceRequestRow(row)

    assert len(row) == len(join.SR_COLUMNS) + 1
    assert projected[0] == "210545626"
    assert projected[12] == "MONTAGUE GARDENS"
    assert projected[-1] is None
    assert all(field is None for field in projected[1:12])


def test_oracle_projection_keeps_the_number_and_the_index(join):
    row = [f"field{position}" for position in range(16)]

    assert join.projectOracleRow(row) == ["field0", "field15"]
    assert len(join.projectOracleRow(row)) == len(join.ORACLE_COLUMNS)


def test_the_input_table_and_the_oracle_have_the_columns_their_loaders_expect(join):
    # The width check in loadCsvStream is only as good as these lists.
    assert len(join.SR_COLUMNS) == 15
    assert join.SR_COLUMNS[0] == "notification_number"
    assert join.SR_COLUMNS[-1] == "longitude"
    assert join.ORACLE_COLUMNS == ("notification_number", "h3_level8_index")


def test_container_runtime_honours_the_override(join, monkeypatch):
    monkeypatch.setenv("CCT_RUNTIME", "podman")
    assert join.containerRuntime() == "podman"


def test_container_runtime_prefers_docker_then_podman_then_refuses(join, monkeypatch):
    # Decided by a faked PATH, so the result does not depend on what the test machine has installed.
    monkeypatch.delenv("CCT_RUNTIME", raising=False)
    installed = {"docker", "podman"}
    monkeypatch.setattr(join.shutil, "which", lambda name: f"/usr/bin/{name}" if name in installed else None)

    assert join.containerRuntime() == "docker"
    installed.discard("docker")
    assert join.containerRuntime() == "podman"
    installed.discard("podman")
    with pytest.raises(RuntimeError, match="neither docker nor podman"):
        join.containerRuntime()


def test_the_hexagon_query_asks_for_the_geometry_the_join_needs(join):
    # Section 1 only needed the properties; the join needs the polygon as well.
    assert "s.geometry" in join.hexQuery
    assert join.hexQuery.rstrip().endswith("s.properties.resolution = 8")


def test_dsn_quotes_the_password_and_targets_the_published_port(join):
    dsn = join.dsn("p@ss/w0rd")

    assert "p%40ss%2Fw0rd" in dsn
    assert f"127.0.0.1:{join.hostPort}" in dsn
    assert dsn.endswith(f"/{join.dbName}")
