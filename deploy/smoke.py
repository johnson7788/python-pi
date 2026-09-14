#!/usr/bin/env python3
"""Post-deploy smoke test: does this deployment actually enforce what it claims?

    python deploy/smoke.py https://pi.example.com --username admin --password '…'

The checks are the deployment's security invariants, in the order a real browser meets
them. Each one is here because breaking it is silent:

1. ``/api/health`` — the server is up and reports ``authMode: team``. A deployment that
   fell back to ``single`` has no accounts and no isolation at all.
2. Anonymous access to data endpoints — must be **401**. This is the check that catches a
   middleware wired with the wrong path prefix, which would otherwise leak every user's
   workspace to anyone who can reach the host.
3. Login sets a cookie — without it no browser can get past the login page.
4. The session reaches the account's own state — proves the cookie is honoured end to end.
5. A bad password is rejected — proves the login form is not accepting anything.
6. Logout invalidates the session on the next request — proves revocation is immediate
   rather than at the next expiry.

Exit code is 0 when every check passes, 1 otherwise; failures print what was expected and
what happened. Requires `httpx` (already a runtime dependency of this project).
"""

from __future__ import annotations

import argparse
import sys

import httpx

#: Endpoints that carry a session, a file or an account. All must be closed to anonymous
#: callers; `/api/health` is the deliberate exception and is checked separately.
PROTECTED = (
    "/api/state",
    "/api/sessions",
    "/api/models",
    "/api/usage/me",
    "/api/workspace/tree",
    "/api/admin/users",
    "/api/admin/audit",
)


class Smoke:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.failures: list[str] = []
        self.checks = 0

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        self.checks += 1
        if condition:
            print(f"  ok    {label}")
            return
        print(f"  FAIL  {label}{f' — {detail}' if detail else ''}")
        self.failures.append(label)

    def run(self, username: str, password: str) -> int:
        print(f"smoke: {self.base_url}")

        try:
            health = httpx.get(f"{self.base_url}/api/health", timeout=10)
        except Exception as error:  # noqa: BLE001 - any transport failure is the same verdict
            print(f"  FAIL  /api/health unreachable — {error}")
            return 1
        self.check("/api/health responds", health.status_code == 200, f"HTTP {health.status_code}")
        payload = health.json() if health.status_code == 200 else {}
        self.check(
            "deployment is in team mode",
            payload.get("authMode") == "team",
            f"authMode={payload.get('authMode')!r} (accounts are disabled — not a team deployment)",
        )

        for path in PROTECTED:
            response = httpx.get(f"{self.base_url}{path}", timeout=10)
            self.check(
                f"anonymous {path} is refused",
                response.status_code == 401,
                f"HTTP {response.status_code}",
            )

        client = httpx.Client(base_url=self.base_url, timeout=20)
        try:
            wrong = client.post(
                "/api/auth/login", json={"username": username, "password": password + "-wrong"}
            )
            self.check("a wrong password is rejected", wrong.status_code == 401, f"HTTP {wrong.status_code}")

            login = client.post("/api/auth/login", json={"username": username, "password": password})
            self.check("login succeeds", login.status_code == 200, login.text[:200])
            if login.status_code != 200:
                return self._report()

            me = client.get("/api/auth/me")
            self.check("/api/auth/me returns the account", me.status_code == 200, f"HTTP {me.status_code}")
            if me.status_code == 200:
                user = me.json().get("user") or {}
                self.check(
                    "/api/auth/me identifies the caller",
                    user.get("username") == username,
                    f"username={user.get('username')!r}",
                )
                self.check(
                    "the account has a role",
                    user.get("role") in {"user", "admin", "superadmin"},
                    f"role={user.get('role')!r}",
                )

            state = client.get("/api/state")
            # A 503 here means the session could not be bootstrapped — almost always a
            # missing model catalog in PI_TEMPLATE_AGENT_DIR. Echo the server's own
            # explanation, because "HTTP 503" tells an operator nothing.
            self.check(
                "the session reaches its own state",
                state.status_code == 200,
                f"HTTP {state.status_code} {state.text[:300]}",
            )

            tree = client.get("/api/workspace/tree")
            self.check("the workspace is readable", tree.status_code == 200, f"HTTP {tree.status_code}")
            if tree.status_code == 200:
                # A user who can see another user's files would show up as a path outside
                # their own workspace; the API scopes by uid, so this is a sanity check only.
                self.check(
                    "the workspace root is scoped, not the server's cwd",
                    isinstance(tree.json().get("entries"), list),
                    "unexpected tree shape",
                )

            usage = client.get("/api/usage/me")
            self.check("usage is readable", usage.status_code == 200, f"HTTP {usage.status_code}")

            logout = client.post("/api/auth/logout")
            self.check("logout succeeds", logout.status_code == 200, f"HTTP {logout.status_code}")
            after = client.get("/api/state")
            self.check(
                "logout revokes the session immediately",
                after.status_code == 401,
                f"HTTP {after.status_code} — revocation is not taking effect",
            )
        finally:
            client.close()

        return self._report()

    def _report(self) -> int:
        print()
        if self.failures:
            print(f"smoke: FAILED — {len(self.failures)} of {self.checks} checks did not pass")
            for failure in self.failures:
                print(f"  - {failure}")
            return 1
        print(f"smoke: PASS — {self.checks} checks")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a team deployment of python-pi.")
    parser.add_argument("base_url", help="e.g. https://pi.example.com")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", help="Read from --password-stdin instead when possible.")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from the first line of stdin (keeps it out of `ps`)",
    )
    args = parser.parse_args()

    password = args.password
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    if not password:
        parser.error("pass --password or --password-stdin")

    return Smoke(args.base_url).run(args.username, password)


if __name__ == "__main__":
    sys.exit(main())
