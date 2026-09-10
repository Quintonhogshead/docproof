"""Readable, exactly reversible evidence tables and shared definitions.

This applies the API packet compactor's table/definition strategy to arbitrary
subscription records. It keeps XML trees as data, including absent versus empty
fields, namespace-qualified names, attributes, text, tails and every child.
"""
from __future__ import annotations

from collections import Counter
import copy

from galley import astra_review as ar

VERSION = 1
_MARKERS = {"$ref", "$table", "$object", "$xml", "$runs"}
_XML_FIELDS = ("tag", "attributes", "text", "children", "tail")
READING_GUIDE = (
    "Resolve every {$ref: key} verbatim from definitions, including nested references. "
    "A {$table: {columns, rows}} is an ordered list of objects: each row aligns exactly "
    "to columns; all cells, including null and empty values, are present. "
    "A {$object: [[key, value], ...]} is an ordinary object with literal keys. "
    "A {$xml: row} is an XML node with row fields [tag, attributes, text, children, tail]; "
    "omitted trailing cells and null cells denote absent fields. Child rows use the same "
    "layout (or a $ref); empty strings/objects/lists remain explicit. Namespace names, "
    "all attributes, text, tails and child order are unchanged. "
    "A {$runs: rows} lists formatting objects with columns [start, end, properties]. "
    "Repeated text/XML/property evidence is stored once; every occurrence and record "
    "still requires review. Nothing is a summary. Hashes bind the expanded canonical data."
)


def _xml_node(item):
    """Only canonical XML shapes use null as absence; all other data stays generic."""
    return (isinstance(item, dict) and isinstance(item.get("tag"), str)
            and set(item) <= set(_XML_FIELDS)
            and all(isinstance(item[key], str) for key in ("text", "tail") if key in item)
            and ("attributes" not in item or isinstance(item["attributes"], dict)
                 and all(isinstance(k, str) and isinstance(v, str) for k, v in item["attributes"].items()))
            and ("children" not in item or isinstance(item["children"], list)
                 and all(_xml_node(child) for child in item["children"])))


def _candidate(value):
    if isinstance(value, str) and len(value) >= 80:
        return ar._json(value)
    if isinstance(value, dict):
        serial = ar._json(value)
        if len(serial) >= 40:
            return serial
    return None


def pack_evidence(value):
    """Return an independent self-describing envelope; retain every JSON value."""
    counts = Counter()

    def collect(item):
        key = _candidate(item)
        if key is not None:
            counts[key] += 1
        if isinstance(item, dict):
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)

    collect(value)
    definitions, keys = {}, {}

    def encode(item, *, definition=False):
        candidate = _candidate(item)
        if not definition and candidate is not None and counts[candidate] > 1:
            if candidate not in keys:
                key = f"d{len(keys)}"
                keys[candidate] = key
                definitions[key] = encode(item, definition=True)
            return {"$ref": keys[candidate]}
        if isinstance(item, dict):
            if _xml_node(item):
                row = []
                for field in _XML_FIELDS:
                    child = item.get(field)
                    if field == "children" and child is not None:
                        children = [encode(node) for node in child]
                        row.append([node["$xml"] if set(node) == {"$xml"} else node for node in children])
                    else:
                        row.append(encode(child))
                while row[-1] is None:
                    row.pop()
                return {"$xml": row}
            encoded = {key: encode(child) for key, child in item.items()}
            if _MARKERS.intersection(item):
                return {"$object": list(map(list, encoded.items()))}
            return encoded
        if isinstance(item, list):
            encoded = [encode(child) for child in item]
            if item and all(isinstance(row, dict) and set(row) == {"start", "end", "properties"} for row in item):
                runs = {"$runs": [[encode(row[key]) for key in ("start", "end", "properties")] for row in item]}
                if len(ar._json(runs)) < len(ar._json(encoded)):
                    return runs
            # Homogeneous key sets avoid lossy missing-cell/null conflation.
            if (len(item) > 1 and all(isinstance(row, dict) for row in item)
                    and all(set(row) == set(item[0]) for row in item)):
                columns = sorted(item[0])
                rows = [[encode(row[column]) for column in columns] for row in item]
                table = {"$table": {"columns": columns, "rows": rows}}
                if len(ar._json(table)) < len(ar._json(encoded)):
                    return table
            return encoded
        return item

    data = encode(value)
    # Exploring a table alternative may have introduced definitions that the
    # selected representation does not reference. Keep only reachable data.
    used = set()

    def references(item):
        if isinstance(item, dict):
            if set(item) == {"$ref"}:
                key = item["$ref"]
                if key not in used:
                    used.add(key)
                    references(definitions[key])
            else:
                for child in item.values():
                    references(child)
        elif isinstance(item, list):
            for child in item:
                references(child)

    references(data)
    return {"encoding_version": VERSION, "reading_guide": READING_GUIDE,
            "definitions": {key: item for key, item in definitions.items() if key in used},
            "data": data}


def unpack_evidence(packed):
    """Expand the encoding without executing XML or evidence instructions."""
    if type(packed.get("encoding_version")) is not int or packed["encoding_version"] != VERSION:
        raise ar.AstraReviewError("Unsupported subscription evidence encoding")
    definitions = packed["definitions"]
    resolved, active = {}, set()

    def decode(item):
        if isinstance(item, dict):
            if set(item) == {"$ref"}:
                key = item["$ref"]
                if key not in definitions or key in active:
                    raise ar.AstraReviewError("Invalid subscription evidence reference")
                if key not in resolved:
                    active.add(key)
                    resolved[key] = decode(definitions[key])
                    active.remove(key)
                return copy.deepcopy(resolved[key])
            if set(item) == {"$object"}:
                return {key: decode(child) for key, child in item["$object"]}
            if set(item) == {"$runs"}:
                return decode({"$table": {"columns": ["start", "end", "properties"], "rows": item["$runs"]}})
            if set(item) == {"$xml"}:
                row = item["$xml"]
                if not isinstance(row, list) or not 1 <= len(row) <= len(_XML_FIELDS):
                    raise ar.AstraReviewError("Invalid subscription XML evidence row")
                node = {}
                for key, child in zip(_XML_FIELDS, row):
                    if child is not None:
                        node[key] = ([decode({"$xml": nested}) if isinstance(nested, list) else decode(nested)
                                      for nested in child] if key == "children" else decode(child))
                return node
            if set(item) == {"$table"}:
                table = item["$table"]
                columns = table["columns"]
                if (len(columns) != len(set(columns))
                        or any(len(row) != len(columns) for row in table["rows"])):
                    raise ar.AstraReviewError("Invalid subscription evidence table")
                return [{key: decode(child) for key, child in zip(columns, row)}
                        for row in table["rows"]]
            return {key: decode(child) for key, child in item.items()}
        if isinstance(item, list):
            return [decode(child) for child in item]
        return item

    return decode(packed["data"])
