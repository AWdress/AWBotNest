"""Read-only smoke checks for API availability and authentication boundaries."""

from __future__ import annotations

import os

import httpx


BASE_URL = os.environ.get("AWBOTNEST_TEST_URL", "http://127.0.0.1:18001")


def main() -> None:
    token = os.environ.get("AWBOTNEST_TEST_TOKEN", "")
    if not token:
        raise SystemExit("AWBOTNEST_TEST_TOKEN is required")
    admin = {"Authorization": f"Bearer {token}"}
    private_paths = (
        "/api/status", "/api/accounts", "/api/bots", "/api/plugins", "/api/settings",
        "/api/ui/profile", "/api/ui/notifications", "/api/ui/health",
        "/api/scheduler/jobs", "/api/activity", "/api/logs/recent?limit=20", "/api/self-check",
    )
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        for path in ("/api/health", "/api/auth/status", "/openapi.json"):
            response = client.get(path)
            if response.status_code != 200:
                raise SystemExit(f"public endpoint failed: {path} -> {response.status_code}")
        for path in private_paths:
            anonymous = client.get(path)
            if anonymous.status_code != 401:
                raise SystemExit(f"private endpoint exposed: {path} -> {anonymous.status_code}")
            api_header_only = client.get(path, headers={"X-API-Key": "not-an-admin-token"})
            if api_header_only.status_code != 401:
                raise SystemExit(f"API key header bypassed admin auth: {path}")
            authorized = client.get(path, headers=admin)
            if authorized.status_code != 200:
                raise SystemExit(f"admin endpoint failed: {path} -> {authorized.status_code}")

        api_key = os.environ.get("AWBOTNEST_TEST_API_KEY", "")
        if api_key:
            open_response = client.get("/api/v1/status", headers={"X-API-Key": api_key})
            if open_response.status_code != 200:
                raise SystemExit(f"open API failed: {open_response.status_code}")
            admin_response = client.get("/api/settings", headers={"X-API-Key": api_key})
            if admin_response.status_code != 401:
                raise SystemExit("open API key unexpectedly grants administrator access")
    print(f"API smoke passed: {len(private_paths)} private endpoints + public boundary")


if __name__ == "__main__":
    main()
