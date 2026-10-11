"""The NNC Reporter web app: everything wired together.

Run:   python app.py                 (or: uvicorn nncr.web.server:app)
Then:  http://127.0.0.1:8712         (put it behind HTTPS for anyone else - see README)
"""
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from .. import config, routing, store
from . import account, admin, auth, db, org, reports, settings, usercfg

log = logging.getLogger("nnc")
HERE = Path(__file__).resolve().parent
PAGES = HERE / "pages"


def _page(name: str) -> HTMLResponse:
    html = (PAGES / name).read_text(encoding="utf-8")
    if "<!--NAV-->" in html:
        html = html.replace("<!--NAV-->", (PAGES / "_nav.html").read_text(encoding="utf-8"))
    return HTMLResponse(html)


def _each_user(fn) -> None:
    """Run fn(user, cfg) inside each active person's own data folder."""
    for u in db.users():
        if u["status"] != "active":
            continue
        tokens = config.use_user(f"u{u['id']}", auth.user_dir(u["id"]))
        try:
            fn(u, usercfg.build(u))
        except Exception as e:                                   # noqa: BLE001
            log.warning("start-up check for user %s failed: %s", u["id"], e)
        finally:
            config.release_user(tokens)


def bootstrap() -> None:
    s = settings.get()
    db.purge_expired()
    if not db.users():
        if s.bootstrap_admin:
            uid = db.create_user(s.bootstrap_admin, "", "admin")
            url = auth.issue_invite(db.user(uid), None)
            db.audit("user.invited", uid, detail="bootstrap admin")
            log.warning("\n\n  First admin invited: %s\n  Set the password here (works once, "
                        "for %d days):\n\n  %s\n", s.bootstrap_admin, auth.INVITE_DAYS, url)
        else:
            log.warning("\n\n  No accounts yet. Create the first admin with:\n\n"
                        "    python -m nncr.web.manage create-admin --email you@example.com\n\n"
                        "  (or set NNC_BOOTSTRAP_ADMIN=you@example.com and restart)\n")
    if not s.base_url:
        log.warning("NNC_BASE_URL is not set - links in emails will point at "
                    "http://127.0.0.1:%d", s.port)
    if not s.cookie_secure and s.host not in ("127.0.0.1", "localhost"):
        log.warning("Cookies are not marked Secure. Serve the app over HTTPS and set "
                    "NNC_BASE_URL=https://... before letting anyone else use it.")
    if not s.mail_configured:
        log.warning("SMTP is not configured: invitations and password resets will be "
                    "written to this log instead of emailed.")

    def tidy(u, cfg):
        routing.fix_existing(store, cfg)
        routing.restate_bins(store)
    _each_user(tidy)

    def housekeeping():
        while True:
            time.sleep(3600)
            try:
                db.purge_expired()
            except Exception:                                    # noqa: BLE001
                pass
    threading.Thread(target=housekeeping, daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap()
    yield


app = FastAPI(title="NNC Reporter", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.add_middleware(auth.Gatekeeper)          # outermost: nothing runs before it
app.mount("/assets", StaticFiles(directory=HERE / "assets"), name="assets")

app.include_router(auth.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(org.router)
app.include_router(reports.router)


# ------------------------------------------------------------------ pages

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    u = auth.me(request)
    if u["role"] == "viewer":
        return RedirectResponse("/map", status_code=303)
    return HTMLResponse((Path(config.BASE_DIR) / "nncr" / "static" / "index.html")
                        .read_text(encoding="utf-8"))


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return _page("login.html")


@app.get("/forgot", response_class=HTMLResponse)
def forgot_page():
    return _page("login.html")


@app.get("/reset", response_class=HTMLResponse)
def reset_page():
    return _page("login.html")


@app.get("/invite", response_class=HTMLResponse)
def invite_page():
    return _page("login.html")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return _page("privacy.html")


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    auth.me(request)
    return _page("account.html")


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    if auth.me(request)["role"] != "admin":
        return JSONResponse({"detail": "admins only"}, status_code=403)
    return _page("admin.html")


@app.get("/summary", response_class=HTMLResponse)
def summary_page(request: Request):
    auth.me(request)
    return _page("summary.html")


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/favicon.ico")
def favicon():
    return FileResponse(HERE / "assets" / "icon.svg", media_type="image/svg+xml")


# ------------------------------------------------------------------ main

def main(argv=None) -> int:
    import argparse
    import webbrowser
    ap = argparse.ArgumentParser(description="NNC Reporter web app")
    ap.add_argument("--host", default=None, help="address to listen on "
                    "(default NNC_HOST or 127.0.0.1; use 0.0.0.0 in a container)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--open", action="store_true", help="open a browser at the app")
    ap.add_argument("--inspect", action="store_true",
                    help="dump the live council form's fields for fixing selectors")
    ap.add_argument("--url", default=None)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.inspect:
        from .. import submit
        submit.inspect_form(args.url)
        return 0

    import uvicorn
    s = settings.get()
    host = args.host or s.host
    port = args.port or s.port
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    print(f"\nNNC Reporter running at {url}   (Ctrl+C to stop)\n")
    if args.open:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=True,
                forwarded_allow_ips=s.forwarded_allow_ips, server_header=False)
    return 0
