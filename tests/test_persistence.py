import logging

from smartthings_pushover.persistence import TrackerStore


def test_roundtrip(tmp_path):
    store = TrackerStore(tmp_path / "state" / "washer.json")
    assert store.load() is None
    assert store.save({"started_at": 1.5, "course": "1C"})
    assert store.load() == {"started_at": 1.5, "course": "1C"}
    assert not (tmp_path / "state" / "washer.json.tmp").exists()


def test_corrupt_file_is_ignored(tmp_path, caplog):
    path = tmp_path / "washer.json"
    path.write_text("{not json")
    store = TrackerStore(path, logging.getLogger("t"))
    with caplog.at_level(logging.WARNING):
        assert store.load() is None
        path.write_text("[1,2]")
        assert store.load() is None
    assert sum("will not persist" in r.message for r in caplog.records) == 1  # warned once


def test_unwritable_dir_fails_softly(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    store = TrackerStore(blocker / "washer.json", logging.getLogger("t"))
    assert store.save({"a": 1}) is False
