from pathlib import Path

from wristpull.runtime import host_path, in_container


def test_in_container_reads_the_marker():
    assert not in_container({})
    assert not in_container({"WRISTPULL_IN_CONTAINER": "0"})
    assert in_container({"WRISTPULL_IN_CONTAINER": "1"})


def test_host_path_maps_the_data_mount_only():
    env = {"WRISTPULL_DATA_DIR": "/data", "WRISTPULL_HOST_DATA_DIR": "/home/alice/Health/wristpull"}
    assert host_path(Path("/data/exports/x.md"), env) == Path("/home/alice/Health/wristpull/exports/x.md")
    assert host_path(Path("/config/config.toml"), env) == Path("/config/config.toml")
    assert host_path(Path("/data/x"), {}) == Path("/data/x")


def test_env_file_and_certs_dir_come_from_the_environment_or_the_checkout(tmp_path: Path):
    from wristpull.runtime import certs_dir, env_file

    assert env_file({"WRISTPULL_ENV_FILE": "/checkout/.env"}, cwd=tmp_path) == Path("/checkout/.env")
    assert certs_dir({"WRISTPULL_CERTS_DIR": "/certs"}, cwd=tmp_path) == Path("/certs")
    assert env_file({}, cwd=tmp_path) is None and certs_dir({}, cwd=tmp_path) is None  # not a checkout
    (tmp_path / "compose.yaml").write_text("services: {}\n", "utf-8")
    assert env_file({}, cwd=tmp_path) == tmp_path / ".env"
    assert certs_dir({}, cwd=tmp_path) is None  # no deploy/certs folder yet
    (tmp_path / "deploy" / "certs").mkdir(parents=True)
    assert certs_dir({}, cwd=tmp_path) == tmp_path / "deploy" / "certs"
    assert env_file({"WRISTPULL_ENV_FILE": "  "}, cwd=tmp_path) == tmp_path / ".env"  # blank: fall back
