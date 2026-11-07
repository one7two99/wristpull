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
