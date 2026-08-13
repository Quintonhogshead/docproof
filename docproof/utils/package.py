"""The zip-backed document package shared by every format.

A .docx and an .idml are both a zip of XML parts, and both make the same
fidelity promise: parts docproof did not edit are written back byte-for-byte,
never re-serialized. Only the part names differ, so the machinery lives here
once and each format subclasses it.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree


class ZipPackage:
    """A zip opened into memory. Parts are parsed lazily; only parts explicitly
    marked modified are re-serialized on save.

    Each entry's original compression type, order, timestamp and attributes are
    preserved. That matters beyond tidiness: IDML requires its `mimetype` entry
    to come first and be stored uncompressed, and re-deflating it produces a
    package InDesign refuses."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        with zipfile.ZipFile(self.path) as z:
            infos = z.infolist()
            self._order = [i.filename for i in infos]
            self._infos = {i.filename: i for i in infos}
            self._raw = {n: z.read(n) for n in self._order}
        self._trees: dict[str, etree._Element] = {}
        self._dirty: set[str] = set()
        self._new: dict[str, etree._Element] = {}
        self._new_binary: dict[str, bytes] = {}

    def has(self, name: str) -> bool:
        return name in self._raw or name in self._new or name in self._new_binary

    def names(self) -> list[str]:
        return list(self._order) + list(self._new) + list(self._new_binary)

    def raw(self, name: str) -> bytes:
        """The stored bytes of a part, for entries that are not XML."""
        return self._raw[name]

    def tree(self, name: str) -> etree._Element:
        if name in self._new:
            return self._new[name]
        if name not in self._trees:
            self._trees[name] = etree.fromstring(self._raw[name])
        return self._trees[name]

    def mark_modified(self, name: str) -> None:
        self._dirty.add(name)

    def add_part(self, name: str, root: etree._Element) -> None:
        self._new[name] = root
        self._dirty.add(name)

    def add_binary_part(self, name: str, data: bytes) -> None:
        """A part that is bytes, not XML — an embedded font, an image. Written
        on save exactly as given, never serialized."""
        self._new_binary[name] = data

    def _serialize(self, name: str) -> bytes:
        """Serialize a part, including anything that sits *outside* its root
        element.

        It has to be the ElementTree rather than the root Element: IDML's
        designmap.xml opens with a `<?aid …?>` processing instruction that
        identifies the package to InDesign, and serializing the element alone
        silently drops it — producing a file InDesign refuses to open with no
        indication of why."""
        return etree.tostring(self.tree(name).getroottree(),
                              xml_declaration=True, encoding="UTF-8",
                              standalone=True)

    def save(self, out_path: str | Path) -> None:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            for name in self._order:
                data = (self._serialize(name) if name in self._dirty
                        else self._raw[name])
                z.writestr(self._entry(name), data)
            for name in self._new:
                z.writestr(self._entry(name), self._serialize(name))
            for name, data in self._new_binary.items():
                z.writestr(self._entry(name), data)

    def _entry(self, name: str) -> zipfile.ZipInfo:
        """A ZipInfo that carries the original entry's metadata forward, so a
        part we rewrote is stored the same way the one we didn't would be."""
        old = self._infos.get(name)
        if old is None:
            return zipfile.ZipInfo(name)
        info = zipfile.ZipInfo(name, date_time=old.date_time)
        info.compress_type = old.compress_type
        info.external_attr = old.external_attr
        info.internal_attr = old.internal_attr
        info.create_system = old.create_system
        return info
