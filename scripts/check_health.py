"""Container health: both the private API and the public Streamlit server."""

import os
from urllib.request import ProxyHandler, build_opener


def main():
    opener = build_opener(ProxyHandler({}))
    api_port = os.environ.get("API_PORT", "8000")
    ui_port = os.environ.get("UI_PORT", os.environ.get("PORT", "8501"))
    for port, path in ((api_port, "/v1/health"), (ui_port, "/_stcore/health")):
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
            if response.status != 200:
                raise RuntimeError(f"Unhealthy service on port {port}")


if __name__ == "__main__":
    main()
