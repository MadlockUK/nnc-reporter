"""Submitting at the web level: the server fills the form in a headless browser,
shows it, and presses Submit only when the person approves that report."""
import glob
import os
import time

import pytest

from conftest import login, make_user

FORM = """<!doctype html><html><body>
<h1>Report fly-tipping</h1>
<label for="d">Description</label><textarea id="d">Mattress on the verge</textarea>
<label for="w">What3Words</label><input id="w" value="filled.count.soap">
<label><input type="checkbox" id="c" checked> I agree</label>
<input type="text" id="typed" placeholder="type here">
<button id="sub" onclick="document.body.innerHTML='<p>Thank you for submitting your form.'
  + ' Your reference number is: FLY800000123.</p>'">Submit</button>
</body></html>"""


def _chromium():
    found = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")
    return os.environ.get("NNC_CHROMIUM_PATH") or (found[0] if found else "")


pytestmark = pytest.mark.skipif(not _chromium() and not os.environ.get("CI_HAS_PLAYWRIGHT"),
                                reason="no Chromium for Playwright here")


@pytest.fixture
def server_mode(env, monkeypatch):
    from nncr import submit
    if _chromium():
        monkeypatch.setenv("NNC_CHROMIUM_PATH", _chromium())
    monkeypatch.setenv("NNC_SUBMIT_MODE", "server")

    def fake_prefill(page, r, cfg, signed):
        page.set_content(FORM)
        return True
    monkeypatch.setattr(submit, "_prefill", fake_prefill)
    return submit


def wait(fn, seconds=60, step=0.4):
    end = time.time() + seconds
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(step)
    raise AssertionError("timed out")


def test_prefill_review_and_approve_over_http(client, server_mode):
    make_user("ann@example.com")
    login(client, "ann@example.com")
    r = client.post("/api/reports/manual", json={"category": "Flytipping",
                                                 "detail": "Mattress on the verge",
                                                 "lat": 52.4914, "lon": -0.6923})
    rid = r.json()["added"]
    assert client.post("/api/submit", json={"ids": [rid]}).status_code == 200
    # A second run cannot start while this one is open.
    assert client.post("/api/submit", json={"ids": [rid]}).status_code == 409

    run = wait(lambda: (lambda d: d["run"] if d["run"]["active"] and
                        str(rid) in {str(k) for k in d["run"]["reports"]} else None)(
        client.get("/api/log").json()))
    info = run["reports"].get(str(rid)) or run["reports"].get(rid)
    assert info["reached"] is True and info["has_preview"]
    values = {f["value"] for f in info["fields"]}
    assert "Mattress on the verge" in values and "filled.count.soap" in values

    # Nothing has been sent yet.
    assert client.get("/api/state").json()["reports"][0]["status"] == "awaiting"
    assert client.get(f"/api/run/{rid}/preview").headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/run/{rid}/frame").status_code in (200, 204)
    frame = wait(lambda: (lambda r: r if r.status_code == 200 else None)(
        client.get(f"/api/run/{rid}/frame")))
    assert frame.headers["content-type"] == "image/jpeg"

    # The live view: click into a box and type.
    assert client.post(f"/api/run/{rid}/input", json={"kind": "click", "x": 9999, "y": 1}).status_code == 400
    assert client.post(f"/api/run/{rid}/input", json={"kind": "type", "text": "hello"}).status_code == 200
    assert client.post(f"/api/run/{rid}/input", json={"kind": "key", "key": "Tab"}).status_code == 200

    # Someone else cannot approve it: their run state is their own.
    from fastapi.testclient import TestClient
    from nncr.web import server
    make_user("bob@example.com")
    with TestClient(server.app, base_url="http://testserver",
                    headers={"Origin": "http://testserver"}) as b:
        login(b, "bob@example.com")
        assert b.post(f"/api/run/{rid}/submit").status_code == 409
        assert b.get("/api/log").json()["run"]["active"] is False

    assert client.post(f"/api/run/{rid}/submit").status_code == 200
    rep = wait(lambda: next((x for x in client.get("/api/state").json()["reports"]
                             if x["status"] == "submitted"), None))
    assert rep["reference"] == "FLY800000123"
    wait(lambda: not client.get("/api/log").json()["busy"])
    log = "\n".join(client.get("/api/log").json()["log"]["lines"])
    assert "pressed 'submit' on your instruction" in log
    from nncr.web import db
    assert any(e["event"] == "report.submit_approved" for e in db.audit_log(20))


def test_cancel_leaves_report_unsent(client, server_mode):
    make_user("ann@example.com")
    login(client, "ann@example.com")
    rid = client.post("/api/reports/manual", json={"category": "Flytipping", "detail": "Bags",
                                                   "lat": 52.49, "lon": -0.69}).json()["added"]
    client.post("/api/submit", json={"ids": [rid]})
    wait(lambda: client.get("/api/log").json()["run"]["reports"])
    assert client.post(f"/api/run/{rid}/cancel").status_code == 200
    wait(lambda: not client.get("/api/log").json()["busy"])
    rep = client.get("/api/state").json()["reports"][0]
    assert rep["status"] == "ready" and not rep["reference"]


def test_server_browser_only_visits_council_sites(env, monkeypatch):
    from playwright.sync_api import sync_playwright
    from nncr import submit
    monkeypatch.setenv("NNC_SUBMIT_MODE", "server")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=_chromium() or None)
        ctx = b.new_context()
        submit.guard_navigation(ctx)
        page = ctx.new_page()
        for url in ("https://example.org/", "http://169.254.169.254/latest/meta-data/",
                    "https://northnorthants.gov.uk.evil.example/"):
            with pytest.raises(Exception) as e:
                page.goto(url, timeout=10000)
            assert "ERR_BLOCKED_BY_CLIENT" in str(e.value)
        # A link clicked inside a page is refused the same way.
        page = ctx.new_page()
        page.set_content('<a id="a" href="https://example.org/">away</a>')
        page.click("#a")
        page.wait_for_timeout(800)
        assert "example.org" not in page.url
        b.close()
    assert "refused to open https://example.org" in "\n".join(submit.log_for()["lines"])
