"""Phase order shared by the driver and decision log."""

ALL_PHASES: tuple[str, ...] = (
    "profile", "approve", "sweeps", "ladder", "flights", "audit", "reread",
    "adjudicate", "verify", "settle", "astra_review", "certify", "deliver",
)
COPYEDIT_PHASES: tuple[str, ...] = ("flights", "reread")
MECHANICAL_PHASES: tuple[str, ...] = tuple(
    phase for phase in ALL_PHASES if phase not in COPYEDIT_PHASES
)

#: What each driver phase is actually doing, in the words the Proofread
#: drawer shows a person who is not going to read the log. The fixed lane's
#: own stages carry their descriptions in `galley.fixed_workflow.workflow_plan`;
#: these are the mechanical driver's.
PHASE_WORK: dict[str, str] = {
    "profile": "Reading the manuscript and planning the work",
    "approve": "Approving the plan against the budget",
    "sweeps": "Mechanical sweeps over the whole manuscript",
    "ladder": "Working the correction ladder",
    "flights": "Concurrent copy-editing flights",
    "audit": "Auditing what the sweeps changed",
    "reread": "Re-reading the corrected manuscript",
    "adjudicate": "Deciding the disputed corrections",
    "verify": "Verifying every change landed as written",
    "settle": "Settling the residual items round by round",
    "astra_review": "Astra's final review of the corrected book",
    "certify": "Certifying the run and building the package",
    "deliver": "Uploading the hand-off to Drive",
    "formatting": "Formatting the reading copy",
}
