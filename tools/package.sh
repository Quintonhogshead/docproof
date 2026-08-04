#!/usr/bin/env bash
#
# Build a DocProof disk image you can send to someone.
#
#   tools/package.sh               build dist/DocProof-<version>-<commit>.dmg
#   tools/package.sh --skip-tests  build without running the suite first
#
# The result is a normal Mac disk image: DocProof, a shortcut to Applications
# to drag it onto, and a "Read me first" page. It is NOT signed with Apple, so
# the person opening it has to approve it once — which is exactly what the read
# me tells them to do. Signing and notarising with a Developer ID would remove
# that step; see docs/app.md.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
STAGE="build/dmg"

tests=true
for arg in "$@"; do
  case "$arg" in
    --skip-tests) tests=false ;;
    -h|--help) sed -n '3,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

[ -x "$PYTHON" ] || { echo "No virtualenv at $PYTHON." >&2; exit 1; }

version() { "$PYTHON" - <<'PY'
import ast, pathlib
for line in pathlib.Path("docproof/__init__.py").read_text().splitlines():
    if line.startswith("__version__"):
        print(ast.literal_eval(line.split("=", 1)[1].strip())); break
PY
}

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

VERSION="$(version)"
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
DMG="dist/DocProof-$VERSION-$COMMIT.dmg"

say "Packaging"
echo "  version   $VERSION"
echo "  commit    $COMMIT"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  echo "  changes   uncommitted changes in the working tree — they WILL be shipped"
fi

if $tests; then
  say "Checking it still works"
  "$PYTHON" -m pytest -q
fi

say "Building the app"
rm -rf build/DocProof dist/DocProof dist/DocProof.app
"$PYTHON" -m PyInstaller --noconfirm DocProof.spec
[ -d dist/DocProof.app ] || { echo "No dist/DocProof.app was produced" >&2; exit 1; }

say "Staging the disk image"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R dist/DocProof.app "$STAGE/DocProof.app"
ln -s /Applications "$STAGE/Applications"

# The read me is a template so the version, build date and commit in it are the
# ones actually inside the image — a stale "read me first" is worse than none.
BUILT="$(date '+%e %B %Y' | sed 's/^ //')"
sed -e "s/@VERSION@/$VERSION/g" \
    -e "s/@COMMIT@/$COMMIT/g" \
    -e "s/@BUILT@/$BUILT/g" \
    tools/readme_dmg.html > "$STAGE/Read me first.html"
echo "  DocProof.app, Applications shortcut, Read me first.html"

say "Making $DMG"
mkdir -p dist
rm -f "$DMG"
hdiutil create \
  -volname "DocProof $VERSION" \
  -srcfolder "$STAGE" \
  -format UDZO -ov -quiet \
  "$DMG"
rm -rf "$STAGE"

SIZE="$(du -h "$DMG" | cut -f1 | tr -d ' ')"
say "Done"
echo "  $ROOT/$DMG  ($SIZE)"
echo
echo "  Send that one file. The person opening it will have to approve"
echo "  DocProof once in System Settings → Privacy & Security, because it is"
echo "  unsigned — the read me inside walks them through it."
