#!/usr/bin/env python3
"""Fetch the latest HoloPatcher and OdyPatcher for one job, fresh every run.

  patchers.py fetch --dest DIR --out FILE [--odypatcher-only | --holopatcher-only]

OdyPatcher: the linux-x86_64 asset of https://github.com/kolto-labs/odypatcher/releases/latest,
checked against that release's SHA256SUMS.txt before it is unpacked. The repository is private, so a
token that can read it is required: ODYPATCHER_TOKEN, GITHUB_TOKEN or GH_TOKEN, else `gh auth token`.

HoloPatcher: the newest `holopatcher` on PyPI, installed into its own venv under DIR so it never
shares the pykotor pin manual.py installs. Its GitHub releases stopped shipping binaries at
v1.60-patcher-beta4 (2024) and the HoloPatcher repository is gone; the PyPI package is the newest
build there is and has the same unattended CLI (--install --game-dir --tslpatchdata
--namespace-option-index --console). pykotor imports tkinter even in CLI mode, so the venv is made
from --python (default: this interpreter), which needs Tk (python3-tk on Ubuntu).

FILE gets {"holopatcher": {"path", "version", "pykotor"}, "odypatcher": {"path", "version", "asset",
"sha256"}}: the executables and exactly which versions this run installed with.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request

ODYPATCHER_REPOSITORY = "kolto-labs/odypatcher"
ODYPATCHER_ASSET = "odypatcher-linux-x86_64.tar.gz"
ODYPATCHER_SUMS = "SHA256SUMS.txt"
HOLOPATCHER_PACKAGE = "holopatcher"
ATTEMPTS = 3


TOKEN_VARIABLES = ("ODYPATCHER_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


def github_token():
    for name in TOKEN_VARIABLES:
        if os.environ.get(name):
            return os.environ[name]
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


class SameHostAuthorization(urllib.request.HTTPRedirectHandler):
    """Private release assets redirect to a signed storage URL that must not see the GitHub token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


OPENER = urllib.request.build_opener(SameHostAuthorization)


def request(url, accept=None, token=None):
    headers = {"User-Agent": "odyssey-ci-patchers"}
    if accept:
        headers["Accept"] = accept
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = "Bearer " + token
    error = None
    for _ in range(ATTEMPTS):
        try:
            with OPENER.open(urllib.request.Request(url, headers=headers), timeout=120) as response:
                return response.read()
        except OSError as caught:
            error = caught
    raise SystemExit("patchers: cannot fetch %s: %s" % (url, error))


def sums_for(text, name):
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*").rsplit("/", 1)[-1] == name:
            return parts[0].lower()
    raise SystemExit("patchers: %s lists no checksum for %s" % (ODYPATCHER_SUMS, name))


def executable_in(root, name):
    found = [path for path in Path(root).rglob("*") if path.is_file() and path.name == name]
    if len(found) != 1:
        raise SystemExit("patchers: expected one %s in the %s asset, found %d" % (name, ODYPATCHER_ASSET, len(found)))
    found[0].chmod(found[0].stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return found[0]


def fetch_odypatcher(dest):
    token = github_token()
    if not token:
        raise SystemExit("patchers: %s is private; set ODYPATCHER_TOKEN or log in with gh" % ODYPATCHER_REPOSITORY)
    release = json.loads(request("https://api.github.com/repos/%s/releases/latest" % ODYPATCHER_REPOSITORY,
                                 "application/vnd.github+json", token))
    # The API asset URL, not browser_download_url: only the API serves private assets to a token.
    assets = {asset["name"]: asset["url"] for asset in release.get("assets", [])}
    for name in (ODYPATCHER_ASSET, ODYPATCHER_SUMS):
        if name not in assets:
            raise SystemExit("patchers: OdyPatcher %s has no %s asset" % (release.get("tag_name"), name))
    archive = request(assets[ODYPATCHER_ASSET], "application/octet-stream", token)
    expected = sums_for(request(assets[ODYPATCHER_SUMS], "application/octet-stream", token)
                        .decode("utf-8", "replace"), ODYPATCHER_ASSET)
    actual = hashlib.sha256(archive).hexdigest()
    if actual != expected:
        raise SystemExit("patchers: %s sha256 %s does not match %s (%s)" % (ODYPATCHER_ASSET, actual,
                                                                         ODYPATCHER_SUMS, expected))
    root = Path(dest) / "odypatcher"
    root.mkdir(parents=True, exist_ok=True)
    tarball = root / ODYPATCHER_ASSET
    tarball.write_bytes(archive)
    with tarfile.open(tarball) as stream:
        stream.extractall(root / "bin", filter="data")
    tarball.unlink()
    path = executable_in(root / "bin", "odypatcher")
    return {"path": str(path), "version": release["tag_name"], "asset": ODYPATCHER_ASSET, "sha256": actual}


def fetch_holopatcher(dest, python_for_venv):
    if subprocess.run([python_for_venv, "-c", "import tkinter"], capture_output=True).returncode:
        raise SystemExit("patchers: %s has no tkinter, which pykotor needs; install python3-tk or pass --python"
                         % python_for_venv)
    venv = Path(dest) / "holopatcher-venv"
    # pip may run build code from PyPI; it gets no GitHub token.
    env = {name: value for name, value in os.environ.items() if name not in TOKEN_VARIABLES}
    subprocess.run([python_for_venv, "-m", "venv", str(venv)], check=True, env=env)
    python = venv / "bin" / "python"
    # --upgrade with no pin: the newest holopatcher and the newest pykotor it accepts, every run.
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--upgrade",
                    HOLOPATCHER_PACKAGE], check=True, env=env)
    versions = json.loads(subprocess.run(
        [str(python), "-c", "import importlib.metadata as m, json; "
                            "print(json.dumps({n: m.version(n) for n in ('holopatcher', 'pykotor')}))"],
        check=True, capture_output=True, text=True).stdout)
    path = venv / "bin" / "holopatcher"
    if not path.is_file():
        raise SystemExit("patchers: the holopatcher package installed no holopatcher command")
    return {"path": str(path), "version": versions["holopatcher"], "pykotor": versions["pykotor"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("--dest", required=True)
    fetch.add_argument("--out", required=True)
    fetch.add_argument("--python", default=sys.executable, help="interpreter for the HoloPatcher venv (needs Tk)")
    only = fetch.add_mutually_exclusive_group()
    only.add_argument("--odypatcher-only", action="store_true")
    only.add_argument("--holopatcher-only", action="store_true")
    args = parser.parse_args(argv)
    found = {}
    if not args.odypatcher_only:
        found["holopatcher"] = fetch_holopatcher(args.dest, args.python)
    if not args.holopatcher_only:
        found["odypatcher"] = fetch_odypatcher(args.dest)
    Path(args.out).write_text(json.dumps(found, indent=2) + "\n", encoding="utf-8")
    for name, value in found.items():
        print("patchers: %s %s at %s" % (name, value["version"], value["path"]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
