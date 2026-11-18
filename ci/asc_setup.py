"""(Ported from jimas21/pokerogue-ios.) Gets the Apple side ready before a build, using the App Store Connect API key. Safe to run every time.

1. Registers the bundle ID (Certificates, Identifiers & Profiles) if it is not there yet.
2. Checks that the App Store Connect app record exists. Apple's API cannot create one, so if it is missing this
   stops with the exact steps to make it (about a minute in App Store Connect), before any long build starts.
3. Makes sure the app has an internal TestFlight group that receives every build automatically, and that the
   team's users are in it. Problems here are reported as warnings and never stop the build.

Logs are public: this prints no emails, names or identifiers beyond what GitHub masks.

Environment: ASC_KEY_PATH, ASC_KEY_ID, ASC_ISSUER_ID, BUNDLE_ID. Needs PyJWT and cryptography.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt

API = "https://api.appstoreconnect.apple.com/v1"
GROUP_NAME = "Me"


def token() -> str:
    with open(os.environ["ASC_KEY_PATH"], encoding="utf-8") as f:
        key = f.read()
    now = int(time.time())
    return jwt.encode(
        {"iss": os.environ["ASC_ISSUER_ID"], "iat": now, "exp": now + 15 * 60, "aud": "appstoreconnect-v1"},
        key,
        algorithm="ES256",
        headers={"kid": os.environ["ASC_KEY_ID"], "typ": "JWT"},
    )


def call(method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
    url = API + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    # App Store Connect answers 500/502/503/429 now and then ("An unexpected error occurred", 11-17): try again a
    # few times before failing the whole release.
    last = None
    for wait in (10, 30, 60, None):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + token())
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "fixer-ci")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            last = err
            if err.code not in (429, 500, 502, 503, 504) or wait is None:
                break
            print(f"{method} {path} -> HTTP {err.code}; Apple's side, retrying in {wait} s", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError) as err:
            if wait is None:
                raise
            print(f"{method} {path} -> {getattr(err, 'reason', err)}; retrying in {wait} s", file=sys.stderr)
        time.sleep(wait)
    detail = last.read().decode(errors="replace")
    try:
        errors = json.loads(detail).get("errors", [])
        detail = "; ".join(f"{x.get('title')}: {x.get('detail')}" for x in errors) or detail
    except ValueError:
        pass
    raise RuntimeError(f"{method} {path} -> HTTP {last.code}: {detail}") from None


def ensure_bundle_id(identifier: str) -> None:
    found = call("GET", "/bundleIds", query={"filter[identifier]": identifier, "limit": "200"})
    if any(b["attributes"]["identifier"] == identifier for b in found.get("data", [])):
        print("Bundle ID: registered.")
        return
    call("POST", "/bundleIds", {
        "data": {"type": "bundleIds",
                 "attributes": {"identifier": identifier, "name": "Fixer", "platform": "IOS"}}})
    print("Bundle ID: was missing, registered it now.")


def find_app(identifier: str) -> str | None:
    apps = call("GET", "/apps", query={"filter[bundleId]": identifier, "limit": "200"})
    for app in apps.get("data", []):
        if app["attributes"].get("bundleId") == identifier:
            return app["id"]
    return None


def ensure_internal_group(app_id: str) -> None:
    groups = call("GET", f"/apps/{app_id}/betaGroups", query={"limit": "200"}).get("data", [])
    internal = [g for g in groups if g["attributes"].get("isInternalGroup")]
    if internal:
        print(f"TestFlight: internal group present ({len(internal)}).")
        return
    group = call("POST", "/betaGroups", {
        "data": {"type": "betaGroups",
                 "attributes": {"name": GROUP_NAME, "isInternalGroup": True, "hasAccessToAllBuilds": True},
                 "relationships": {"app": {"data": {"type": "apps", "id": app_id}}}}})["data"]
    print("TestFlight: created an internal group that gets every build.")

    users = call("GET", "/users", query={"limit": "200"}).get("data", [])
    added = 0
    for user in users:
        a = user["attributes"]
        email = a.get("username") or a.get("email")
        if not email:
            continue
        try:
            call("POST", "/betaTesters", {
                "data": {"type": "betaTesters",
                         "attributes": {"email": email, "firstName": a.get("firstName") or "",
                                        "lastName": a.get("lastName") or ""},
                         "relationships": {"betaGroups": {"data": [{"type": "betaGroups", "id": group["id"]}]}}}})
            added += 1
        except RuntimeError as e:
            print(f"::warning::Could not add one team member to the TestFlight group ({str(e)[:200]}).")
    print(f"TestFlight: added {added} of {len(users)} team member(s) to the group.")


def main() -> int:
    identifier = os.environ["BUNDLE_ID"]

    def apple_down(e: RuntimeError) -> bool:
        return " -> HTTP 5" in str(e)

    # This is one-time setup (done 2026-11-16). When Apple's API answers with server errors even after the retries
    # (11-17: GET /bundleIds -> 500 for minutes), carry on: the bundle ID and app record exist, and the upload step
    # has its own retries.
    try:
        ensure_bundle_id(identifier)
        app_id = find_app(identifier)
    except RuntimeError as e:
        if not apple_down(e):
            raise
        print(f"::warning::App Store Connect is answering with server errors ({str(e)[:200]}); skipping the "
              "one-time setup checks and going on to the build.")
        return 0
    if not app_id:
        print("::error::There is no App Store Connect app for this bundle ID yet. Apple's API cannot create one.")
        print("Make it once (about a minute), then run this workflow again:")
        print("  1. appstoreconnect.apple.com > Apps > + > New App")
        print("  2. Platform iOS. Name: anything not taken on the App Store, e.g. 'Fixer by the owner'.")
        print("  3. Bundle ID: pick the Fixer one this step just registered. SKU: fixer. Full Access.")
        return 3
    print("App record: present.")

    try:
        ensure_internal_group(app_id)
    except RuntimeError as e:
        print(f"::warning::TestFlight group setup did not finish ({str(e)[:300]}). Add yourself under the app's "
              "TestFlight > Internal Testing in App Store Connect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
