#!/usr/bin/env python3
"""Daily security watch for a published image.

Opens one GitHub issue per finding and never builds, tags or releases
anything. Three checks:

1. Trivy report (HIGH/CRITICAL with a fix), read from TRIVY_JSON.
2. Distribution updates: security updates (Debian) or updates of the big
   packages (Alpine) that the distribution already ships but the image
   doesn't have yet.
3. Upstream: a security release of nginx, Node.js, PHP, Python, OpenSSL,
   curl or Git that is newer than what the image has, before the
   distribution ships it. Alpine images only: Debian backports fixes without
   changing the version, so comparing versions would raise false alarms.

4. Release pull request: when the distribution already ships a fix (checks
   1 and 2), a pull request `security-release` prepares the next patch
   release (changelog entry, version files). Merging it is the approval:
   .github/workflows/security-release.yml then tags and publishes. Needs
   RELEASE_TOKEN, a token that can push and open pull requests, because
   what GITHUB_TOKEN pushes or opens starts no other workflow (no tests on
   the pull request, no build for the tag).

An issue is opened once per finding: if an issue with the same title exists,
open or closed, nothing happens. Closing an issue without fixing it
therefore silences that finding for good.

A source that can't be reached only gives a warning. After three runs in a
row without it, an issue says so. The count is kept in STATE_DIR, which the
workflow carries from one run to the next as an artifact.

Environment: IMAGE (required), TRIVY_JSON, STATE_DIR, GH_TOKEN,
GITHUB_REPOSITORY, DRY_RUN=1 (print the issues instead of opening them);
for the release pull request RELEASE_TOKEN, CHANGELOG (default
CHANGELOG.md), VERSION_FILES (space separated), TAG_PREFIX (default "v").
`security_watch.py release` (run after the merge) tags and publishes.
Standard library only.
"""

import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

IMAGE = os.environ.get("IMAGE", "")
TRIVY_JSON = os.environ.get("TRIVY_JSON", "")
STATE_DIR = os.environ.get("STATE_DIR", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
LABEL = "security"
FAILURE_RUNS = 3
CVE_RE = re.compile(r"CVE-\d{4}-\d+")

# Alpine packages that count as "big": their updates are reported even when
# Trivy doesn't know a CVE for them yet. Debian marks security updates, so
# there every security update counts.
ALPINE_TRACKED = re.compile(
    r"^(nginx|nodejs(-current)?|php\d+|python3|openssl|libssl\d+|libcurl|"
    r"git|sqlite-libs|ffmpeg-libavcodec|ghostscript|busybox|musl)$"
)

failed_sources = set()
checked_sources = set()


def warn(msg):
    print(f"::warning::{msg}")


def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": "security-watch"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            return data if binary else data.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - any failure counts
            err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{url}: {err}")


def source(name):
    """Decorator: run a check, record the source as failed if it raises."""

    def wrap(fn):
        def run(*args):
            checked_sources.add(name)
            try:
                return fn(*args) or []
            except Exception as e:  # noqa: BLE001
                failed_sources.add(name)
                warn(f"{name} not reachable or not readable: {e}")
                return []

        return run

    return wrap


# --- versions ---------------------------------------------------------------


def vt(v):
    """'1.30.4-r1' -> (1, 30, 4); 'v24.22.1' -> (24, 22, 1)."""
    v = v.split("-r")[0].lstrip("v")
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def apk_key(v):
    """Alpine version with its -rN release, for comparing secfixes."""
    m = re.match(r"(.*?)(?:-r(\d+))?$", v)
    return vt(m.group(1)), int(m.group(2) or 0)


def branch(v, parts=2):
    return vt(v)[:parts]


# --- the image ---------------------------------------------------------------

INVENTORY = r"""
cat /etc/os-release
echo ---PKG
if command -v apk >/dev/null; then apk info -v 2>/dev/null
else dpkg-query -W -f '${Package} ${Version}\n' 2>/dev/null; fi
echo ---NODE
node -v 2>/dev/null
echo ---PY
python3 -c 'import platform; print(platform.python_version())' 2>/dev/null
echo ---UPGRADES
if command -v apk >/dev/null; then
  apk update -q >/dev/null 2>&1 && apk list -u 2>/dev/null
elif command -v apt-get >/dev/null; then
  apt-get update -qq >/dev/null 2>&1 && apt list --upgradable 2>/dev/null
fi
"""


def inventory():
    out = subprocess.run(
        ["docker", "run", "--rm", "--user", "0", "--entrypoint", "sh", IMAGE,
         "-c", INVENTORY],
        capture_output=True, text=True, check=True, timeout=600,
    ).stdout
    parts = dict(re.findall(r"(?ms)^---(\w+)\n(.*?)(?=^---\w+\n|\Z)",
                            "---OS\n" + out))
    os_release = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', parts["OS"], re.M))
    pkgs = {}
    for line in parts["PKG"].splitlines():
        m = re.match(r"^(.+)-(\d[^-]*-r\d+)$", line)  # apk: name-1.2.3-r0
        if m:
            pkgs[m.group(1)] = m.group(2)
        elif " " in line:  # dpkg: name version
            name, ver = line.split(" ", 1)
            pkgs[name] = ver
    return {
        "distro": os_release.get("ID", ""),
        "distro_version": os_release.get("VERSION_ID", ""),
        "pkgs": pkgs,
        "node": parts["NODE"].strip(),
        "python": parts["PY"].strip(),
        "upgrades": parts["UPGRADES"],
    }


# --- check 1: Trivy ------------------------------------------------------------


def choose_fix(fixed, installed):
    """'1.26.10, 1.27.3' for v1.26.7 -> '1.26.10' (same line first)."""
    opts = [o.strip() for o in fixed.split(",") if o.strip()]
    v = vt(installed)
    same = [o for o in opts if vt(o)[:2] == v[:2]]
    newer = [o for o in opts if vt(o) > v]
    return min(same or newer or opts, key=vt)


def cve_line(v):
    return (f"- [{v['VulnerabilityID']}]({v.get('PrimaryURL') or ''}) "
            f"({v.get('Severity', '?')}): `{v['PkgName']}` "
            f"{v['InstalledVersion']} -> {v['FixedVersion']}. "
            f"{(v.get('Title') or '').strip()}")


def newest(ids):
    return max(ids, key=lambda c: tuple(int(x) for x in re.findall(r"\d+", c)) or (0,))


def trivy_findings(inv):
    checked_sources.add("Trivy")
    if not TRIVY_JSON or not os.path.exists(TRIVY_JSON):
        failed_sources.add("Trivy")
        warn("no Trivy report")
        return []
    distro = inv["distro"].capitalize() or "the distribution"
    out = []
    for result in json.load(open(TRIVY_JSON)).get("Results") or []:
        vulns = [v for v in result.get("Vulnerabilities") or [] if v.get("FixedVersion")]
        if not vulns:
            continue
        if result.get("Class") == "os-pkgs":
            # One issue per distribution package, named after its fix.
            groups = {}
            for v in vulns:
                groups.setdefault((v["PkgName"], v["InstalledVersion"]), []).append(v)
            for (pkg, installed), vs in sorted(groups.items()):
                fix = max((choose_fix(v["FixedVersion"], installed) for v in vs), key=apk_key)
                ids = sorted({v["VulnerabilityID"] for v in vs})
                out.append({
                    "release": f"`{pkg}` {fix} ({', '.join(ids)})",
                    "key": f"{pkg} {fix} available",
                    "title": f"Security: {pkg} {fix} available (image has {installed})",
                    "section": "Trivy (HIGH/CRITICAL with a fix)",
                    "text": "\n".join(cve_line(v) for v in vs),
                    "todo": f"{distro} already ships the fix: rebuild the image "
                            "and publish a patch release.",
                })
        else:
            # Libraries inside a program or language packages (Go binaries,
            # Python, npm): one issue per file, named after its newest CVE.
            target = result.get("Target", "?")
            ids = [v["VulnerabilityID"] for v in vulns]
            out.append({
                "key": f"{target} {newest(ids)}",
                "title": f"Security: fixes for {target} (newest {newest(ids)})",
                "section": f"Trivy: {target} ({result.get('Type', '')}), "
                           f"{len(vulns)} HIGH/CRITICAL with a fix",
                "text": "\n".join(cve_line(v) for v in vulns),
                "todo": "Update the dependency (requirements, lock file) or, if "
                        "the file comes with the base image, the base image; "
                        "then publish a patch release.",
            })
    return out


# --- check 2: distribution updates ----------------------------------------------


def upgrade_findings(inv):
    checked_sources.add("package index")
    out = []
    if inv["distro"] == "alpine":
        # nginx-1.30.6-r0 x86_64 {nginx} (BSD-2-Clause) [upgradable from: nginx-1.30.4-r1]
        rx = re.compile(r"^(.+)-(\d[^-]*-r\d+) \S+ \{(.+?)\}.*\[upgradable from: .+-(\d[^-]*-r\d+)\]")
        for line in inv["upgrades"].splitlines():
            m = rx.match(line)
            if m and ALPINE_TRACKED.match(m.group(1)):
                fixes = [c for ver, cves in secdb(inv).get(m.group(3), [])
                         if ver == apk_key(m.group(2)) for c in sorted(cves)]
                out.append((m.group(1), m.group(2), m.group(4), "Alpine", fixes))
    elif inv["distro"] == "debian":
        # curl/stable-security 8.14.1-2+deb13u8 amd64 [upgradable from: 8.14.1-2+deb13u7]
        rx = re.compile(r"^([^/]+)/(\S+) (\S+) \S+ \[upgradable from: (\S+)\]")
        for line in inv["upgrades"].splitlines():
            m = rx.match(line)
            if m and "security" in m.group(2):
                out.append((m.group(1), m.group(3), m.group(4), "Debian security", []))
    if not inv["upgrades"].strip() and inv["distro"] in ("alpine", "debian"):
        # Either nothing to upgrade or the index couldn't be fetched; apk and
        # apt print nothing in both cases. An up-to-date image is the normal
        # case, so this is not counted as a failure.
        pass
    return [{
        "key": f"{pkg} {new} available",
        "title": f"Security: {pkg} {new} available (image has {old})",
        "section": f"{origin} package update",
        "text": f"The {origin} repository has `{pkg}` {new}; the image has {old}."
                + (f" It fixes {', '.join(cves)}." if cves else ""),
        "todo": "Rebuild the image and publish a patch release.",
        "release": f"`{pkg}` {new}" + (f" ({', '.join(cves)})" if cves else ""),
    } for pkg, new, old, origin, cves in out]


# --- check 3: upstream ---------------------------------------------------------


def secdb(inv):
    """Alpine secfixes: package -> [(version key, {CVE, ...})]."""
    if inv.get("_secdb") is not None:
        return inv["_secdb"]
    rel = "v" + ".".join(inv["distro_version"].split(".")[:2])
    db = {}
    try:
        for repo in ("main", "community"):
            data = json.loads(fetch(f"https://secdb.alpinelinux.org/{rel}/{repo}.json"))
            for p in data["packages"]:
                for ver, fixes in p["pkg"].get("secfixes", {}).items():
                    cves = set(CVE_RE.findall(" ".join(fixes)))
                    db.setdefault(p["pkg"]["name"], []).append((apk_key(ver), cves))
    except Exception as e:  # noqa: BLE001
        failed_sources.add("Alpine secdb")
        warn(f"Alpine secdb not reachable: {e}")
    checked_sources.add("Alpine secdb")
    inv["_secdb"] = db
    return db


def backported(inv, origin, installed, cves):
    """CVEs the distribution already fixed in the installed version."""
    if not origin or not installed or "-r" not in installed:
        return set()
    have = apk_key(installed)
    return {c for ver, fixed in secdb(inv).get(origin, []) if ver <= have
            for c in fixed} & set(cves)


def upstream_finding(prog, fixed, installed, text, cves=(), origin=None, inv=None):
    if cves and inv is not None:
        open_cves = set(cves) - backported(inv, origin, installed, cves)
        if not open_cves:
            return None  # the distribution backported every fix
        text += ("\n\nAlready fixed by the distribution (backported): "
                 + ", ".join(sorted(set(cves) - open_cves))
                 if open_cves != set(cves) else "")
    return {
        "key": f"{prog} {fixed} released upstream",
        "title": f"Security: {prog} {fixed} released upstream "
                 f"(image has {installed})",
        "section": f"Upstream {prog}",
        "text": text,
        "todo": "The distribution doesn't ship the fix yet. Check whether the "
                "flaw applies to this container (feature used, reachable from "
                "outside); mitigate it if so. Rebuild once the distribution has "
                "the update (a separate issue follows then).",
    }


def pkg_version(inv, pattern):
    for name, ver in inv["pkgs"].items():
        if re.fullmatch(pattern, name):
            return name, ver
    return None, None


@source("nginx.org")
def check_nginx(inv, installed):
    page = fetch("https://nginx.org/en/security_advisories.html")
    v = vt(installed)
    hits, fixed_versions = [], []
    for item in re.findall(r"<li><p>(.*?)</p></li>", page, re.S):
        fields = [html.unescape(re.sub(r"<[^>]+>", "", f)).strip()
                  for f in item.split("<br>")]
        get = lambda k: next((f.split(":", 1)[1].strip() for f in fields
                              if f.startswith(k + ":")), "")
        severity = get("Severity").lower()
        if severity in ("", "low"):
            continue
        vulnerable = False
        for rng in get("Vulnerable").split(","):
            lo, _, hi = rng.strip().partition("-")
            if lo and vt(lo) <= v <= vt(hi or lo):
                vulnerable = True
        safe = [n.strip().rstrip("+") for n in get("Not vulnerable").split(",")
                if n.strip().endswith("+")]
        if any(vt(s)[:2] == v[:2] and v >= vt(s) for s in safe):
            vulnerable = False
        if not vulnerable:
            continue
        same = [s for s in safe if vt(s)[:2] == v[:2]]
        newer = sorted((s for s in safe if vt(s) > v), key=vt)
        fix = same[0] if same else (newer[0] if newer else "?")
        fixed_versions.append(fix)
        cves = list(dict.fromkeys(CVE_RE.findall(item)))
        hits.append((fields[0], severity, cves, fix))
    if not hits:
        return []
    fix = max(fixed_versions, key=lambda s: vt(s) if s != "?" else (0,))
    text = "\n".join(f"- {t} ({s}), {', '.join(c)}, fixed in {f}"
                     for t, s, c, f in hits)
    text += "\n\nSource: https://nginx.org/en/security_advisories.html"
    return [upstream_finding("nginx", fix, installed, text,
                             [c for h in hits for c in h[2]], "nginx", inv)]


@source("nodejs.org")
def check_node(inv, installed):
    releases = json.loads(fetch("https://nodejs.org/dist/index.json"))
    v = vt(installed)
    sec = [r for r in releases if r.get("security")
           and vt(r["version"])[:1] == v[:1] and vt(r["version"]) > v]
    if not sec:
        return []
    latest = max(sec, key=lambda r: vt(r["version"]))
    text = "\n".join(f"- Node.js {r['version']} ({r['date']}), security release"
                     for r in sorted(sec, key=lambda r: vt(r["version"])))
    text += "\n\nSource: https://nodejs.org/en/blog/vulnerability"
    # Node.js lists no CVEs in its index, so backports can't be checked:
    # Alpine updates nodejs to the new release instead of patching.
    return [upstream_finding("Node.js", latest["version"].lstrip("v"),
                             installed, text)]


@source("php.net")
def check_php(inv, installed):
    v = vt(installed)
    data = json.loads(fetch(
        f"https://www.php.net/releases/index.php?json&version={v[0]}.{v[1]}&max=50"))
    sec = [k for k, r in data.items()
           if "security" in (r.get("tags") or []) and vt(k) > v]
    if not sec:
        return []
    sec.sort(key=vt)
    text = "\n".join(f"- PHP {k} ({data[k].get('date', '')}), security release"
                     for k in sec)
    text += f"\n\nSource: https://www.php.net/ChangeLog-{v[0]}.php"
    return [upstream_finding("PHP", sec[-1], installed, text)]


@source("endoflife.date (Python)")
def check_python(inv, installed):
    data = json.loads(fetch("https://endoflife.date/api/v1/products/python/"))
    v = vt(installed)
    for rel in data["result"]["releases"]:
        if vt(rel["name"])[:2] == v[:2]:
            latest = (rel.get("latest") or {}).get("name", "")
            if latest and vt(latest) > v:
                text = (f"- Python {latest} ({rel['latest'].get('date', '')}) "
                        f"is the current {rel['name']} release.\n\n"
                        "Python bugfix and security releases both carry "
                        "security fixes; check the changelog: "
                        f"{rel['latest'].get('link', 'https://www.python.org/downloads/')}")
                return [upstream_finding("Python", latest, installed, text)]
    return []


@source("OpenSSL release metadata")
def check_openssl(inv, installed):
    v = vt(installed)
    hits = []
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "clone", "-q", "--depth", "1",
                        "https://github.com/openssl/release-metadata", d],
                       check=True, timeout=300)
        sec = os.path.join(d, "secjson")
        for name in os.listdir(sec):
            if not name.startswith("CVE-"):
                continue
            cna = json.load(open(os.path.join(sec, name)))["containers"]["cna"]
            sev = next((m.get("other", {}).get("content", {}).get("text", "")
                        for m in cna.get("metrics", [])), "").lower()
            if sev == "low":
                continue
            for aff in cna.get("affected", []):
                for r in aff.get("versions", []):
                    if r.get("status") != "affected" or "lessThan" not in r:
                        continue
                    if vt(r["version"]) <= v < vt(r["lessThan"]):
                        hits.append((name[:-5], sev or "?", r["lessThan"],
                                     cna.get("title", "")))
    if not hits:
        return []
    fix = max((h[2] for h in hits), key=vt)
    text = "\n".join(f"- {c} ({s}): {t}, fixed in {f}" for c, s, f, t in sorted(hits))
    text += "\n\nSource: https://openssl-library.org/news/vulnerabilities/"
    return [upstream_finding("OpenSSL", fix, installed, text,
                             [h[0] for h in hits], "openssl", inv)]


def osv_affected(events, v):
    """OSV SEMVER events: is v in an introduced..fixed range? -> fixed version."""
    intro = None
    for e in events:
        if "introduced" in e:
            intro = e["introduced"]
        elif "fixed" in e and intro is not None:
            if vt(intro or "0") <= v < vt(e["fixed"]):
                return e["fixed"]
            intro = None
        elif "last_affected" in e and intro is not None:
            if vt(intro or "0") <= v <= vt(e["last_affected"]):
                return "?"
            intro = None
    if intro is not None and vt(intro or "0") <= v:
        return "?"  # open range: no fix yet
    return None


@source("curl.se")
def check_curl(inv, installed):
    v = vt(installed)
    hits = []
    for e in json.loads(fetch("https://curl.se/docs/vuln.json")):
        sev = (e.get("database_specific") or {}).get("severity", "").lower()
        if sev == "low":
            continue
        for aff in e.get("affected", []):
            for r in aff.get("ranges", []):
                if r.get("type") == "SEMVER":
                    fix = osv_affected(r.get("events", []), v)
                    if fix:
                        cve = next(iter(e.get("aliases") or []), e["id"])
                        hits.append((cve, sev or "?", fix, e.get("summary", "")))
    if not hits:
        return []
    known = [h[2] for h in hits if h[2] != "?"]
    fix = max(known, key=vt) if known else "?"
    text = "\n".join(f"- {c} ({s}): {t}, fixed in {f}" for c, s, f, t in sorted(hits))
    text += "\n\nSource: https://curl.se/docs/security.html"
    return [upstream_finding("curl", fix, installed, text,
                             [h[0] for h in hits], "curl", inv)]


@source("Git advisories (GitHub)")
def check_git(inv, installed):
    v = vt(installed)
    out = subprocess.run(
        ["gh", "api", "--paginate", "repos/git/git/security-advisories?per_page=100"],
        capture_output=True, text=True, check=True, timeout=300).stdout
    advisories = json.loads(out.replace("][", ","))
    since = datetime.now(timezone.utc) - timedelta(days=3 * 365)
    hits = []
    for a in advisories:
        if datetime.fromisoformat(a["published_at"].replace("Z", "+00:00")) < since:
            continue
        if (a.get("severity") or "").lower() == "low":
            continue
        patched = [vt(p) for vuln in a.get("vulnerabilities") or []
                   for p in re.findall(r"v?(\d+\.\d+\.\d+)", vuln.get("patched_versions") or "")]
        if not patched:
            continue
        same = [p for p in patched if p[:2] == v[:2]]
        if same:
            fix = min(same)
            if v >= fix:
                continue
        elif v[:2] > max(patched)[:2]:
            continue  # a newer line than every patched one: has the fix
        else:
            fix = min(p for p in patched if p > v) if any(p > v for p in patched) else max(patched)
        hits.append((a.get("cve_id") or a["ghsa_id"], a.get("severity") or "?",
                     ".".join(map(str, fix)), a.get("summary", "")))
    if not hits:
        return []
    fix = max((h[2] for h in hits), key=vt)
    text = "\n".join(f"- {c} ({s}): {t}, fixed in {f}" for c, s, f, t in sorted(hits))
    text += "\n\nSource: https://github.com/git/git/security/advisories"
    return [upstream_finding("Git", fix, installed, text,
                             [h[0] for h in hits], "git", inv)]


def upstream_findings(inv):
    if inv["distro"] != "alpine":
        print(f"Upstream check skipped: {inv['distro']} backports fixes "
              "without changing versions.")
        return []
    found = []
    _, nginx = pkg_version(inv, "nginx")
    if nginx:
        found += check_nginx(inv, nginx)
    node = inv["node"] or (pkg_version(inv, r"nodejs(-current)?")[1] or "")
    if node:
        found += check_node(inv, node)
    _, php = pkg_version(inv, r"php\d+")
    if php:
        found += check_php(inv, php)
    python = pkg_version(inv, "python3")[1] or inv["python"]
    if python:
        found += check_python(inv, python)
    _, ssl = pkg_version(inv, r"libssl\d+|openssl")
    if ssl:
        found += check_openssl(inv, ssl)
    _, curl = pkg_version(inv, r"libcurl|curl")
    if curl:
        found += check_curl(inv, curl)
    _, git = pkg_version(inv, "git")
    if git:
        found += check_git(inv, git)
    return [f for f in found if f]


# --- source failures ------------------------------------------------------------


def failure_findings():
    path = os.path.join(STATE_DIR, "state.json") if STATE_DIR else ""
    state = {}
    if path and os.path.exists(path):
        state = json.load(open(path))
    today = date.today().isoformat()
    out = []
    for name in sorted(checked_sources):
        if name in failed_sources:
            entry = state.get(name) or {"runs": 0, "since": today}
            entry["runs"] += 1
            state[name] = entry
            if entry["runs"] >= FAILURE_RUNS:
                out.append({
                    "key": f"watch: {name} unreachable since {entry['since']}",
                    "title": f"Security watch: {name} unreachable since {entry['since']}",
                    "section": "Security watch",
                    "text": f"The daily security watch couldn't read {name} in "
                            f"{entry['runs']} runs in a row. Its findings are "
                            "missing until it works again; see the run's warnings.",
                    "todo": "Check whether the source moved or changed its "
                            "format and update `.github/scripts/security_watch.py`.",
                })
        else:
            state.pop(name, None)
    if path:
        os.makedirs(STATE_DIR, exist_ok=True)
        json.dump(state, open(path, "w"), indent=2)
    return out


# --- release pull request -------------------------------------------------------

RELEASE_BRANCH = "security-release"
RELEASE_LABEL = "security-release"
CHANGELOG = os.environ.get("CHANGELOG", "CHANGELOG.md")
VERSION_FILES = os.environ.get("VERSION_FILES", "").split()
TAG_PREFIX = os.environ.get("TAG_PREFIX", "v")
HEADING = re.compile(r"^## \[?(\d+\.\d+\.\d+)\]?(?: - \S+)?\s*$", re.M)


def bump(version, unreleased):
    major, minor, patch = vt(version)[:3]
    if re.search(r"^### Added", unreleased, re.M):
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def new_changelog(text, lines, repo):
    """Changelog with the next version's entry; (text, old, new, notes)."""
    m = HEADING.search(text)
    if not m:
        raise RuntimeError(f"no released version in {CHANGELOG}")
    old = m.group(1)
    bracket = m.group(0).startswith("## [")
    security = "".join(f"- {line}\n" for line in lines)
    unrel = re.search(r"^## \[?Unreleased\]?\s*\n(.*?)(?=^## )", text, re.M | re.S)
    body = unrel.group(1).strip("\n") if unrel else ""
    new = bump(old, body)
    intro = ("- Security updates of the distribution packages, picked up "
             "by rebuilding the image:\n")
    if re.search(r"^### Security\s*$", body, re.M):
        body = re.sub(r"^### Security\s*\n", lambda _: "### Security\n" + intro
                      + "".join("  " + l + "\n" for l in security.splitlines()),
                      body, count=1, flags=re.M)
    else:
        body = (body + "\n\n" if body else "") + "### Security\n" + intro + "".join(
            "  " + l + "\n" for l in security.splitlines())
    title = f"## [{new}] - {date.today().isoformat()}" if bracket else \
        f"## {new} - {date.today().isoformat()}"
    entry = f"{title}\n\n{body.strip()}\n\n"
    if unrel:
        text = text[:unrel.start()] + entry + text[unrel.end():]
    else:
        text = text[:m.start()] + entry + text[m.start():]
    link = re.search(rf"^\[{re.escape(old)}\]: \S+$", text, re.M)
    if link:
        url = (f"https://github.com/{repo}/compare/"
               f"{TAG_PREFIX}{old}...{TAG_PREFIX}{new}")
        text = text[:link.start()] + f"[{new}]: {url}\n" + text[link.start():]
    return text, old, new, body.strip()


def bump_version_files(old, new):
    for path in VERSION_FILES:
        text = open(path).read()
        if os.path.basename(path) == "VERSION":
            text = text.replace(old, new, 1)
        else:
            # Only lines that name the version (`__version__ = "1.2.3"`,
            # `version: "1.2.3"`), not every occurrence of the number.
            text = re.sub(rf"^(.*version.*){re.escape(old)}(.*)$",
                          lambda m: m.group(1) + new + m.group(2),
                          text, count=1, flags=re.M | re.I)
        if new not in text:
            raise RuntimeError(f"{path}: version {old} not found")
        open(path, "w").write(text)


def release_pr(items):
    token = os.environ.get("RELEASE_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token:
        warn("no RELEASE_TOKEN: no release pull request")
        return
    env = {**os.environ, "GH_TOKEN": token}
    run = lambda *a: subprocess.run(a, check=True, capture_output=True,
                                    text=True, env=env).stdout
    if json.loads(run("gh", "pr", "list", "--head", RELEASE_BRANCH,
                      "--state", "open", "--json", "number")):
        print("Release pull request already open.")
        return
    lines = sorted({i["line"] for i in items})
    text, old, new, notes = new_changelog(open(CHANGELOG).read(), lines, repo)
    run("git", "checkout", "-B", RELEASE_BRANCH)
    open(CHANGELOG, "w").write(text)
    bump_version_files(old, new)
    run("git", "add", CHANGELOG, *VERSION_FILES)
    run("git", "-c", "user.name=github-actions[bot]",
        "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-m", f"Release {new} with security updates")
    run("git", "push", "--force",
        f"https://x-access-token:{token}@github.com/{repo}.git",
        f"HEAD:refs/heads/{RELEASE_BRANCH}")
    labels = json.loads(run("gh", "label", "list", "--limit", "200", "--json", "name"))
    if RELEASE_LABEL not in {n["name"] for n in labels}:
        run("gh", "label", "create", RELEASE_LABEL, "--color", "0E8A16",
            "--description", "Merging it publishes the release")
    closes = sorted({n for i in items for n in i["issues"]})
    body = (f"Release **{new}** (was {old}). The distribution already ships "
            "these fixes; rebuilding the image picks them up:\n\n"
            + "".join(f"- {l}\n" for l in lines)
            + "\n**Merging this pull request publishes the release**: "
            "`security-release.yml` tags "
            f"`{TAG_PREFIX}{new}`, creates the GitHub release and the build "
            "publishes the image. Installations with automatic updates get it "
            "right after.\n\nChangelog entry:\n\n" + notes + "\n\n"
            + "".join(f"Closes #{n}\n" for n in closes)
            + "\n---\nOpened by the daily security watch. Close it without "
            "merging to skip this release.")
    url = run("gh", "pr", "create", "--head", RELEASE_BRANCH, "--base", "main",
              "--title", f"Release {new}: security updates", "--body", body,
              "--label", RELEASE_LABEL).strip()
    print(f"Release pull request: {url}")


def publish_release():
    """After the release pull request is merged: tag and publish."""
    text = open(CHANGELOG).read()
    m = HEADING.search(text)
    version = m.group(1)
    end = re.compile(r"^## ", re.M).search(text, m.end())
    notes = text[m.end():end.start() if end else len(text)].strip()
    notes = re.sub(r"^\[\d+\.\d+\.\d+\]: \S+\s*$", "", notes, flags=re.M).strip()
    tag = f"{TAG_PREFIX}{version}"
    if subprocess.run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
                      capture_output=True).returncode == 0:
        print(f"{tag} exists already; nothing to do.")
        return
    git = ["git", "-c", "user.name=github-actions[bot]",
           "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com"]
    subprocess.run(git + ["tag", "-a", tag, "-m", f"Release {version}"], check=True)
    # The checkout's credentials are RELEASE_TOKEN's, so the tag starts the build.
    subprocess.run(["git", "push", "origin", tag], check=True)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(notes + "\n")
    subprocess.run(["gh", "release", "create", tag, "--title", version,
                    "--notes-file", f.name, "--verify-tag"], check=True)
    print(f"Published {tag}")


# --- issues -------------------------------------------------------------------


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True,
                          check=True).stdout


def existing_issues():
    out = gh("issue", "list", "--state", "all", "--label", LABEL,
             "--limit", "1000", "--json", "number,title,state")
    return json.loads(out)


def stem(title):
    # The installed version may differ between runs; match on the rest.
    return title.split(" (image has")[0]


def ensure_label():
    names = json.loads(gh("label", "list", "--limit", "200", "--json", "name"))
    if LABEL not in {n["name"] for n in names}:
        gh("label", "create", LABEL, "--color", "B60205",
           "--description", "Opened by the daily security watch")


def main():
    inv = inventory()
    print(f"{IMAGE}: {inv['distro']} {inv['distro_version']}, "
          f"{len(inv['pkgs'])} packages")
    findings = trivy_findings(inv) + upgrade_findings(inv) + upstream_findings(inv)
    findings += failure_findings()

    merged = {}
    for f in findings:
        if f["key"] in merged:
            merged[f["key"]]["sections"].append((f["section"], f["text"]))
        else:
            merged[f["key"]] = {**f, "sections": [(f["section"], f["text"])]}

    if not merged:
        print("Nothing found.")
        return
    issues = [] if DRY_RUN else existing_issues()
    if not DRY_RUN:
        ensure_label()
    release_items = []
    run_url = "{}/{}/actions/runs/{}".format(
        os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
        os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", ""))
    for f in merged.values():
        known = [i for i in issues if stem(i["title"]) == stem(f["title"])]
        if f.get("release"):
            # Fixed by a rebuild: part of the release pull request, unless
            # the issue was closed without a fix (dismissed).
            open_ = [i["number"] for i in known if i["state"] == "OPEN"]
            if open_ or not known:
                release_items.append({"line": f["release"], "issues": open_})
        if known:
            print(f"Already reported: {f['title']}")
            continue
        body = f"Image: `{IMAGE}`\n\n"
        body += "\n\n".join(f"### {s}\n\n{t}" for s, t in f["sections"])
        body += f"\n\n### What to do\n\n{f['todo']}\n\n"
        body += ("---\nOpened by the daily security watch "
                 f"([run]({run_url})). Close this issue after the release "
                 "that fixes it; a closed issue isn't opened again.")
        if DRY_RUN:
            print(f"\n=== {f['title']}\n{body}")
        else:
            url = gh("issue", "create", "--title", f["title"], "--label", LABEL,
                     "--body", body).strip()
            print(f"Opened: {f['title']}")
            if f.get("release") and release_items:
                release_items[-1]["issues"].append(int(url.rsplit("/", 1)[1]))
    if release_items:
        release_pr(release_items)


if __name__ == "__main__":
    try:
        publish_release() if sys.argv[1:] == ["release"] else main()
    except subprocess.CalledProcessError as e:
        print(e.stderr or e, file=sys.stderr)
        raise
