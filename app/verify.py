"""One-shot verification entrypoint used by Docker Compose.

It runs the pytest suite against the production code, waits for the API service to
accept HTTP requests, and probes its production health endpoint.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


def run_tests() -> None:
    print("==> Running pytest suite", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--disable-warnings"],
        check=False,
    )
    if result.returncode != 0:
        print("verify: pytest failed", file=sys.stderr, flush=True)
        raise SystemExit(result.returncode)


def probe_health() -> None:
    host = os.getenv("VERIFY_API_HOST", "api")
    port = os.getenv("VERIFY_API_PORT", os.getenv("API_PORT", "8000"))
    url = os.getenv("VERIFY_URL", f"http://{host}:{port}/health")
    deadline = time.time() + float(os.getenv("VERIFY_TIMEOUT_SECONDS", "60"))
    last_error: Exception | None = None

    print(f"==> Probing production endpoint {url}", flush=True)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                body = response.read().decode("utf-8")
                if response.status == 200 and json.loads(body).get("ok") is True:
                    print("verify: API health probe succeeded", flush=True)
                    return
                last_error = RuntimeError(f"unexpected health response {response.status}: {body}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        time.sleep(1.0)

    print(f"verify: API did not become healthy: {last_error}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def main() -> None:
    run_tests()
    probe_health()


if __name__ == "__main__":
    main()
