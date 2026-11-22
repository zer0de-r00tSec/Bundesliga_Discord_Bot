"""Wappen: URL-Umbau, Konvertierung, Nachladen, Platzhalter."""
from __future__ import annotations

import asyncio
import io

import httpx
import pytest
from PIL import Image

from app import crests, image_composer
from app.matchdata import parse_match
from tests.fixtures import match


def _png(w=64, h=64, color=(200, 0, 0, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def crest_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(image_composer, "CRESTS_DIR", tmp_path)
    monkeypatch.setattr(crests, "CRESTS_DIR", tmp_path)
    monkeypatch.setattr(crests, "get_crest_path", image_composer.get_crest_path)
    monkeypatch.setattr(crests, "REQUEST_PAUSE", 0)
    crests._failed.clear()
    image_composer.clear_cache()
    yield tmp_path
    image_composer.clear_cache()


def test_wikimedia_thumbnail_url():
    url = "https://upload.wikimedia.org/wikipedia/commons/5/55/Logo_Energie_Cottbus.svg"
    assert crests.wikimedia_png_url(url) == (
        "https://upload.wikimedia.org/wikipedia/commons/thumb/5/55/Logo_Energie_Cottbus.svg/"
        "500px-Logo_Energie_Cottbus.svg.png")
    assert crests.wikimedia_png_url("https://i.imgur.com/apFwbYZ.png") is None
    assert crests.wikimedia_png_url("https://upload.wikimedia.org/wikipedia/de/a/ab/Logo.png") is None


def test_to_png_scales_down_and_keeps_alpha():
    out = crests.to_png(_png(1200, 600))
    img = Image.open(io.BytesIO(out))
    assert img.format == "PNG" and img.mode == "RGBA" and max(img.size) == crests.CREST_SIZE


def test_ensure_downloads_missing_and_keeps_existing(crest_dir):
    (crest_dir / "54.png").write_bytes(_png())
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert "StadionEcho" in request.headers["user-agent"]
        return httpx.Response(200, content=_png(300, 300), headers={"content-type": "image/png"})

    http = crests.new_http_client(transport=httpx.MockTransport(handler))
    teams = {93: ("Energie Cottbus", "https://example.invalid/93.png"),
             54: ("Hertha BSC", "https://example.invalid/54.png")}
    new = asyncio.run(crests.ensure_crests(teams, http=http))
    assert new == [93] and calls == ["https://example.invalid/93.png"]
    assert (crest_dir / "93.png").exists()
    assert image_composer._crest(93, 120) is not None
    # zweiter Lauf: nichts mehr zu tun
    assert asyncio.run(crests.ensure_crests(teams, http=http)) == []


def test_failed_download_is_not_retried_immediately(crest_dir):
    n = {"calls": 0}

    def handler(request):
        n["calls"] += 1
        return httpx.Response(403)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    teams = {36: ("VfL Osnabrück", "https://example.invalid/36.png")}
    assert asyncio.run(crests.ensure_crests(teams, http=http)) == []
    assert asyncio.run(crests.ensure_crests(teams, http=http)) == []
    assert n["calls"] == 1
    assert asyncio.run(crests.ensure_crests(teams, http=http, force=True)) == []
    assert n["calls"] == 2


def test_cache_cleared_after_download(crest_dir):
    """Composer hat 'fehlt' gecacht → nach dem Laden muss das Wappen sofort da sein."""
    assert image_composer._crest(93, 120) is None
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=_png())))
    asyncio.run(crests.ensure_crests({93: ("Energie Cottbus", "https://example.invalid/x.png")}, http=http))
    assert image_composer._crest(93, 120) is not None


def test_teams_from_snaps():
    snap = parse_match(match(1, 40, 7))
    teams = crests.teams_from_snaps([snap])
    assert teams[40][1] == "https://example.invalid/40.png" and set(teams) == {40, 7}


def test_compose_uses_placeholder_for_one_missing_crest(crest_dir):
    (crest_dir / "54.png").write_bytes(_png())
    data = image_composer._compose(93, 54, "Energie Cottbus", "Hertha BSC", "22.11. 13:30", "2. BL", 120, "Anpfiff!")
    assert data and Image.open(io.BytesIO(data)).format == "PNG"
    assert image_composer._compose(1, 2, "A", "B", "", "", 120, "x") is None


def test_initials():
    assert image_composer.initials("Energie Cottbus") == "EC"
    assert image_composer.initials("Hertha BSC") == "HB"
    assert image_composer.initials("1. FC Kaiserslautern") == "K"
