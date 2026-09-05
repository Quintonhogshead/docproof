"""What this build is, and whether a newer one exists.

Two audiences, so two answers. On the machine DocProof was built on, the
question is about the checkout: has HEAD moved since the commit this build was
made from. On a Mac somebody was *sent* a build, there is no checkout, so the
question goes to the GitHub releases the builder publishes.

What it deliberately does not do is update itself. The bundle is unsigned, so
an app that downloaded and ran new code on somebody's behalf would be asking
them to trust something macOS is right to distrust. It fetches the disk image
and shows it to them in the Finder; installing it is their decision, and it is
one drag.
"""
from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from docproof import __version__

from .settings import get_api_key, resource_root

log = logging.getLogger("docproof.app.version")

BUILD_INFO = "build_info.json"
GITHUB_API = "https://api.github.com"
# GitHub's own documented pin. Without it the API is free to change shape.
GITHUB_ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"


def _git(args: list[str], cwd: Path, *, runner=subprocess.run) -> str:
    """One git answer, or "" — every caller here has a sensible thing to say
    when git is missing, the directory is not a checkout, or the commit the
    build was made from is no longer in the history."""
    try:
        done = runner(["git", "-C", str(cwd), *args], capture_output=True,
                      text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("git %s failed: %s", " ".join(args), e)
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def repo_slug(url: str) -> str:
    """"owner/name" out of any of the URL shapes git hands back."""
    url = url.strip().removesuffix(".git")
    for prefix in ("git@github.com:", "ssh://git@github.com/",
                   "https://github.com/", "http://github.com/"):
        if url.startswith(prefix):
            return url[len(prefix):]
    return ""


def build_info(*, runner=subprocess.run) -> dict:
    """This build, described.

    A packaged .app reads what the spec stamped into it. Run from a checkout
    there is nothing stamped and nothing to stamp — the source *is* what is
    running — so the same questions are put to git directly."""
    stamped = resource_root() / BUILD_INFO
    if stamped.is_file():
        try:
            info = json.loads(stamped.read_text("utf-8"))
            info["frozen"] = True
            info.setdefault("repo", "")      # stamped by builds that have one
            return info
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Ignoring unreadable %s (%s)", BUILD_INFO, e)

    root = resource_root()
    return {
        "version": __version__,
        "built": "",                     # nothing was built; this is the source
        "commit": _git(["rev-parse", "--short", "HEAD"], root, runner=runner),
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], root,
                       runner=runner),
        "source": str(root),
        "repo": repo_slug(_git(["remote", "get-url", "origin"], root,
                               runner=runner)),
        "frozen": False,
    }



def _open_url(request: urllib.request.Request, timeout: int = 30):
    """The one place this module touches the network. Passed in by every
    caller so no test ever reaches GitHub."""
    return urllib.request.urlopen(request, timeout=timeout)


def _request(url: str, token: str, *, accept: str = GITHUB_ACCEPT):
    request = urllib.request.Request(url)
    request.add_header("Accept", accept)
    request.add_header("X-GitHub-Api-Version", API_VERSION)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    return request


def latest_release(repo: str, token: str, *, opener=_open_url) -> dict:
    """The newest published release, or a dict saying why not.

    Every failure here is something a person can act on — no token, the wrong
    token, no releases yet, no internet — so each gets its own sentence rather
    than a traceback."""
    try:
        with opener(_request(f"{GITHUB_API}/repos/{repo}/releases/latest",
                             token)) as response:
            return {"ok": True, "release": json.loads(response.read())}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "message":
                    "GitHub would not accept that token. Check it has not "
                    "expired and that it can read this repository."}
        if e.code == 404:
            # A private repo answers 404 rather than 403 to a token that cannot
            # see it, so this is two problems wearing the same hat.
            return {"ok": False, "message":
                    f"No published release found for {repo} — either none has "
                    f"been made yet, or the token cannot see that repository."}
        return {"ok": False, "message": f"GitHub answered {e.code} ({e.reason})."}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"ok": False, "message":
                f"Could not reach GitHub: {getattr(e, 'reason', e)}."}
    except (json.JSONDecodeError, OSError) as e:
        return {"ok": False, "message": f"GitHub sent something unreadable: {e}"}


def _as_numbers(version: str) -> tuple:
    """"v0.10.2" → (0, 10, 2), so 0.10 sorts above 0.9 the way a person means."""
    cleaned = version.strip().lstrip("vV").split("+")[0].split("-")[0]
    parts = []
    for piece in cleaned.split("."):
        if not piece.isdigit():
            return ()                    # not a number we can reason about
        parts.append(int(piece))
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    """Is `candidate` a later version than `current`? Unparseable versions fall
    back to "different means newer", which errs towards telling somebody there
    is something to look at rather than silently sitting on it."""
    a, b = _as_numbers(candidate), _as_numbers(current)
    if a and b:
        return a > b
    return candidate.strip().lstrip("vV") != current.strip().lstrip("vV")


def dmg_asset(release: dict) -> dict | None:
    for asset in release.get("assets") or []:
        if str(asset.get("name", "")).endswith(".dmg"):
            return asset
    return None


def check_for_update(*, runner=subprocess.run, opener=_open_url,
                     token: str | None = None) -> dict:
    """Is there a newer DocProof than this one?

    Asked of whichever source of truth this machine actually has: the checkout
    if there is one, the published releases if there isn't. Returns something
    the Settings screen can print as-is — every failure is a sentence, because
    a moved folder, a missing token and a repo with no releases yet are all
    normal things to find rather than faults."""
    info = build_info(runner=runner)

    if not info["frozen"]:
        dirty = _git(["status", "--porcelain"], Path(info["source"]),
                     runner=runner)
        return {"ok": True, "current": True, "info": info,
                "message": "You are running from the source, so this is as new "
                           "as it gets."
                           + (" It has uncommitted changes in it."
                              if dirty else "")}

    source = Path(info.get("source") or "")
    if source.is_dir() and _git(["rev-parse", "--short", "HEAD"], source,
                                runner=runner):
        return _check_against_checkout(info, source, runner=runner)
    return _check_against_releases(info, opener=opener, token=token)


def _check_against_checkout(info: dict, source: Path, *, runner) -> dict:
    """The machine this was built on: compare against the working checkout,
    which is ahead of any release the moment somebody commits."""
    head = _git(["rev-parse", "--short", "HEAD"], source, runner=runner)
    built_from = info.get("commit") or ""
    if head == built_from:
        return {"ok": True, "current": True, "info": info, "head": head,
                "message": f"Up to date — this is the latest build of "
                           f"{info['version']}."}

    behind = _git(["rev-list", "--count", f"{built_from}..HEAD"], source,
                  runner=runner) if built_from else ""
    if behind.isdigit() and int(behind) > 0:
        n = int(behind)
        what = f"{n} change{'' if n == 1 else 's'}"
    else:
        # The commit it was built from is not an ancestor of HEAD: a rebase, a
        # different branch, or a build from a checkout since rewritten.
        what = "changes"
    return {"ok": True, "current": False, "info": info, "head": head,
            "behind": behind, "source": "checkout",
            # This machine has the source, so the app can rebuild itself from
            # it — no release to publish and nothing to type. `tools/update.sh`
            # still does the same job for anyone who prefers a terminal.
            "can_rebuild": True,
            "message": f"There {'is' if what.startswith('1 ') else 'are'} "
                       f"{what} in {source} that this build does not have."}


def _check_against_releases(info: dict, *, opener, token: str | None) -> dict:
    """A Mac that was sent this build: ask GitHub what has been published."""
    repo = info.get("repo") or ""
    token = get_api_key("github") if token is None else token
    if not repo:
        return {"ok": False, "info": info,
                "message": "This build does not say where it was published "
                           "from, so there is nothing to check. Ask whoever "
                           "sent it to you whether there is a newer one."}
    if not token:
        return {"ok": False, "info": info, "needs_token": True,
                "message": "To check for new versions, paste the GitHub token "
                           "you were given below. Without it, ask whoever sent "
                           "you DocProof whether there is a newer build."}

    found = latest_release(repo, token, opener=opener)
    if not found["ok"]:
        return {"ok": False, "info": info, "message": found["message"]}

    release = found["release"]
    tag = str(release.get("tag_name") or "")
    if not is_newer(tag, info["version"]):
        return {"ok": True, "current": True, "info": info, "source": "github",
                "message": f"Up to date — {info['version']} is the latest "
                           f"release."}

    asset = dmg_asset(release)
    name = str(release.get("name") or tag)
    return {"ok": True, "current": False, "info": info, "source": "github",
            "tag": tag, "release_name": name, "notes": release.get("body") or "",
            "asset": {"name": asset["name"], "size": asset.get("size", 0)}
                     if asset else None,
            "message": (f"{name} is available — you have {info['version']}."
                        if asset else
                        f"{name} is available, but that release has no disk "
                        f"image attached. Ask whoever published it.")}


def download_release(*, opener=_open_url, runner=subprocess.run,
                     token: str | None = None,
                     into: Path | None = None) -> dict:
    """Fetch the newest release's disk image into ~/Downloads.

    Downloaded, not installed. DocProof is unsigned, so replacing it with code
    fetched over the network is precisely the thing macOS is right to stop; the
    recipient drags the new copy over the old one themselves."""
    checked = check_for_update(runner=runner, opener=opener, token=token)
    if checked.get("current") or not checked.get("ok"):
        return {"ok": False, "message": checked["message"]}
    asset = checked.get("asset")
    if not asset:
        return {"ok": False, "message": checked["message"]}

    info = checked["info"]
    repo = info["repo"]
    token = get_api_key("github") if token is None else token
    into = into or Path.home() / "Downloads"
    into.mkdir(parents=True, exist_ok=True)
    target = into / asset["name"]

    # Re-read the release for the asset id: the check only kept what it needed
    # to describe it, and an id is not something to reconstruct.
    found = latest_release(repo, token, opener=opener)
    if not found["ok"]:
        return {"ok": False, "message": found["message"]}
    match = next((a for a in found["release"].get("assets") or []
                  if a.get("name") == asset["name"]), None)
    if match is None:
        return {"ok": False, "message": "That release changed while DocProof "
                                        "was looking at it. Try again."}

    url = f"{GITHUB_API}/repos/{repo}/releases/assets/{match['id']}"
    try:
        with opener(_request(url, token,
                             accept="application/octet-stream")) as response:
            target.write_bytes(response.read())
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return {"ok": False,
                "message": f"The download did not finish: "
                           f"{getattr(e, 'reason', e)}"}

    log.info("Downloaded %s to %s", target.name, into)
    return {"ok": True, "path": str(target), "filename": target.name,
            "message": f"{target.name} is in your Downloads folder. Open it "
                       f"and drag DocProof to Applications, replacing the old "
                       f"one."}
