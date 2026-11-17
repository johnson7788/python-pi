"""The slides-deck skill's builder is shipped code, not a prompt.

It runs with whatever ``python3`` a deployment has, so the only dependencies it may
use are the standard library.  These tests run it the way an agent would and assert
the two promises the skill makes: a valid pptx comes out, and a broken spec fails
loudly instead of producing a half-deck.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL = REPO_ROOT / "template" / "skills" / "slides-deck"
SCRIPT = SKILL / "scripts" / "build_deck.py"
EXAMPLE = SKILL / "examples" / "demo.json"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_doctor_reports_theme_asset():
    result = run("doctor")
    assert result.returncode == 0, result.stderr
    assert "theme asset" in result.stdout


def test_builds_the_example_deck(tmp_path: Path):
    out = tmp_path / "demo.pptx"
    result = run("build", str(EXAMPLE), "-o", str(out))

    assert result.returncode == 0, result.stderr
    assert out.is_file()
    assert "11 pages" in result.stdout

    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        slides = [name for name in names if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
        assert len(slides) == 11
        content_types = archive.read("[Content_Types].xml").decode()
        assert content_types.count("presentationml.slide+xml") == 11
        presentation = archive.read("ppt/presentation.xml").decode()
        assert presentation.count("<p:sldId ") == 11
        # cover + image_text carry pictures, and the media parts are declared
        assert sum(1 for name in names if name.startswith("ppt/media/")) == 2
        assert 'Extension="jpeg"' in content_types
        # 16:9 canvas, and the theme carries the deck font rather than 宋体
        assert 'cx="12192000" cy="6858000"' in presentation
        theme = archive.read("ppt/theme/theme1.xml").decode()
        assert 'script="Hans"' in theme
        assert "宋体" not in theme
        # PowerPoint repairs files with duplicate shape ids inside one slide.
        import re

        for name in slides:
            ids = re.findall(r'<p:cNvPr id="(\d+)"', archive.read(name).decode())
            assert len(ids) == len(set(ids)), f"duplicate shape id in {name}"
            assert "1" in ids  # the spTree group keeps id 1


def test_theme_font_override_reaches_the_theme(tmp_path: Path):
    spec = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    spec["theme"]["fonts"] = {"latin": "Arial", "ea": "PingFang SC"}
    for page in spec["pages"]:
        if page.get("image"):
            page["image"] = str(EXAMPLE.parent / page["image"])
    path = tmp_path / "deck.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "deck.pptx"

    result = run("build", str(path), "-o", str(out))
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(out) as archive:
        theme = archive.read("ppt/theme/theme1.xml").decode()
    assert theme.count('typeface="PingFang SC"') >= 2


def test_image_page_embeds_media_and_relationship(tmp_path: Path):
    spec = {
        "title": "t",
        "pages": [
            {"layout": "cover", "title": "封面", "image": str(EXAMPLE.parent / "assets" / "cover.jpg")},
        ],
    }
    path = tmp_path / "deck.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "deck.pptx"

    result = run("build", str(path), "-o", str(out))
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(out) as archive:
        assert "ppt/media/image1.jpeg" in archive.namelist()
        slide = archive.read("ppt/slides/slide1.xml").decode()
        assert "<p:pic>" in slide and 'r:embed="rId2"' in slide
        rels = archive.read("ppt/slides/_rels/slide1.xml.rels").decode()
        assert "relationships/image" in rels and "../media/image1.jpeg" in rels
        assert 'Extension="jpeg"' in archive.read("[Content_Types].xml").decode()


def test_unknown_layout_fails_loudly(tmp_path: Path):
    spec = {"title": "t", "pages": [{"layout": "carousel", "title": "x"}]}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(spec), encoding="utf-8")

    check = run("check", str(path))
    assert check.returncode == 1
    assert "unknown layout" in check.stdout

    out = tmp_path / "bad.pptx"
    build = run("build", str(path), "-o", str(out))
    assert build.returncode == 1
    assert not out.exists()


def test_missing_required_items_is_an_error(tmp_path: Path):
    spec = {"title": "t", "pages": [{"layout": "timeline", "title": "路线图"}]}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(spec), encoding="utf-8")

    result = run("check", str(path))
    assert result.returncode == 1
    assert "requires 'items'" in result.stdout
