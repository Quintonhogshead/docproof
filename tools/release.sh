#!/usr/bin/env bash
#
# Publish a DocProof release to GitHub.
#
#   1. Bump __version__ in docproof/__init__.py and commit it.
#   2. tools/release.sh
#
# Tags v<version>, builds the disk image, and creates the GitHub release with
# the image attached. That release is what a copy of DocProof somebody was sent
# checks against, so publishing one is what makes their "check for a newer
# version" say yes.
#
#   tools/release.sh --draft       create it as a draft to look at first
#   tools/release.sh --skip-tests  pass through to package.sh
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"

draft=""
package_args=()
for arg in "$@"; do
  case "$arg" in
    --draft) draft="--draft" ;;
    --skip-tests) package_args+=("--skip-tests") ;;
    -h|--help) sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }
die() { echo "$1" >&2; exit 1; }

version() { "$PYTHON" - <<'PY'
import ast, pathlib
for line in pathlib.Path("docproof/__init__.py").read_text().splitlines():
    if line.startswith("__version__"):
        print(ast.literal_eval(line.split("=", 1)[1].strip())); break
PY
}

command -v gh >/dev/null || die "The GitHub CLI is not installed: brew install gh"
gh auth status >/dev/null 2>&1 || die "Not signed in to GitHub: gh auth login"
git remote get-url origin >/dev/null 2>&1 || die "No 'origin' remote to publish to."

VERSION="$(version)"
TAG="v$VERSION"

# A release is a permanent public claim about what a version contains. Building
# one out of a dirty tree makes that claim untrue and unreproducible.
[ -z "$(git status --porcelain)" ] || \
  die "The working tree has uncommitted changes. Commit them first — a release
should be something you can check out again."

if git rev-parse "$TAG" >/dev/null 2>&1; then
  die "$TAG already exists. Bump __version__ in docproof/__init__.py first."
fi

PREVIOUS="$(git describe --tags --abbrev=0 2>/dev/null || true)"
COMMIT="$(git rev-parse --short HEAD)"
DMG="dist/DocProof-$VERSION-$COMMIT.dmg"

say "Releasing $TAG"
echo "  commit    $COMMIT"
echo "  previous  ${PREVIOUS:-none — this is the first release}"

tools/package.sh "${package_args[@]}"
[ -f "$DMG" ] || die "package.sh did not produce $DMG"

say "Tagging and pushing"
git tag -a "$TAG" -m "DocProof $VERSION"
git push origin HEAD
git push origin "$TAG"

if [ -n "$PREVIOUS" ]; then
  NOTES="$(git log --no-merges --pretty='- %s' "$PREVIOUS..HEAD")"
else
  NOTES="$(git log --no-merges --pretty='- %s' -20)"
fi

say "Creating the release"
gh release create "$TAG" "$DMG" \
  --title "DocProof $VERSION" \
  --notes "$NOTES" \
  $draft

say "Published"
gh release view "$TAG" --json url --jq '"  " + .url'
echo
echo "  Anyone with a copy of DocProof and a read-only token will now see this"
echo "  when they press Check for a newer version."
