"""Watching a Google Drive folder for manuscripts to prepare.

A headless second front door to the same pipelines the app runs. It wakes on a
schedule, looks in one folder, prepares any manuscript nobody has prepared yet,
puts the result back beside the original, and exits. There is no daemon: the
work between two ticks is worth nothing to keep in memory, and everything that
must survive is either a marker on the Drive file or a file in the watch home.

Prep only, for now. `tick.py` names the seams where copy editing joins.

See docs/watch.md.
"""
