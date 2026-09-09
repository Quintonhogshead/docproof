"""Content that cannot be represented by the manuscript's plain-text stream."""
from __future__ import annotations

import hashlib

from lxml import etree

from ..utils.xml_helpers import qn

MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
MATH_TAGS = {f"{{{MATH_NS}}}oMath", f"{{{MATH_NS}}}oMathPara"}
GRAPHIC_TAGS = {qn(tag) for tag in ("w:drawing", "w:pict", "w:object")}
PROTECTED_TAGS = MATH_TAGS | GRAPHIC_TAGS


def has_math(element) -> bool:
    return any(node.tag in MATH_TAGS for node in element.iter())


def protected_content(root) -> tuple[str, ...]:
    """Ordered fingerprints of graphics and equations, excluding nested copies.

    Text comparison cannot detect a missing drawing or a changed equation.
    These elements are outside prep's formatting remit and must survive intact.
    Exclusive canonicalization ignores namespace declarations on their parents.
    """
    result = []
    for node in root.iter():
        if node.tag not in PROTECTED_TAGS:
            continue
        if any(parent.tag in PROTECTED_TAGS for parent in node.iterancestors()):
            continue
        data = etree.tostring(node, method="c14n", exclusive=True)
        result.append(hashlib.sha256(data).hexdigest())
    return tuple(result)
