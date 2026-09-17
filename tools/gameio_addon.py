#!/usr/bin/env python3
"""Build, validate and inspect Gameio add-ons (catalog-shards-v1).

Standard library only. Run `python3 tools/gameio_addon.py --help`.
The validation rules mirror the Gameio Android client (AddonFormat.kt), so an
add-on that passes `validate` imports and resolves in the app.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ADAPTER = "catalog-shards-v1"
LOOKUP_KEY = "igdbId:platformSlug"
PARTITION = "sha256-prefix-2"
SHARD_COUNT = 256
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SHARD_BYTES = 1024 * 1024
MAX_SHARD_KEYS = 1000
MAX_SOURCES_PER_GAME = 200
MAX_ALLOWED_HOSTS = 64
KINDS = ("internet_archive", "http", "torrent")
USER_AGENT = "gameio-addon-tool/1.0"

ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")
BUILD_ID_PATTERN = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
KEY_PATTERN = re.compile(r"[1-9][0-9]*:[a-z0-9][a-z0-9-]{0,99}")
IA_ITEM = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}")
HEX_32 = re.compile(r"[a-fA-F0-9]{32}")
HEX_40 = re.compile(r"[a-fA-F0-9]{40}")
HOST_PATTERN = re.compile(
    r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)

# Platform slugs used by the Gameio catalog (GET /api/catalog platform_slugs).
PLATFORMS = {
    "atari2600": "Atari 2600",
    "dc": "Dreamcast",
    "gamegear": "Game Gear",
    "gb": "Game Boy",
    "gba": "Game Boy Advance",
    "gbc": "Game Boy Color",
    "genesis": "Genesis / Mega Drive",
    "n64": "Nintendo 64",
    "nds": "Nintendo DS",
    "neogeoaes": "Neo Geo AES",
    "nes": "NES",
    "ngc": "GameCube",
    "ps2": "PlayStation 2",
    "ps3": "PlayStation 3",
    "psp": "PlayStation Portable",
    "psx": "PlayStation",
    "saturn": "Saturn",
    "sega32": "Sega 32X",
    "segacd": "Sega CD",
    "sms": "Master System",
    "snes": "SNES",
    "tg16": "TurboGrafx-16",
    "wii": "Wii",
    "xbox": "Xbox",
    "xbox360": "Xbox 360",
}

SOURCE_COLUMNS = [
    "igdb_id", "platform", "kind", "filename",
    "url", "ia_item", "path", "info_hash", "file_index",
    "size", "md5", "sha1", "region",
]


class AddonError(ValueError):
    pass


# ---------------------------------------------------------------- shared rules

def lookup_key(igdb_id: Any, platform: str) -> str:
    key = f"{igdb_id}:{platform}"
    if not KEY_PATTERN.fullmatch(key):
        raise AddonError(f"invalid game key {key!r}: need a positive IGDB id and a lowercase platform slug")
    return key


def shard_for(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:2]


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def source_id(kind: str, locator: dict[str, Any]) -> str:
    return hashlib.sha256(json_bytes({"kind": kind, "locator": locator})).hexdigest()


def has_control(value: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" for ch in value)


def valid_path(path: Any) -> bool:
    return (
        isinstance(path, str) and path.strip() != "" and len(path) <= 2000
        and not path.startswith("/") and "\\" not in path and not has_control(path)
        and all(part not in ("", ".", "..") for part in path.split("/"))
    )


def valid_host(host: Any) -> bool:
    return isinstance(host, str) and bool(HOST_PATTERN.fullmatch(host)) and "." in host


def approved_url(value: Any, allowed_hosts: list[str], what: str) -> str:
    if not isinstance(value, str) or has_control(value) or " " in value:
        raise AddonError(f"{what}: not a valid URL")
    parts = urllib.parse.urlsplit(value)
    try:
        _ = parts.port
    except ValueError:
        raise AddonError(f"{what}: invalid port") from None
    if parts.scheme != "https":
        raise AddonError(f"{what}: must use https://")
    if parts.username is not None or parts.password is not None:
        raise AddonError(f"{what}: must not contain a username or password")
    if parts.fragment or value.endswith("#"):
        raise AddonError(f"{what}: must not contain a #fragment")
    host = (parts.hostname or "").lower()
    if host not in allowed_hosts:
        raise AddonError(f"{what}: host {host!r} is not in allowedHosts {allowed_hosts}")
    return host


# ------------------------------------------------------------------ validation

def require(condition: bool, message: str) -> None:
    if not condition:
        raise AddonError(message)


def validate_manifest(manifest: Any) -> None:
    require(isinstance(manifest, dict), "manifest must be a JSON object")
    for field, kind in (("schemaVersion", int), ("id", str), ("name", str), ("version", str),
                        ("adapter", str), ("lookup", dict), ("allowedHosts", list)):
        require(isinstance(manifest.get(field), kind) and not isinstance(manifest.get(field), bool),
                f"manifest.{field} is missing or has the wrong type")
    lookup = manifest["lookup"]
    for field in ("key", "partition", "urlTemplate"):
        require(isinstance(lookup.get(field), str), f"manifest.lookup.{field} is missing")
    require(manifest["schemaVersion"] == SCHEMA_VERSION, "schemaVersion must be 1")
    require(manifest["adapter"] == ADAPTER, f"adapter must be {ADAPTER!r}")
    require(lookup["key"] == LOOKUP_KEY, f"lookup.key must be {LOOKUP_KEY!r}")
    require(lookup["partition"] == PARTITION, f"lookup.partition must be {PARTITION!r}")
    require(bool(ID_PATTERN.fullmatch(manifest["id"])), "id: letters, digits, '.', '_' or '-', max 128, starting with a letter or digit")
    name = manifest["name"]
    require(name.strip() != "" and len(name) <= 100 and not has_control(name), "name: 1-100 characters, no control characters")
    version = manifest["version"]
    require(version.strip() != "" and len(version) <= 100, "version: 1-100 characters")
    hosts = manifest["allowedHosts"]
    require(0 < len(hosts) <= MAX_ALLOWED_HOSTS, f"allowedHosts: 1-{MAX_ALLOWED_HOSTS} entries")
    require(len(set(hosts)) == len(hosts), "allowedHosts: duplicate entries")
    for host in hosts:
        require(valid_host(host), f"allowedHosts: {host!r} must be a lowercase host name only (no scheme, port or path)")
    template = lookup["urlTemplate"]
    require(len(template) <= 2048, "urlTemplate: max 2048 characters")
    require(template.count("{shard}") == 1, "urlTemplate must contain {shard} exactly once")
    replaced = template.replace("{shard}", "00")
    require("{" not in replaced and "}" not in replaced, "urlTemplate: only the {shard} placeholder is allowed")
    approved_url(replaced, hosts, "urlTemplate")


def validate_source(source: Any, manifest: dict[str, Any], where: str) -> None:
    require(isinstance(source, dict), f"{where}: source must be an object")
    for field in ("id", "kind", "filename"):
        require(isinstance(source.get(field), str), f"{where}: {field} is missing")
    require(isinstance(source.get("locator"), dict), f"{where}: locator is missing")
    require(bool(ID_PATTERN.fullmatch(source["id"])), f"{where}: invalid id")
    filename = source["filename"]
    require(filename.strip() != "" and len(filename) <= 500 and filename not in (".", "..")
            and "/" not in filename and "\\" not in filename and not has_control(filename),
            f"{where}: filename must be a plain file name (no folders), max 500 characters")
    size = source.get("size")
    require(size is None or (isinstance(size, int) and not isinstance(size, bool) and size > 0), f"{where}: size must be a positive integer")
    for field, pattern in (("md5", HEX_32), ("sha1", HEX_40)):
        value = source.get(field)
        require(value is None or (isinstance(value, str) and bool(pattern.fullmatch(value))), f"{where}: invalid {field}")
    region = source.get("region")
    require(region is None or (isinstance(region, str) and len(region) <= 100), f"{where}: region max 100 characters")
    loc = source["locator"]
    item, path, url, info_hash, index = (loc.get(k) for k in ("item", "path", "url", "infoHash", "fileIndex"))
    kind = source["kind"]
    if kind == "internet_archive":
        require(isinstance(item, str) and bool(IA_ITEM.fullmatch(item)), f"{where}: internet_archive needs locator.item (the archive.org identifier)")
        require(valid_path(path), f"{where}: internet_archive needs a relative locator.path inside the item")
        require(url is None and info_hash is None and index is None, f"{where}: internet_archive locator only takes item and path")
    elif kind == "http":
        approved_url(url, manifest["allowedHosts"], f"{where}: locator.url")
        require(item is None and path is None and info_hash is None and index is None, f"{where}: http locator only takes url")
    elif kind == "torrent":
        require(isinstance(info_hash, str) and bool(HEX_40.fullmatch(info_hash)), f"{where}: torrent needs a 40-character hex locator.infoHash (BitTorrent v1)")
        require(isinstance(index, int) and not isinstance(index, bool) and index >= 0, f"{where}: torrent needs locator.fileIndex >= 0")
        require(valid_path(path), f"{where}: torrent needs locator.path (file path inside the torrent)")
        require(item is None and url is None, f"{where}: torrent locator only takes infoHash, fileIndex and path")
    else:
        raise AddonError(f"{where}: kind must be one of {', '.join(KINDS)}")


def validate_shard(raw: bytes, manifest: dict[str, Any], shard: str) -> dict[str, Any]:
    require(len(raw) <= MAX_SHARD_BYTES, f"shard {shard}: larger than {MAX_SHARD_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AddonError(f"shard {shard}: not valid UTF-8 JSON ({exc})") from None
    require(isinstance(data, dict) and data.get("schemaVersion") == SCHEMA_VERSION, f"shard {shard}: schemaVersion must be 1")
    entries = data.get("entries")
    require(isinstance(entries, dict), f"shard {shard}: entries must be an object")
    require(len(entries) <= MAX_SHARD_KEYS, f"shard {shard}: more than {MAX_SHARD_KEYS} games")
    for key, sources in entries.items():
        require(bool(KEY_PATTERN.fullmatch(key)), f"shard {shard}: invalid key {key!r}")
        require(shard_for(key) == shard, f"shard {shard}: key {key!r} belongs in shard {shard_for(key)}")
        require(isinstance(sources, list), f"shard {shard}: {key} must map to a list")
        require(len(sources) <= MAX_SOURCES_PER_GAME, f"shard {shard}: {key} has more than {MAX_SOURCES_PER_GAME} sources")
        ids = [s.get("id") for s in sources if isinstance(s, dict)]
        require(len(set(ids)) == len(sources), f"shard {shard}: {key} has duplicate source ids")
        for n, source in enumerate(sources):
            validate_source(source, manifest, f"shard {shard} {key}[{n}]")
    return data


# ----------------------------------------------------------------- build input

def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AddonError(f"config not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise AddonError(f"{path}: invalid JSON ({exc})") from None
    for field in ("id", "name", "version", "baseUrl"):
        require(isinstance(config.get(field), str) and config[field].strip(), f"{path}: {field} is required")
    extra = config.get("extraHosts", [])
    require(isinstance(extra, list) and all(isinstance(h, str) for h in extra), f"{path}: extraHosts must be a list of host names")
    return config


def read_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        rows = json.loads(text)
        require(isinstance(rows, list), f"{path}: expected a JSON list of sources")
        return [dict(row, _where=f"{path.name}[{n}]") for n, row in enumerate(rows)]
    reader = csv.DictReader(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    unknown = set(reader.fieldnames or []) - set(SOURCE_COLUMNS)
    require(not unknown, f"{path}: unknown columns {sorted(unknown)}; allowed: {', '.join(SOURCE_COLUMNS)}")
    return [dict(row, _where=f"{path.name} line {n + 2}") for n, row in enumerate(reader)]


def blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def as_int(value: Any, where: str, field: str) -> int | None:
    if blank(value):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        raise AddonError(f"{where}: {field} must be a whole number") from None


def row_to_source(row: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    where = row.get("_where", "source")
    platform = str(row.get("platform", "")).strip()
    igdb_id = as_int(row.get("igdb_id"), where, "igdb_id")
    key = lookup_key(igdb_id, platform)
    if platform not in PLATFORMS:
        print(f"warning: {where}: platform {platform!r} is not a known Gameio catalog platform", file=sys.stderr)
    kind = str(row.get("kind", "")).strip()
    filename = str(row.get("filename", "")).strip()
    host = None
    if kind == "internet_archive":
        locator = {"item": str(row.get("ia_item", "")).strip(), "path": str(row.get("path", "")).strip()}
        if blank(filename):
            filename = locator["path"].rsplit("/", 1)[-1]
    elif kind == "http":
        url = str(row.get("url", "")).strip()
        locator = {"url": url}
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if blank(filename):
            filename = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    elif kind == "torrent":
        locator = {
            "infoHash": str(row.get("info_hash", "")).strip().lower(),
            "fileIndex": as_int(row.get("file_index"), where, "file_index"),
            "path": str(row.get("path", "")).strip().lstrip("/"),
        }
        if blank(filename):
            filename = locator["path"].rsplit("/", 1)[-1]
    else:
        raise AddonError(f"{where}: kind must be one of {', '.join(KINDS)}")
    entry: dict[str, Any] = {"id": source_id(kind, locator), "kind": kind, "locator": locator, "filename": filename}
    size = as_int(row.get("size"), where, "size")
    if size is not None:
        entry["size"] = size
    for field in ("md5", "sha1"):
        if not blank(row.get(field)):
            entry[field] = str(row[field]).strip().lower()
    if not blank(row.get("region")):
        entry["region"] = str(row["region"]).strip()
    return key, entry, host


def build(config: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, bytes]:
    addon_id = config["id"].strip()
    require(bool(BUILD_ID_PATTERN.fullmatch(addon_id)) and len(addon_id) <= 100,
            "id: use lowercase letters and digits separated by '.', '_' or '-' (for example com.example.homebrew)")
    base_url = config["baseUrl"].strip().rstrip("/")
    hosts = {urllib.parse.urlsplit(base_url).hostname or ""}
    hosts.update(h.strip().lower() for h in config.get("extraHosts", []))
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}
    pending: list[tuple[str, str, dict[str, Any]]] = []
    for row in rows:
        try:
            key, entry, host = row_to_source(row)
        except AddonError as exc:
            msg = str(exc)
            raise AddonError(msg if msg.startswith(row.get("_where", "")) else f"{row.get('_where')}: {msg}") from None
        if host:
            hosts.add(host)
        pending.append((row.get("_where", ""), key, entry))
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "id": addon_id,
        "name": config["name"].strip(),
        "version": config["version"].strip(),
        "adapter": ADAPTER,
        "lookup": {"key": LOOKUP_KEY, "partition": PARTITION, "urlTemplate": f"{base_url}/{{shard}}.json"},
        "allowedHosts": sorted(hosts),
    }
    validate_manifest(manifest)
    for where, key, entry in pending:
        validate_source(entry, manifest, where)
        group = grouped[shard_for(key)].setdefault(key, [])
        same = next((s for s in group if s["id"] == entry["id"]), None)
        if same is not None:
            require(same == entry, f"{where}: {key} lists the same file twice with different details")
            continue
        group.append(entry)
        require(len(group) <= MAX_SOURCES_PER_GAME, f"{key}: more than {MAX_SOURCES_PER_GAME} sources")
    files: dict[str, bytes] = {}
    for shard, entries in grouped.items():
        require(len(entries) <= MAX_SHARD_KEYS, f"shard {shard}: more than {MAX_SHARD_KEYS} games")
        for group in entries.values():
            group.sort(key=lambda s: s["id"])
        content = json_bytes({"schemaVersion": SCHEMA_VERSION, "entries": entries})
        require(len(content) <= MAX_SHARD_BYTES, f"shard {shard}: larger than {MAX_SHARD_BYTES} bytes")
        files[f"{shard}.json"] = content
    content = json_bytes(manifest)
    require(len(content) <= MAX_MANIFEST_BYTES, "manifest larger than 64 KB")
    files["manifest.json"] = content
    return files


def write_new_dir(output: Path, files: dict[str, bytes]) -> None:
    require(not output.exists(), f"{output} already exists: every build needs a new version directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        for name, content in files.items():
            (staging / name).write_bytes(content)
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


# ---------------------------------------------------------------------- remote

def fetch(url: str, max_bytes: int, allowed_hosts: list[str] | None = None) -> bytes:
    current = url
    for _ in range(6):
        if allowed_hosts is not None:
            approved_url(current, allowed_hosts, "redirect target")
        request = urllib.request.Request(current, headers={"User-Agent": USER_AGENT})
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=30) as response:
                data = response.read(max_bytes + 1)
                require(len(data) <= max_bytes, f"{current}: larger than {max_bytes} bytes")
                return data
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                current = urllib.parse.urljoin(current, exc.headers["Location"])
                continue
            raise AddonError(f"{current}: HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise AddonError(f"{current}: {exc.reason}") from None
    raise AddonError(f"{url}: too many redirects")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def read_manifest(source: str) -> tuple[dict[str, Any], bytes]:
    if source.startswith(("https://", "http://")):
        raw = fetch(source, MAX_MANIFEST_BYTES)
    else:
        raw = Path(source).read_bytes()
    require(len(raw) <= MAX_MANIFEST_BYTES, "manifest larger than 64 KB")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AddonError(f"manifest is not valid UTF-8 JSON ({exc})") from None
    validate_manifest(manifest)
    return manifest, raw


def shard_bytes(manifest: dict[str, Any], shard: str, shard_dir: Path | None) -> bytes:
    if shard_dir is not None:
        path = shard_dir / f"{shard}.json"
        require(path.is_file(), f"missing shard file {path} (all 256 shards 00-ff must exist)")
        return path.read_bytes()
    url = manifest["lookup"]["urlTemplate"].replace("{shard}", shard)
    return fetch(url, MAX_SHARD_BYTES, manifest["allowedHosts"])


# -------------------------------------------------------------------- torrents

def bdecode(data: bytes, pos: int = 0) -> tuple[Any, int]:
    token = data[pos:pos + 1]
    if token == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if token in (b"l", b"d"):
        pos += 1
        items: list[Any] = []
        while data[pos:pos + 1] != b"e":
            value, pos = bdecode(data, pos)
            items.append(value)
        if token == b"l":
            return items, pos + 1
        return dict(zip(items[::2], items[1::2])), pos + 1
    if token.isdigit():
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        return data[colon + 1:colon + 1 + length], colon + 1 + length
    raise AddonError("not a valid .torrent file")


def info_span(data: bytes) -> tuple[int, int]:
    """Byte range of the raw `info` dictionary, needed for the v1 info hash."""
    pos = 1
    while data[pos:pos + 1] != b"e":
        key, pos = bdecode(data, pos)
        start = pos
        _, pos = bdecode(data, pos)
        if key == b"info":
            return start, pos
    raise AddonError(".torrent file has no info dictionary")


def torrent_files(data: bytes) -> tuple[str, str, list[tuple[int, str, int]]]:
    meta, _ = bdecode(data)
    require(isinstance(meta, dict) and b"info" in meta, "not a valid .torrent file")
    start, end = info_span(data)
    info_hash = hashlib.sha1(data[start:end]).hexdigest()
    info = meta[b"info"]
    name = info.get(b"name.utf-8", info.get(b"name", b"")).decode("utf-8", "replace")
    require(b"files" in info or b"length" in info, "BitTorrent v2-only torrents are not supported (need a v1 info hash)")
    if b"files" not in info:
        return info_hash, name, [(0, name, info[b"length"])]
    files = []
    for index, entry in enumerate(info[b"files"]):
        parts = entry.get(b"path.utf-8", entry.get(b"path", []))
        files.append((index, "/".join(p.decode("utf-8", "replace") for p in parts), entry[b"length"]))
    return info_hash, name, files


# -------------------------------------------------------------------- commands

def cmd_init(args: argparse.Namespace) -> None:
    target = Path(args.directory)
    config_path, sources_path = target / "addon.json", target / "sources.csv"
    require(not config_path.exists() and not sources_path.exists(), f"{target} already has addon.json or sources.csv")
    target.mkdir(parents=True, exist_ok=True)
    config = {
        "id": args.id,
        "name": args.name,
        "version": "1",
        "baseUrl": args.base_url or "https://addons.example.com/my-addon/1",
        "extraHosts": [],
    }
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    sources_path.write_text(",".join(SOURCE_COLUMNS) + "\n", encoding="utf-8")
    print(f"created {config_path} and {sources_path}")
    print("next: add one row per downloadable file to sources.csv, then run build")


def cmd_build(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    if args.version:
        config["version"] = args.version
    if args.base_url:
        config["baseUrl"] = args.base_url
    rows: list[dict[str, Any]] = []
    for path in args.sources:
        rows.extend(read_rows(Path(path)))
    require(rows, "no sources found")
    files = build(config, rows)
    output = Path(args.output or Path(args.config).parent / "build" / config["version"])
    write_new_dir(output, files)
    games = sum(len(json.loads(c)["entries"]) for n, c in files.items() if n != "manifest.json")
    sizes = [len(c) for n, c in files.items() if n != "manifest.json"]
    print(json.dumps({
        "output": str(output), "games": games, "sources": len(rows),
        "largestShardBytes": max(sizes), "totalShardBytes": sum(sizes),
        "shardUrl": json.loads(files["manifest.json"])["lookup"]["urlTemplate"],
        "allowedHosts": json.loads(files["manifest.json"])["allowedHosts"],
    }, indent=2))
    print(f"\nupload the 256 shard files so they are served at the shardUrl above;\n"
          f"share {output / 'manifest.json'} as the file users import in Gameio.")


def cmd_validate(args: argparse.Namespace) -> None:
    manifest, raw = read_manifest(args.manifest)
    print(f"manifest ok: {manifest['name']} ({manifest['id']}) version {manifest['version']}")
    shard_dir = Path(args.shards) if args.shards else None
    if shard_dir is None and not args.remote:
        local = Path(args.manifest).parent if not args.manifest.startswith("http") else None
        if local is not None and (local / "00.json").is_file():
            shard_dir = local
    if shard_dir is None and not args.remote:
        print("shards not checked: pass --shards DIR or --remote")
        return
    games = sources = 0
    kinds: dict[str, int] = {}
    names = [f"{i:02x}" for i in range(SHARD_COUNT)]
    with ThreadPoolExecutor(max_workers=1 if shard_dir else 8) as pool:
        shards = list(pool.map(lambda shard: validate_shard(shard_bytes(manifest, shard, shard_dir), manifest, shard), names))
    for data in shards:
        games += len(data["entries"])
        for group in data["entries"].values():
            sources += len(group)
            for s in group:
                kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    where = "remote" if shard_dir is None else str(shard_dir)
    print(f"all 256 shards ok ({where}): {games} games, {sources} sources {kinds}")


def cmd_lookup(args: argparse.Namespace) -> None:
    manifest, _ = read_manifest(args.manifest)
    key = lookup_key(args.igdb_id, args.platform)
    shard = shard_for(key)
    shard_dir = Path(args.shards) if args.shards else None
    data = validate_shard(shard_bytes(manifest, shard, shard_dir), manifest, shard)
    sources = data["entries"].get(key, [])
    print(f"{key} -> shard {shard}.json, {len(sources)} source(s)")
    for s in sources:
        print(json.dumps(s, ensure_ascii=False))


def cmd_torrent(args: argparse.Namespace) -> None:
    info_hash, name, files = torrent_files(Path(args.file).read_bytes())
    pattern = re.compile(args.match, re.IGNORECASE) if args.match else None
    chosen = [f for f in files if pattern is None or pattern.search(f[1])]
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=SOURCE_COLUMNS)
        writer.writeheader()
        for index, path, size in chosen:
            writer.writerow({"igdb_id": "", "platform": args.platform or "", "kind": "torrent",
                             "filename": path.rsplit("/", 1)[-1], "info_hash": info_hash,
                             "file_index": index, "path": path, "size": size})
        return
    print(f"name: {name}\ninfo hash: {info_hash}\nfiles: {len(files)} ({len(chosen)} shown)")
    for index, path, size in chosen:
        print(f"{index}\t{size}\t{path}")


def cmd_find(args: argparse.Namespace) -> None:
    server = args.server.rstrip("/")
    token = os.environ.get("GAMEIO_TOKEN")
    if not token:
        user = args.user or input("Gameio username: ")
        password = getpass.getpass("Gameio password (not stored): ")
        body = urllib.parse.urlencode({"grant_type": "password", "username": user, "password": password,
                                       "scope": "me.read roms.read platforms.read"}).encode()
        request = urllib.request.Request(f"{server}/api/token", data=body, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                token = json.load(response)["access_token"]
        except urllib.error.HTTPError as exc:
            raise AddonError(f"sign-in failed: HTTP {exc.code}") from None
    query = {"search": args.query, "limit": str(args.limit)}
    if args.platform:
        query["platform_slug"] = args.platform
    request = urllib.request.Request(f"{server}/api/catalog?{urllib.parse.urlencode(query)}",
                                     headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise AddonError(f"catalog search failed: HTTP {exc.code}") from None
    print(f"{result.get('total', 0)} match(es)")
    for game in result.get("items", []):
        platforms = ",".join(game.get("platform_slugs") or [])
        print(f"{game['igdb_id']}\t{game.get('release_year') or ''}\t{game['name']}\t[{platforms}]")


def cmd_platforms(_: argparse.Namespace) -> None:
    for slug, name in sorted(PLATFORMS.items()):
        print(f"{slug}\t{name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gameio_addon", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create addon.json and an empty sources.csv")
    p.add_argument("directory")
    p.add_argument("--id", required=True, help="stable id, e.g. com.example.homebrew")
    p.add_argument("--name", required=True, help="name shown in Gameio")
    p.add_argument("--base-url", help="HTTPS folder that will serve the shard files")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("build", help="turn addon.json + sources into manifest.json and 256 shards")
    p.add_argument("--config", default="addon.json")
    p.add_argument("--sources", nargs="+", default=["sources.csv"], help="CSV or JSON source lists")
    p.add_argument("--version", help="override the version in addon.json")
    p.add_argument("--base-url", help="override baseUrl in addon.json")
    p.add_argument("--output", help="new directory (default: build/<version> next to addon.json)")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("validate", help="check a manifest and its shards with the app's rules")
    p.add_argument("manifest", help="manifest.json path or https URL")
    p.add_argument("--shards", help="directory with 00.json-ff.json")
    p.add_argument("--remote", action="store_true", help="download all 256 shards from urlTemplate")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("lookup", help="show what the app will find for one game")
    p.add_argument("manifest")
    p.add_argument("igdb_id", type=int)
    p.add_argument("platform")
    p.add_argument("--shards", help="read shards from this directory instead of the web")
    p.set_defaults(func=cmd_lookup)

    p = sub.add_parser("torrent", help="list files, indexes and the info hash of a .torrent")
    p.add_argument("file")
    p.add_argument("--match", help="regular expression to filter file paths")
    p.add_argument("--csv", action="store_true", help="print sources.csv rows (fill in igdb_id)")
    p.add_argument("--platform", help="platform slug to prefill in --csv output")
    p.set_defaults(func=cmd_torrent)

    p = sub.add_parser("find", help="search the Gameio catalog for IGDB ids")
    p.add_argument("query")
    p.add_argument("--platform")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--server", default="https://playgameio.com")
    p.add_argument("--user", help="Gameio username (or set GAMEIO_TOKEN)")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("platforms", help="list Gameio platform slugs")
    p.set_defaults(func=cmd_platforms)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except AddonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
