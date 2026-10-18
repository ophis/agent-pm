import argparse
import base64
import hashlib
import io
import json
import os
import re
import tarfile
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

VERSION = "0.163.0"
SOURCE = "83086188ae0614bd882818ff1aa23f3194962012"
PLATFORMS = {
    "darwin-arm64": "aarch64-apple-darwin",
    "linux-arm64": "aarch64-unknown-linux-musl",
    "linux-x64": "x86_64-unknown-linux-musl",
}


def registry_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "registry.npmjs.org":
        raise ValueError("registry_origin")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("registry_redirect")


def fetch(url):
    registry_url(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=60) as response:
        registry_url(response.url)
        return response.read(200 * 1024 * 1024)


def verify_integrity(data, integrity):
    expected = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if integrity != expected:
        raise ValueError("integrity_mismatch")


def native_bytes(data, platform):
    expected = f"package/vendor/{PLATFORMS[platform]}/bin/codex"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        entries = [entry for entry in archive.getmembers() if entry.name == expected]
        if len(entries) != 1 or not entries[0].isfile():
            raise ValueError("native_entry")
        file = archive.extractfile(entries[0])
        if file is None:
            raise ValueError("native_missing")
        return file.read()


def inventory():
    metadata = json.loads(fetch("https://registry.npmjs.org/@openai%2fcodex"))
    stable = [v for v in metadata["versions"] if re.fullmatch(r"0\.163\.\d+", v)]
    return {
        "date": datetime.now(UTC).isoformat(),
        "status": "source_only",
        "fixed_candidate": VERSION,
        "source_commit": SOURCE,
        "latest_stable": metadata["dist-tags"]["latest"],
        "latest_same_minor": max(stable, key=lambda v: tuple(map(int, v.split(".")))),
        "published": metadata["time"][VERSION],
        "allowed_versions": [],
        "refresh_lead_seconds": None,
        "live_refresh_count": 0,
    }


def download(platform, destination):
    version = f"{VERSION}-{platform}"
    metadata = json.loads(fetch(f"https://registry.npmjs.org/@openai%2fcodex/{version}"))
    data = fetch(metadata["dist"]["tarball"])
    verify_integrity(data, metadata["dist"]["integrity"])
    binary = native_bytes(data, platform)
    with destination.open("xb") as file:
        os.fchmod(file.fileno(), 0o700)
        file.write(binary)
    return {"version": VERSION, "status": "download_verified", "count": 1}


def main():
    parser = argparse.ArgumentParser(description="TASK-647 public metadata/native download only")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inventory")
    native = commands.add_parser("native")
    native.add_argument("--platform", choices=PLATFORMS, required=True)
    native.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = (
            inventory()
            if args.command == "inventory"
            else download(args.platform, args.destination)
        )
    except (OSError, ValueError, KeyError, tarfile.TarError):
        print(json.dumps({"status": "failed", "error": "metadata_or_download"}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
