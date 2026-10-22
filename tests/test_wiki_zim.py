"""Wikipedia のオフライン版(`app/wiki_zim.py`)。小さな ZIM をその場で作って引く。"""
from __future__ import annotations

import asyncio
import hashlib
import io

import httpx
import pytest
from PIL import Image

from app import thumbs, wiki_zim
from tests.test_thumbs import serve  # noqa: F401  (外へ出ずに応答を差し替える)

writer = pytest.importorskip("libzim.writer")

COMMONS = "_assets_/" + hashlib.md5(b"https://upload.wikimedia.org/wikipedia/commons/").hexdigest()


def webp(width=500, height=375) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (30, 120, 30)).save(out, "WEBP")
    return out.getvalue()


class _Item(writer.Item):
    def __init__(self, path: str, data: bytes, mimetype: str = "image/webp"):
        super().__init__()
        self.path, self.data, self.mimetype = path, data, mimetype

    def get_path(self):
        return self.path

    def get_title(self):
        return ""

    def get_mimetype(self):
        return self.mimetype

    def get_contentprovider(self):
        return writer.StringProvider(self.data)

    def get_hints(self):
        return {writer.Hint.FRONT_ARTICLE: False}


@pytest.fixture
def zim(tmp_path, monkeypatch):
    """`CHIEZO_ZIM_DIR` に、渡した絵だけを入れた ZIM を置く(作り手の mwoffliner と同じ名前の決まり)。"""
    folder = tmp_path / "zim"
    folder.mkdir()
    monkeypatch.setenv("CHIEZO_ZIM_DIR", str(folder))

    def make(items: dict[str, bytes], name="wikipedia_ja_all_maxi_2026-06.zim"):
        monkeypatch.setenv("CHIEZO_ZIM_DIR", str(folder))
        with writer.Creator(str(folder / name)).config_indexing(False, "jpn") as creator:
            creator.add_metadata("Date", "2026-06-07")
            for path, data in items.items():
                creator.add_item(_Item(path, data))
        return folder / name

    return make


class TestAssetNames:
    def test_japanese_is_decoded_and_reserved_characters_stay_encoded(self):
        # ZIM の中の名前は URL の名前を decodeURI した形(日本語は戻し、`,` は %2C のまま)
        url = "https://upload.wikimedia.org/wikipedia/commons/e/e4/%E7%AD%91%E5%89%8D%E7%85%AE%2C_1.jpg"
        assert wiki_zim.asset_names(url)[0] == f"{COMMONS}/筑前煮%2C_1.jpg"

    def test_raw_commas_are_encoded_like_the_zim(self):
        url = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Home,_Greenville.JPG"
        assert wiki_zim.asset_names(url)[0] == f"{COMMONS}/Home%2C_Greenville.JPG"

    def test_scaled_copies_point_at_the_same_picture(self):
        url = "https://upload.wikimedia.org/wikipedia/commons/thumb/5/52/Kakiage.jpg/330px-Kakiage.jpg"
        assert wiki_zim.asset_names(url)[0] == f"{COMMONS}/Kakiage.jpg"

    def test_each_project_has_its_own_folder(self):
        url = "https://upload.wikimedia.org/wikipedia/ja/5/52/Logo.png"
        ja = hashlib.md5(b"https://upload.wikimedia.org/wikipedia/ja/").hexdigest()
        assert wiki_zim.asset_names(url)[0] == f"_assets_/{ja}/Logo.png"

    def test_other_hosts_are_not_looked_up(self):
        assert wiki_zim.asset_names("https://example.org/a.jpg") == []


class TestImage:
    def test_reads_the_picture_without_unpacking(self, zim):
        data = webp()
        zim({f"{COMMONS}/筑前煮.jpg": data})

        url = "https://upload.wikimedia.org/wikipedia/commons/e/e4/%E7%AD%91%E5%89%8D%E7%85%AE.jpg"
        assert wiki_zim.image(url) == data
        assert wiki_zim.image("https://upload.wikimedia.org/wikipedia/commons/a/ab/Nope.jpg") is None

    def test_nothing_placed_means_nothing_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHIEZO_ZIM_DIR", str(tmp_path / "none"))
        assert wiki_zim.image("https://upload.wikimedia.org/wikipedia/commons/a/ab/A.jpg") is None
        assert wiki_zim.status() == []

    def test_a_replaced_file_is_opened_again(self, zim):
        url = "https://upload.wikimedia.org/wikipedia/commons/a/ab/New.jpg"
        zim({f"{COMMONS}/Old.jpg": webp()}, name="wikipedia_ja_all_maxi_2026-01.zim")
        assert wiki_zim.image(url) is None

        zim({f"{COMMONS}/New.jpg": webp()})
        assert wiki_zim.image(url) is not None
        assert [z["name"] for z in wiki_zim.status()] == [
            "wikipedia_ja_all_maxi_2026-06.zim",
            "wikipedia_ja_all_maxi_2026-01.zim",
        ]

    def test_status_tells_what_is_placed(self, zim):
        zim({f"{COMMONS}/A.jpg": webp()})
        [placed] = wiki_zim.status()
        assert placed["name"] == "wikipedia_ja_all_maxi_2026-06.zim"
        assert placed["date"] == "2026-06-07"
        assert placed["media"] == 1


class TestThumbs:
    def test_pictures_in_the_offline_copy_are_not_fetched(self, serve, zim):  # noqa: F811
        # Wikimedia は作り置きの無い縮小版を作らせる頼みを絞る。オフライン版にあれば外へ出ない
        zim({f"{COMMONS}/Kakiage.jpg": webp()})
        calls = serve({})
        items = [{"title": "かき揚げ", "image": "https://upload.wikimedia.org/wikipedia/commons/5/52/Kakiage.jpg"}]

        made = asyncio.run(thumbs.attach(thumbs.normalize(True), items, {}, False, caller="写真の依頼"))

        assert made == 1
        assert calls == []
        assert items[0]["extra"]["thumb"]
        assert thumbs.recent_log()[0]["result"] == "オフライン版から取った"

    def test_pictures_missing_from_it_are_still_fetched(self, serve, zim):  # noqa: F811
        zim({f"{COMMONS}/Other.jpg": webp()})
        scaled = "https://upload.wikimedia.org/wikipedia/commons/thumb/5/52/Kakiage.jpg/330px-Kakiage.jpg"
        calls = serve({scaled: httpx.Response(200, content=webp(), headers={"content-type": "image/webp"})})
        items = [{"title": "かき揚げ", "image": "https://upload.wikimedia.org/wikipedia/commons/5/52/Kakiage.jpg"}]

        assert asyncio.run(thumbs.attach(thumbs.normalize(True), items, {}, False)) == 1
        assert calls == [scaled]
        assert thumbs.recent_log()[0]["result"] == "取った"

    def test_the_memory_page_says_whether_it_is_placed(self, serve, zim, tmp_path, monkeypatch):  # noqa: F811
        from app.views import admin

        monkeypatch.setenv("CHIEZO_ZIM_DIR", str(tmp_path / "none"))
        assert "置いていません" in admin._wiki_zim_section_html(None)

        zim({f"{COMMONS}/A.jpg": webp()})
        html = admin._wiki_zim_section_html(None)
        assert "wikipedia_ja_all_maxi_2026-06.zim" in html
        assert "2026-06-07" in html
