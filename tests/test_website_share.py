from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from docproof.website.export import ExportError
from docproof.website.share import shareable_html

REPO = Path(__file__).resolve().parents[1]


def test_review_embeds_all_pages_once_per_asset_and_cannot_close_json_script(tmp_path):
    spec = json.loads((REPO / "config/website/demo/site.json").read_text())
    spec["author"]["name"] = 'Test </script><img src="https://bad.example"> & Author'
    rows = json.loads((REPO / "config/website/demo/assets.json").read_text())
    unused = {**rows[0], "id": "unused-private-image"}
    shell = tmp_path / "shell.html"
    shell.write_text('<script type="application/json">__DOCPROOF_SITE_DATA__</script>')
    paths = {a["id"]: REPO / "config/website/demo/assets" / a["filename"] for a in rows}
    html = shareable_html(spec, assets=rows + [unused], asset_paths=paths, shell_path=shell)
    assert html.count("</script>") == 1
    data = json.loads(html.split(">", 1)[1].rsplit("</script>", 1)[0])
    assert data["author"] == spec["author"]["name"]
    assert len(data["pages"]) == 5
    assert all(set(pages) == {"home", "about", "books", "contact"} for pages in data["pages"].values())
    assert set(data["assets"]) == {a["id"] for a in rows}
    assert "unused-private-image" not in html
    assert str(REPO) not in html
    for asset in rows:
        payload = data["assets"][asset["id"]].split(",", 1)[1]
        assert hashlib.sha256(base64.b64decode(payload)).hexdigest() == asset["sha256"]


def test_review_refuses_missing_unapproved_or_changed_images(tmp_path):
    spec = json.loads((REPO / "config/website/demo/site.json").read_text())
    rows = json.loads((REPO / "config/website/demo/assets.json").read_text())
    with pytest.raises(ExportError, match="missing or unapproved"):
        shareable_html(spec)
    with pytest.raises(ExportError, match="local source"):
        shareable_html(spec, assets=rows)
    wrong = tmp_path / "wrong-image.jpg"
    wrong.write_bytes(b"changed")
    with pytest.raises(ExportError, match="does not match"):
        shareable_html(spec, assets=rows, asset_paths={a["id"]: wrong for a in rows})
