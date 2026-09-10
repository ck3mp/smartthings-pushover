import os

from smartthings_pushover.health import Heartbeat, Reachability


def test_reachability_offline_once_then_online():
    r = Reachability(offline_after_s=900)
    assert r.outage_s(0) is None
    assert not r.crossed_offline(0)
    r.mark_down(100)
    r.mark_down(200)  # second call does not move the start
    assert r.outage_s(400) == 300
    assert not r.crossed_offline(900)
    assert r.crossed_offline(1000)
    assert not r.crossed_offline(2000)  # only once per outage
    outage, was_offline = r.mark_up(2500)
    assert outage == 2400 and was_offline
    assert r.outage_s(2600) is None
    r.mark_down(3000)
    assert r.mark_up(3010) == (10, False)


def test_heartbeat_touch_and_freshness(tmp_path):
    path = tmp_path / "hb" / "beat"
    assert not Heartbeat.is_fresh(path, 60)
    assert Heartbeat(path).touch()
    assert Heartbeat.is_fresh(path, 60)
    os.utime(path, (0, 0))
    assert not Heartbeat.is_fresh(path, 60)
