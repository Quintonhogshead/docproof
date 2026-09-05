"""Phase order shared by the driver and decision log."""

ALL_PHASES: tuple[str, ...] = (
    "profile", "approve", "sweeps", "ladder", "flights", "audit", "reread",
    "verify", "settle", "certify", "deliver",
)
COPYEDIT_PHASES: tuple[str, ...] = ("flights", "reread")
MECHANICAL_PHASES: tuple[str, ...] = tuple(
    phase for phase in ALL_PHASES if phase not in COPYEDIT_PHASES
)
