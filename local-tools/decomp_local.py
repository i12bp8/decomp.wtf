#!/usr/bin/env python3
"""Talk to the running local decomp.wtf instance without printing its session key."""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / "decomp-project/.decomp/server.json"
BASE = "http://127.0.0.1:6464"
SLUG = "yu-gi-oh-ddm"


def request(path, *, data=None, query=None):
    secret = json.loads(SESSION.read_text())["secret"]
    url = BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Cookie": f"decomp_live={secret}"}
    if data is not None:
        headers.update({"Content-Type": "application/json", "X-Decomp": "1"})
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None, headers=headers
    )
    with urllib.request.urlopen(req, timeout=120) as res:
        return json.load(res)


def follow_job(job_id):
    """Wait for a background job and return its report, never repeat its request."""
    print(f"Following job {job_id}", file=sys.stderr)
    while True:
        job = request("/api/job", query={"id": job_id})
        if job["state"] == "done":
            return job["result"]
        if job["state"] == "error":
            raise RuntimeError(job.get("error") or "Rescan job failed")
        time.sleep(0.5)


def main():
    global BASE, SLUG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=SLUG, help="Project slug (default: yu-gi-oh-ddm)")
    parser.add_argument("--port", type=int, default=6464, help="Local decomp.wtf port")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="List open functions, smallest first")
    listing.add_argument("--limit", type=int, default=25)
    listing.add_argument("--min-insns", type=int, default=1)
    listing.add_argument("--max-insns", type=int, default=20)
    listing.add_argument("--max-vram", type=lambda s: int(s, 0), default=0x08100000)
    detail = sub.add_parser("detail", help="Print function assembly and current draft")
    detail.add_argument("name")
    trial = sub.add_parser("try", help="Compile a C source file and compare with the ROM")
    trial.add_argument("name")
    trial.add_argument("source", type=Path)
    diff = sub.add_parser("diff", help="Show latest instruction diff")
    diff.add_argument("name")
    sub.add_parser("stop-autopilot", help="Stop background matching for this project")
    sub.add_parser("status", help="Show project and autopilot status")
    imp = sub.add_parser("import", help="Import a local game file (confirms you own it); prints the job id to poll")
    imp.add_argument("path", type=Path, help="Absolute path to your copy of the game")
    imp.add_argument("--toolchain", default=None, help="Compiler to select (optional)")
    sub.add_parser("job", help="Show a job by id").add_argument("id")
    rescan = sub.add_parser("rescan", help="Preview GBA function changes from edited functions.cfg; waits for the report")
    rescan.add_argument("--apply", action="store_true", help="Apply reviewed changes; requires the preview revision")
    rescan.add_argument("--revision", help="Exact revision from the reviewed preview (required with --apply)")
    args = parser.parse_args()
    if args.command == "rescan":
        if args.apply and not args.revision:
            parser.error("rescan --apply requires --revision from a reviewed preview")
        if args.revision and not args.apply:
            parser.error("rescan --revision is only used with --apply")
    SLUG = args.project
    BASE = f"http://127.0.0.1:{args.port}"

    if args.command == "list":
        data = request("/api/map", query={"slug": SLUG})
        rows = [dict(zip(data["keys"], row)) for row in data["rows"]]
        rows = [r for r in rows if r["status"] == "open" and args.min_insns <= r["insns"] <= args.max_insns
                and r["vram"] < args.max_vram]
        rows.sort(key=lambda r: (r["insns"], -(r["best"] is not None), r["vram"]))
        for r in rows[: args.limit]:
            print(f"{r['name']}  {r['insns']:>3} insns  {r['attempts']:>3} tries  best={r['best']}")
        print(f"{len(rows)} open functions matching filters", file=sys.stderr)
    elif args.command == "detail":
        print(json.dumps(request("/api/function", query={"slug": SLUG, "name": args.name}), indent=2))
    elif args.command == "try":
        source = args.source.read_text()
        print(json.dumps(request("/api/try", data={"slug": SLUG, "name": args.name, "source": source}), indent=2))
    elif args.command == "diff":
        print(json.dumps(request("/api/diff", query={"slug": SLUG, "name": args.name}), indent=2))
    elif args.command == "stop-autopilot":
        result = request("/api/autopilot", data={"slug": SLUG, "action": "stop"})
        print(json.dumps(result.get("autopilot", {}), indent=2))
    elif args.command == "status":
        result = request("/api/project", query={"slug": SLUG})
        print(json.dumps({k: result.get(k) for k in ("slug", "functions", "matched", "autopilot")}, indent=2))
    elif args.command == "import":
        query = {"path": str(args.path.resolve()), "confirmed": "1"}
        if args.toolchain:
            query["toolchain"] = args.toolchain
        started = request("/api/import", data={}, query=query)
        print(json.dumps(started, indent=2))
        print(json.dumps(request("/api/job", query={"id": started["job"]}), indent=2))
    elif args.command == "job":
        print(json.dumps(request("/api/job", query={"id": args.id}), indent=2))
    elif args.command == "rescan":
        data = {"slug": SLUG, "apply": args.apply}
        if args.apply:
            data["revision"] = args.revision
        try:
            started = request("/api/rescan", data=data)
            print(json.dumps(follow_job(started["job"]), indent=2))
        except urllib.error.HTTPError as exc:
            try:
                message = json.load(exc).get("error") or str(exc)
            except (ValueError, AttributeError):
                message = str(exc)
            parser.exit(1, f"Rescan failed: {message}\n")
        except (OSError, RuntimeError, ValueError) as exc:
            parser.exit(1, f"Rescan failed: {exc}\n")


if __name__ == "__main__":
    main()
