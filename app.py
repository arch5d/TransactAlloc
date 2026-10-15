"""ASGI application object.

Split out of main.py so `uvicorn app:app` and `python main.py serve` both
work, and so tests can import the app without launching a server.
"""

from pathlib import Path
import sys

# See main.py: uvicorn may run on a cp1252 console where printing an arrow
# raises UnicodeEncodeError inside a worker thread.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

import api

app = FastAPI(title="TransactAlloc", version="1.0.0",
              description="Adaptive cloud resource manager with fragmentation-aware reallocation")
app.include_router(api.router, prefix="/api")


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True})


_static = Path(__file__).parent / "static"
if _static.is_dir():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
