"""Read-only MCP access to the exact, hash-frozen files of one book review."""

import argparse
import hashlib
import json
from pathlib import Path


def write_manifest(root: Path, documents: dict[str, Path], images: dict[str, Path]) -> Path:
    """Register only generated evidence inside this job, never arbitrary document text paths."""
    root = root.resolve()
    files = {}
    for kind, items in (('json', documents), ('image', images)):
        for key, supplied in items.items():
            path = supplied.resolve()
            if not path.is_relative_to(root):
                raise ValueError('Evidence must be materialized inside its job folder.')
            if key in files or path.suffix.lower() not in ({'.json'} if kind == 'json' else {'.png', '.jpg', '.jpeg'}):
                raise ValueError('Invalid evidence registration.')
            raw = path.read_bytes()
            files[key] = {'path': str(path), 'kind': kind, 'bytes': len(raw),
                          'sha256': hashlib.sha256(raw).hexdigest()}
    manifest = root / 'astra-evidence.json'
    manifest.write_text(json.dumps({'version': 1, 'files': files}, ensure_ascii=False), encoding='utf-8')
    return manifest


class EvidenceStore:
    def __init__(self, manifest: Path):
        value = json.loads(manifest.read_text(encoding='utf-8'))
        if value.get('version') != 1 or not isinstance(value.get('files'), dict):
            raise ValueError('Invalid evidence registry.')
        self.files = value['files']

    def data(self, evidence_id: str) -> tuple[dict, bytes]:
        if evidence_id not in self.files:
            raise ValueError('Unknown evidence ID; arbitrary filesystem paths are not accepted.')
        row = self.files[evidence_id]
        path = Path(row['path'])
        if path.suffix.lower() not in {'.json', '.png', '.jpg', '.jpeg'}:
            raise ValueError('Unsupported evidence format.')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('The frozen evidence changed; this review must stop.')
        return row, raw

    def index(self) -> dict:
        return {key: {k: v for k, v in row.items() if k != 'path'} for key, row in self.files.items()}

    def read_text(self, evidence_id: str, start: int = 0, characters: int = 40000) -> dict:
        row, raw = self.data(evidence_id)
        if row['kind'] != 'json' or type(start) is not int or type(characters) is not int or start < 0 or not 1 <= characters <= 80000:
            raise ValueError('Read a JSON evidence ID using a valid bounded character range.')
        content = raw.decode('utf-8')
        end = min(start + characters, len(content))
        return {'text': content[start:end], 'start': start, 'total_characters': len(content),
                'next_start': end if end < len(content) else None}

    def field(self, evidence_id: str, pointer: str = '', start: int = 0, limit: int = 20) -> dict:
        row, raw = self.data(evidence_id)
        if row['kind'] != 'json' or type(start) is not int or type(limit) is not int or start < 0 or not 1 <= limit <= 80000:
            raise ValueError('Invalid JSON field request.')
        value = json.loads(raw)
        if pointer:
            if not pointer.startswith('/'):
                raise ValueError('Use a JSON Pointer beginning with /.')
            for key in pointer[1:].split('/'):
                key = key.replace('~1', '/').replace('~0', '~')
                value = value[int(key)] if isinstance(value, list) else value[key]
        if isinstance(value, (list, str)):
            end = min(start + limit, len(value))
            return {'value': value[start:end], 'total': len(value), 'next_start': end if end < len(value) else None}
        if isinstance(value, dict):
            return {'keys': list(value), 'instruction': 'Read child values with their JSON Pointer; no fields have been omitted from the underlying evidence.'}
        return {'value': value}

    def find(self, evidence_id: str, text: str, story_id: str = '', start: int = 0) -> dict:
        row, raw = self.data(evidence_id)
        if row['kind'] != 'json' or not text or len(text) > 4000 or start < 0:
            raise ValueError('Use a nonempty bounded exact search string.')
        hits = []
        for story in json.loads(raw).get('stories', []):
            if story_id and story['id'] != story_id:
                continue
            content = story['text']
            offset = 0
            while (offset := content.find(text, offset)) >= 0:
                hits.append({'story_id': story['id'], 'offset': offset,
                             'context': content[max(0, offset-300):offset+len(text)+300]})
                offset += len(text)
        end = min(start+20, len(hits))
        return {'matches': hits[start:end], 'total_matches': len(hits),
                'next_start': end if end < len(hits) else None}

    def find_many(self, evidence_id: str, searches: list[str], story_id: str = '') -> list[dict]:
        if (not isinstance(searches, list) or not 1 <= len(searches) <= 20
                or any(not isinstance(text, str) or not text or len(text) > 4000 for text in searches)):
            raise ValueError('Supply between 1 and 20 bounded exact search strings.')
        results = []
        for text in searches:
            found = self.find(evidence_id, text, story_id)
            results.append({'text': text, 'matches': found['matches'][:3],
                            'total_matches': found['total_matches'],
                            'next_start': 3 if found['total_matches'] > 3 else None})
        return results


def create_server(manifest: Path):
    from mcp.server.fastmcp import FastMCP, Image
    from mcp.types import ToolAnnotations
    store = EvidenceStore(manifest)
    server = FastMCP('docproof_evidence', instructions=(
        'Read-only frozen correction evidence and book snapshots. No filesystem paths, commands, credentials, '
        'network access, or modifications are accepted. Use the evidence IDs from list_evidence. '
        'Read all required sources and every required review image before declaring coverage.'))
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=annotations)
    def list_evidence() -> dict:
        """List every available evidence ID, format, byte size and content hash."""
        return store.index()

    @server.tool(annotations=annotations)
    def read_evidence(evidence_id: str, start: int = 0, characters: int = 40000) -> dict:
        """Read complete JSON evidence in bounded text chunks; follow next_start until null."""
        return store.read_text(evidence_id, start, characters)

    @server.tool(annotations=annotations)
    def read_json_field(evidence_id: str, pointer: str = '', start: int = 0, limit: int = 20) -> dict:
        """Read exact JSON fields, array rows, or text ranges without shell commands. /stories/0/text selects a story's complete text; page through it with start/limit. Empty pointer lists top-level keys."""
        return store.field(evidence_id, pointer, start, limit)

    @server.tool(annotations=annotations)
    def find_in_stories(evidence_id: str, text: str, story_id: str = '', start: int = 0) -> dict:
        """Find exact native story anchors and their match counts with surrounding context. Case-sensitive, no regex, no replacements; paginates 20 matches at a time."""
        return store.find(evidence_id, text, story_id, start)

    @server.tool(annotations=annotations)
    def find_story_anchors(evidence_id: str, searches: list[str], story_id: str = '') -> list[dict]:
        """Search up to 20 exact anchors together. Returns full match counts and at most 3 contexts each; use find_in_stories with next_start for additional matches. No fuzzy matches or edits."""
        return store.find_many(evidence_id, searches, story_id)

    @server.tool(annotations=annotations)
    def view_evidence_image(evidence_id: str) -> Image:
        """Visually inspect one registered PNG/JPEG page or source image, with no file or network writes."""
        row, raw = store.data(evidence_id)
        if row['kind'] != 'image':
            raise ValueError('Select an image evidence ID.')
        return Image(data=raw, format='png' if Path(row['path']).suffix.lower() == '.png' else 'jpeg')

    @server.tool(annotations=annotations)
    def view_evidence_images(evidence_ids: list[str]) -> list:
        """View 1 to 8 registered images in a single call, each with its ID label. Group before/after pairs in order; preserves every original image and its resolution."""
        if not 1 <= len(evidence_ids) <= 8 or len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError('Select between 1 and 8 distinct image evidence IDs.')
        # Validate the whole batch before returning any image.
        pictures = [view_evidence_image(evidence_id) for evidence_id in evidence_ids]
        return [item for evidence_id, picture in zip(evidence_ids, pictures) for item in (evidence_id, picture)]

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    create_server(args.manifest).run(transport='stdio')


if __name__ == '__main__':
    main()
