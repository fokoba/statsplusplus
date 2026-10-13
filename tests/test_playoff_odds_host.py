import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import playoff_odds as po


def test_resolve_host_sends_cookie():
    seen = {}

    class R:
        def geturl(self):
            return "https://atl-01.statsplus.net/ppl/playoffodds/"

    def fake_open(req, timeout=0):
        seen["cookie"] = req.get_header("Cookie")
        return R()

    with mock.patch.object(po.urllib.request, "urlopen", fake_open):
        assert po._resolve_host("ppl", "sessionid=x") == "https://atl-01.statsplus.net"
    assert seen["cookie"] == "sessionid=x"
