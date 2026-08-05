"""Entry point for the packaged Mac app.

PyInstaller runs its entry script as `__main__`, with no package around it, so
`app/desktop.py` cannot be the entry point directly — its relative imports
would have nothing to resolve against. This stub is the thing PyInstaller
runs; it imports the real module by package name.

`--watch-once` is answered here rather than inside `app.desktop` so that a
scheduled pass never imports uvicorn, FastAPI or pywebview. macOS starts this
a few times a day to look in a Drive folder, and none of that is on its way.

It exists at all because a packaged DocProof has no `docproof-watch` command
anywhere on the machine — somebody who was sent an .app has the app and
nothing else — so the app has to be able to schedule itself.
"""
import sys

WATCH_ONCE = "--watch-once"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if WATCH_ONCE in argv:
        # Straight to the watcher's own command line rather than to `tick`,
        # which buys the whole of its careful behaviour: the folder lock, the
        # log in the watch home, standing aside politely when a pass is already
        # running, and the exit codes launchd reads. `--home` here is the
        # watcher's, because the watcher's parser is what reads it.
        from app.watch.cli import main as watch
        return watch([a for a in argv if a != WATCH_ONCE] + ["once"])
    from app.desktop import main as desktop
    return desktop(argv)


if __name__ == "__main__":
    raise SystemExit(main())
