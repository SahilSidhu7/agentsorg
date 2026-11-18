"""Cut a release: bump every version number, test, build the wheel, and (optionally) ship it.

    python scripts/release.py 0.8.6              # bump + test + build; nothing leaves this machine
    python scripts/release.py 0.8.6 --deploy     # ...then upload the site (wheel, installers, latest.json)
    python scripts/release.py 0.8.6 --push       # ...then commit "Release 0.8.6", tag v0.8.6, push
    python scripts/release.py 0.8.6 --publish    # both
    python scripts/release.py 0.8.6 --dry-run    # show what would change

Run it with the project's venv Python (it runs pytest and pip from the same interpreter).
"""
import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = "https://agentsorg.sahilsidhu.pro"

# file -> pattern whose group 1 is the version
FILES = {
    "agentsorg/__init__.py": r'__version__ = "([^"]+)"',
    "pyproject.toml": r'(?m)^version = "([^"]+)"',
    "install.sh": r'VERSION="([^"]+)"',
    "site/install.sh": r'VERSION="([^"]+)"',
    "install.ps1": r'\$version = "([^"]+)"',
    "site/install.ps1": r'\$version = "([^"]+)"',
    "site/latest.json": r'"version": "([^"]+)"',
}


def parse(v):
    if not re.fullmatch(r"\d+(\.\d+)*", v):
        sys.exit(f"not a version: {v!r} (want e.g. 0.8.6)")
    return tuple(int(x) for x in v.split("."))


def run(*cmd, **kw):
    print("  $", " ".join(map(str, cmd)))
    r = subprocess.run(cmd, cwd=ROOT, **kw)
    if r.returncode:
        sys.exit(f"failed ({r.returncode}): {' '.join(map(str, cmd))}")
    return r


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser(description="Release agents.org")
    ap.add_argument("version")
    ap.add_argument("--deploy", action="store_true", help="upload the site with wrangler")
    ap.add_argument("--push", action="store_true", help="commit, tag and push to GitHub")
    ap.add_argument("--publish", action="store_true", help="--deploy and --push")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args()
    deploy, push = a.deploy or a.publish, a.push or a.publish
    new = a.version.lstrip("v")

    # 1. every file agrees on the current version, and the new one is newer
    texts, found = {}, {}
    for f, pat in FILES.items():
        texts[f] = (ROOT / f).read_text(encoding="utf-8")
        m = re.search(pat, texts[f])
        if not m:
            sys.exit(f"no version found in {f}")
        found[f] = m.group(1)
    if len(set(found.values())) > 1:
        sys.exit("version numbers disagree:\n" + "\n".join(f"  {f}: {v}" for f, v in found.items()))
    cur = next(iter(found.values()))
    whl = f"agentsorg-{new}-py3-none-any.whl"
    # already built (e.g. an earlier run without --deploy/--push): just ship it
    ship_only = parse(new) == parse(cur) and (deploy or push) and (ROOT / "site" / whl).exists()
    if parse(new) <= parse(cur) and not ship_only:
        sys.exit(f"{new} is not newer than the current {cur}")
    if push and git("rev-parse", "-q", "--verify", f"refs/tags/v{new}"):
        sys.exit(f"tag v{new} already exists")
    print(f"agents.org {new} (already built, shipping)" if ship_only else f"agents.org {cur} -> {new}")

    if a.dry_run and not ship_only:
        for f in FILES:
            print(f"  would set {f}")
        print(f"  would build agentsorg-{new}-py3-none-any.whl and copy it to site/")
        if deploy:
            print("  would deploy the site")
        if push:
            print(f"  would commit, tag v{new} and push:\n" + (git("status", "--short") or "    (only the version files)"))
        return

    if a.dry_run:
        print("  would " + " and ".join(x for x, on in (("deploy the site", deploy), (f"commit, tag v{new} and push", push)) if on))
        return
    if ship_only:
        return ship(new, deploy, push)

    # 2. tests on the code as it is (the version number doesn't affect them)
    if not a.skip_tests:
        print("Running tests")
        run(sys.executable, "-m", "pytest", "-q")

    # 3. bump; put everything back if the build fails
    print("Setting version")
    for f, pat in FILES.items():
        m = re.search(pat, texts[f])
        (ROOT / f).write_text(texts[f][:m.start(1)] + new + texts[f][m.end(1):], encoding="utf-8", newline="")
    try:
        print("Building wheel")
        shutil.rmtree(ROOT / "build", ignore_errors=True)
        run(sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w", "dist", ".")
        if not (ROOT / "dist" / whl).exists():
            sys.exit(f"build did not produce dist/{whl}")
        shutil.copy2(ROOT / "dist" / whl, ROOT / "site" / whl)
    except BaseException:
        for f in FILES:
            (ROOT / f).write_text(texts[f], encoding="utf-8", newline="")
        print(f"Build failed; version numbers put back to {cur}.")
        raise
    print(f"  site/{whl}")
    ship(new, deploy, push)


def ship(new, deploy, push):
    if deploy:
        print("Deploying site")
        npx = shutil.which("npx") or sys.exit("npx not found (needed for wrangler)")
        run(npx, "--yes", "wrangler", "deploy")
        try:
            import httpx
            live = httpx.get(f"{SITE}/latest.json", timeout=15).json().get("version")
            print(f"  {SITE}/latest.json says {live}" + ("" if live == new else "  (not updated yet?)"))
        except Exception as e:
            print(f"  couldn't check latest.json: {e}")
    if push:
        print("Committing")
        run("git", "add", "-A")
        if git("status", "--porcelain"):
            run("git", "commit", "-q", "-m", f"Release {new}")
        run("git", "tag", f"v{new}")
        run("git", "push", "-q", "origin", "HEAD", f"v{new}")

    print(f"\nagents.org {new} is ready." + ("" if deploy else "  Site not deployed: run with --deploy.")
          + ("" if push else "  Not committed: run with --push."))


if __name__ == "__main__":
    main()
