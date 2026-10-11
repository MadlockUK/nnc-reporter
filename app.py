"""NNC Reporter - the web app.

Run:   python app.py --open          (Windows: run.bat)
Then:  http://127.0.0.1:8712

Everyone signs in. The first admin is created with
    python -m nncr.web.manage create-admin --email you@example.com
and invites everyone else from the Admin page. Settings and secrets come from
environment variables or a local, git-ignored .env file - see .env.example.

Diagnostics:
    python app.py --inspect [--url URL]   dump a live council form's fields
"""
import sys

from nncr.web.server import app, main  # noqa: F401  (uvicorn app:app also works)

if __name__ == "__main__":
    sys.exit(main())
