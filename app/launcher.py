"""Start the local WebUI and open a browser after this server is ready."""

import threading
import webbrowser

import uvicorn

from .config import WEBUI_HOST, WEBUI_PORT, ensure_directories


def _open_when_ready(server, stopped: threading.Event, url: str) -> None:
    while not stopped.wait(0.1):
        if server.started:
            webbrowser.open(url)
            return


def run() -> None:
    ensure_directories()
    host = "127.0.0.1" if WEBUI_HOST == "0.0.0.0" else WEBUI_HOST
    if ":" in host:
        host = f"[{'::1' if host == '::' else host}]"
    url = f"http://{host}:{WEBUI_PORT}"
    server = uvicorn.Server(uvicorn.Config("app.main:app", host=WEBUI_HOST, port=WEBUI_PORT))
    stopped = threading.Event()
    opener = threading.Thread(target=_open_when_ready, args=(server, stopped, url), daemon=True)
    opener.start()
    try:
        # Keep the server in the main thread so Ctrl+C works on Windows too.
        server.run()
    finally:
        stopped.set()


if __name__ == "__main__":
    run()
