import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import gameio_addon as ga  # noqa: E402

CONFIG = {"id": "com.example.test", "name": "Test", "version": "1",
          "baseUrl": "https://addons.example.com/test/1", "extraHosts": []}


def row(**values):
    base = {"igdb_id": "1074", "platform": "n64", "kind": "http",
            "url": "https://downloads.example.com/game.z64", "_where": "test"}
    base.update(values)
    return base


def bencode(value):
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(value[k]) for k in sorted(value)) + b"e"


class SharedRulesTest(unittest.TestCase):
    def test_shard_matches_published_gameio_data(self):
        self.assertEqual(ga.shard_for("3378:n64"), "64")
        self.assertEqual(ga.shard_for("1074:n64"), "cf")

    def test_source_id_matches_gameio_server_export(self):
        # Pinned from the server exporter's canonical JSON: sorted keys, compact, UTF-8, trailing newline.
        locator = {"item": "example-archive-item", "path": "N64/Example Game (Europe).z64"}
        self.assertEqual(ga.source_id("internet_archive", locator),
                         "9407e3885d04d6c29bf698cc113796a5feacd2352035dba15196d299267ae8c0")

    def test_keys_need_positive_id_and_lowercase_slug(self):
        for igdb, platform in ((0, "n64"), (-1, "n64"), (1, "N64"), (1, "-n64"), (1, "")):
            with self.assertRaises(ga.AddonError):
                ga.lookup_key(igdb, platform)


class BuildTest(unittest.TestCase):
    def build(self, *rows, **config):
        return ga.build(dict(CONFIG, **config), list(rows))

    def test_writes_manifest_and_all_256_shards(self):
        files = self.build(row())
        self.assertEqual(len(files), 257)
        manifest = json.loads(files["manifest.json"])
        ga.validate_manifest(manifest)
        self.assertEqual(manifest["allowedHosts"], ["addons.example.com", "downloads.example.com"])
        self.assertEqual(manifest["lookup"]["urlTemplate"], "https://addons.example.com/test/1/{shard}.json")
        shard = json.loads(files["cf.json"])
        self.assertEqual(shard["entries"]["1074:n64"][0]["filename"], "game.z64")
        for name, content in files.items():
            if name != "manifest.json":
                ga.validate_shard(content, manifest, name[:2])

    def test_torrent_row_normalises_hash_and_path(self):
        files = self.build(row(kind="torrent", url="", info_hash="A" * 40, file_index="3", path="/Set/Game.zip"))
        source = json.loads(files["cf.json"])["entries"]["1074:n64"][0]
        self.assertEqual(source["locator"], {"infoHash": "a" * 40, "fileIndex": 3, "path": "Set/Game.zip"})
        self.assertEqual(source["filename"], "Game.zip")

    def test_identical_rows_are_merged_but_conflicts_fail(self):
        files = self.build(row(), row())
        self.assertEqual(len(json.loads(files["cf.json"])["entries"]["1074:n64"]), 1)
        with self.assertRaises(ga.AddonError):
            self.build(row(size="10"), row(size="20"))

    def test_rejects_unsafe_sources(self):
        bad = [
            row(url="http://downloads.example.com/game.z64"),
            row(url="https://user:pw@downloads.example.com/game.z64"),
            row(url="https://downloads.example.com/game.z64#part"),
            row(kind="internet_archive", url="", ia_item="item", path="../secret"),
            row(kind="internet_archive", url="", ia_item="bad item", path="game.z64"),
            row(kind="torrent", url="", info_hash="abc", file_index="1", path="a.zip"),
            row(kind="torrent", url="", info_hash="a" * 40, file_index="-1", path="a.zip"),
            row(filename="dir/game.z64"),
            row(size="0"),
            row(md5="xyz"),
            row(kind="ftp"),
        ]
        for values in bad:
            with self.subTest(values=values), self.assertRaises(ga.AddonError):
                self.build(values)

    def test_rejects_bad_config(self):
        for config in ({"id": "Has Space"}, {"baseUrl": "http://addons.example.com/x"}, {"name": " "}):
            with self.subTest(config=config), self.assertRaises(ga.AddonError):
                self.build(row(), **config)

    def test_too_many_sources_for_one_game(self):
        rows = [row(url=f"https://downloads.example.com/{n}.z64") for n in range(ga.MAX_SOURCES_PER_GAME + 1)]
        with self.assertRaises(ga.AddonError):
            self.build(*rows)

    def test_output_directory_is_never_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "1"
            ga.write_new_dir(target, {"a.json": b"{}"})
            with self.assertRaises(ga.AddonError):
                ga.write_new_dir(target, {"a.json": b"{}"})


class ValidateTest(unittest.TestCase):
    def manifest(self, **changes):
        manifest = json.loads(ga.build(CONFIG, [row()])["manifest.json"])
        manifest.update(changes)
        return manifest

    def test_manifest_rules(self):
        bad = [
            {"schemaVersion": 2}, {"adapter": "other"}, {"id": "-x"}, {"name": ""},
            {"allowedHosts": []}, {"allowedHosts": ["https://addons.example.com"]},
            {"allowedHosts": ["addons.example.com", "addons.example.com"]},
            {"allowedHosts": ["Addons.example.com"]},
            {"lookup": {"key": "igdbId:platformSlug", "partition": "sha256-prefix-2",
                        "urlTemplate": "https://addons.example.com/{shard}/{shard}.json"}},
            {"lookup": {"key": "igdbId:platformSlug", "partition": "sha256-prefix-2",
                        "urlTemplate": "https://elsewhere.example.com/{shard}.json"}},
        ]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ga.AddonError):
                ga.validate_manifest(self.manifest(**change))

    def test_key_in_wrong_shard_is_rejected(self):
        manifest = self.manifest()
        content = json.dumps({"schemaVersion": 1, "entries": {"1074:n64": []}}).encode()
        with self.assertRaises(ga.AddonError):
            ga.validate_shard(content, manifest, "00")
        ga.validate_shard(content, manifest, "cf")

    def test_oversized_shard_is_rejected(self):
        with self.assertRaises(ga.AddonError):
            ga.validate_shard(b" " * (ga.MAX_SHARD_BYTES + 1), self.manifest(), "00")

    def test_cli_build_then_validate_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "build"
            example = ROOT / "examples" / "starter"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ga.main(["build", "--config", str(example / "addon.json"),
                                          "--sources", str(example / "sources.csv"), "--output", str(out)]), 0)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(ga.main(["validate", str(out / "manifest.json")]), 0)
            self.assertIn("all 256 shards ok", stdout.getvalue())
            (out / "7f.json").unlink()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ga.main(["validate", str(out / "manifest.json")]), 1)


class TorrentTest(unittest.TestCase):
    def test_lists_files_with_zero_based_index_and_v1_hash(self):
        info = {b"name": b"Collection", b"piece length": 16384, b"pieces": b"x" * 20,
                b"files": [{b"length": 5, b"path": [b"Set", b"a.zip"]},
                           {b"length": 7, b"path": [b"Set", b"b.zip"]}]}
        data = bencode({b"announce": b"udp://tracker.example", b"info": info})
        info_hash, name, files = ga.torrent_files(data)
        self.assertEqual(info_hash, hashlib.sha1(bencode(info)).hexdigest())
        self.assertEqual(name, "Collection")
        self.assertEqual(files, [(0, "Set/a.zip", 5), (1, "Set/b.zip", 7)])

    def test_single_file_torrent(self):
        info = {b"name": b"game.zip", b"length": 9, b"piece length": 16384, b"pieces": b"x" * 20}
        _, _, files = ga.torrent_files(bencode({b"info": info}))
        self.assertEqual(files, [(0, "game.zip", 9)])

    def test_v2_only_torrent_is_rejected(self):
        info = {b"name": b"game", b"meta version": 2, b"file tree": {}, b"piece length": 16384}
        with self.assertRaises(ga.AddonError):
            ga.torrent_files(bencode({b"info": info}))


if __name__ == "__main__":
    unittest.main()
