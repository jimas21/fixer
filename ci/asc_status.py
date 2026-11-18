"""Print what App Store Connect says about the newest builds: processing state, TestFlight state, the internal group.

Same environment as asc_setup.py. Read-only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from asc_setup import call, find_app  # noqa: E402


def main() -> int:
    app_id = find_app(os.environ["BUNDLE_ID"])
    if not app_id:
        print("No app record for this bundle id.")
        return 1
    app = call("GET", f"/apps/{app_id}")["data"]["attributes"]
    print(f"app {app_id}: {app.get('name')} | bundle {app.get('bundleId')}")
    for g in call("GET", f"/apps/{app_id}/betaGroups", query={"limit": "50"}).get("data", []):
        print(f"group {g['id']}: {g['attributes']}")
    res = call("GET", "/builds", query={"filter[app]": app_id, "sort": "-uploadedDate", "limit": "5",
                                        "include": "buildBetaDetail,preReleaseVersion"})
    details = {x["id"]: x["attributes"] for x in res.get("included", []) if x["type"] == "buildBetaDetails"}
    versions = {x["id"]: x["attributes"].get("version") for x in res.get("included", [])
                if x["type"] == "preReleaseVersions"}
    print("build | app version | uploaded | processing | expired | internal TestFlight | external")
    for b in res.get("data", []):
        a = b["attributes"]
        rel = b.get("relationships", {})
        d = details.get((rel.get("buildBetaDetail", {}).get("data") or {}).get("id"), {})
        v = versions.get((rel.get("preReleaseVersion", {}).get("data") or {}).get("id"), "?")
        print(f"{a.get('version')} | {v} | {a.get('uploadedDate')} | {a.get('processingState')} | {a.get('expired')} | "
              f"{d.get('internalBuildState')} | {d.get('externalBuildState')}")
    for g in call("GET", f"/apps/{app_id}/betaGroups", query={"limit": "50"}).get("data", []):
        ga = g["attributes"]
        testers = call("GET", f"/betaGroups/{g['id']}/betaTesters", query={"limit": "50"}).get("data", [])
        print(f"group internal={ga.get('isInternalGroup')} allBuilds={ga.get('hasAccessToAllBuilds')} "
              f"testers={len(testers)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
