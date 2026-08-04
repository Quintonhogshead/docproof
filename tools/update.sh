#!/usr/bin/env bash
#
# Rebuild DocProof.app from this checkout.
#
#   tools/update.sh              build it, leave it in dist/
#   tools/update.sh --install    build it, then replace /Applications/DocProof.app
#   tools/update.sh --skip-tests build without running the suite first
#
# There is no download step. DocProof is built on the machine it runs on, and
# the bundle is unsigned, so an app that fetched and ran new code on your
# behalf would be a worse idea than a script you can read.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
INSTALLED="/Applications/DocProof.app"

install=false
tests=true
for arg in "$@"; do
  case "$arg" in
    --install) install=true ;;
    --skip-tests) tests=false ;;
    -h|--help) sed -n '3,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ ! -x "$PYTHON" ]; then
  echo "No virtualenv at $PYTHON. Create one, or set PYTHON=/path/to/python." >&2
  exit 1
fi

version() { "$PYTHON" - <<'PY'
import ast, pathlib
for line in pathlib.Path("docproof/__init__.py").read_text().splitlines():
    if line.startswith("__version__"):
        print(ast.literal_eval(line.split("=", 1)[1].strip())); break
PY
}

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

say "Where you are"
echo "  version   $(version)"
echo "  commit    $(git rev-parse --short HEAD 2>/dev/null || echo 'not a checkout')"
echo "  branch    $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '-')"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  echo "  changes   uncommitted changes in the working tree — they WILL be built in"
fi
if [ -d "$INSTALLED" ]; then
  echo "  installed $(defaults read "$INSTALLED/Contents/Info" CFBundleShortVersionString 2>/dev/null || echo '?') at $INSTALLED"
fi

# Only pull when there is somewhere to pull from. This repo may well be local.
if git remote | grep -q .; then
  say "Fetching"
  before="$(git rev-parse HEAD)"
  git pull --ff-only
  after="$(git rev-parse HEAD)"
  if [ "$before" = "$after" ]; then
    echo "  already up to date"
  else
    git --no-pager log --oneline "$before..$after" | sed 's/^/  /'
  fi
else
  say "Fetching"
  echo "  no git remote — building from what is in this folder"
fi

if $tests; then
  say "Checking it still works"
  "$PYTHON" -m pytest -q
fi

say "Building"
rm -rf build/DocProof dist/DocProof dist/DocProof.app
"$PYTHON" -m PyInstaller --noconfirm DocProof.spec

built="dist/DocProof.app"
[ -d "$built" ] || { echo "Build produced no $built" >&2; exit 1; }

if $install; then
  say "Installing"
  # Ask before replacing something that is running: macOS will happily let you
  # swap the bundle out from under an open app, and it will misbehave later.
  if pgrep -xq DocProof; then
    echo "DocProof is running. Quit it first, then run this again with --install." >&2
    exit 1
  fi
  rm -rf "$INSTALLED"
  cp -R "$built" "$INSTALLED"
  echo "  $INSTALLED is now $(version)"
else
  say "Built"
  echo "  $ROOT/$built"
  echo "  Install it with:  tools/update.sh --install"
fi

say "Done — $(version) ($(git rev-parse --short HEAD 2>/dev/null || echo 'no commit'))"
