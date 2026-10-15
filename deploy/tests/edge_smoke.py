"""Exercise the pinned official edge locally; all published test ports are loopback-only."""

import socket
import ssl
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "uozi/nginx-ui@sha256:d009a852752a62f6621765a352373d4b43d2ce3bdae5f17b0c9ba634659b2622"
CONTAINER = "njtech-ops-edge-proof"


def main():
    subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            CONTAINER,
            "--platform",
            "linux/amd64",
            "--publish",
            "127.0.0.1:18980:80",
            "--publish",
            "127.0.0.1:18943:443",
            "--publish",
            "127.0.0.1:18900:9000",
            "--env",
            "NGINX_UI_PRESERVE_BUNDLED_CONF=true",
            "--mount",
            f"type=bind,source={ROOT / 'deploy/edge/nginx.conf'},target=/etc/nginx/nginx.conf,readonly",
            "--mount",
            f"type=bind,source={ROOT / 'deploy/edge/00-deny.conf'},target=/etc/nginx/sites-enabled/00-deny.conf,readonly",
            IMAGE,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    try:
        opener = build_opener(ProxyHandler({}))
        for attempt in range(60):
            try:
                with opener.open("http://127.0.0.1:18900", timeout=3) as response:
                    assert response.status == 200
                break
            except (URLError, ConnectionError, TimeoutError):
                if attempt == 59:
                    raise
                time.sleep(1)
        config = subprocess.run(
            ["docker", "exec", CONTAINER, "nginx", "-T"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "proxy_pass http://127.0.0.1:9000" not in config.stdout
        with socket.create_connection(("127.0.0.1", 18980), timeout=3) as connection:
            connection.sendall(b"GET / HTTP/1.0\r\nHost: attacker.invalid\r\n\r\n")
            assert connection.recv(8192) == b""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            with socket.create_connection(("127.0.0.1", 18943), timeout=3) as connection:
                with context.wrap_socket(connection, server_hostname="attacker.invalid"):
                    raise AssertionError("Unknown TLS host must not receive the UI")
        except ssl.SSLError:
            pass
        print(
            "Official edge passed: management HTTP200 only on loopback UI port; unknown public HTTP closed and TLS rejected."
        )
    finally:
        subprocess.run(["docker", "stop", CONTAINER], check=True, timeout=60, capture_output=True)


if __name__ == "__main__":
    main()
