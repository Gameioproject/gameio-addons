# Gameio add-on format (`catalog-shards-v1`)

An add-on tells Gameio where to download games. It has two parts:

1. **Manifest**: one small JSON file that users import into Gameio. It names the add-on and says where the shards live.
2. **Shards**: 256 JSON files (`00.json` … `ff.json`) served over HTTPS. Each lists download sources for some games.

When a user opens a game, the app works out which shard holds that game, downloads only that shard, and caches it for 24 hours (5 minutes if the game wasn't in it). The app never downloads the whole add-on.

Every rule below is enforced by the app (`AddonFormat.kt`). `tools/gameio_addon.py validate` checks the same rules.

## Game keys and shards

A game is identified by `igdbId:platformSlug`, for example `1074:n64`.

- `igdbId`: the game's [IGDB](https://www.igdb.com) id. It must be a positive integer.
- `platformSlug`: the Gameio platform slug. Run `gameio_addon.py platforms` for the list. Pattern: `[a-z0-9][a-z0-9-]{0,99}`.

The shard is the first two hex characters of the SHA-256 of the key's UTF-8 bytes:

```
sha256("1074:n64") = cf96b5e1…  ->  cf.json
```

All 256 shard files must exist, even empty ones (`{"entries":{},"schemaVersion":1}`). A missing shard shows up as a network error for every game in it.

## Manifest

```json
{
  "schemaVersion": 1,
  "id": "com.example.starter",
  "name": "Starter add-on",
  "version": "1",
  "adapter": "catalog-shards-v1",
  "lookup": {
    "key": "igdbId:platformSlug",
    "partition": "sha256-prefix-2",
    "urlTemplate": "https://addons.example.com/starter/1/{shard}.json"
  },
  "allowedHosts": ["addons.example.com", "downloads.example.com"]
}
```

| Field | Rule |
|---|---|
| `schemaVersion` | `1` |
| `id` | Stable forever: re-importing a file with the same id replaces the add-on. Pattern `[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}`. The builder also requires lowercase, for example `com.yourname.homebrew`. |
| `name` | 1–100 characters, no control characters. Shown in the app. |
| `version` | 1–100 characters. Change it on every rebuild. |
| `adapter`, `lookup.key`, `lookup.partition` | Exactly the values shown above. |
| `lookup.urlTemplate` | HTTPS, at most 2048 characters, exactly one `{shard}`, no other `{`/`}`, no username, password or `#fragment`. Its host must be in `allowedHosts`. |
| `allowedHosts` | 1–64 unique lowercase host names (no scheme, port or path). The shard host plus every host that `http` sources use or redirect to. |

The whole manifest must be at most 64 KB.

**Versioning:** the app caches shards by a fingerprint of the manifest, so changing anything (normally `version`) makes every user fetch fresh shards. Publish each version in its own folder (`…/starter/2/`) and never change files already published. That lets the server cache shards forever.

## Shard

```json
{
  "schemaVersion": 1,
  "entries": {
    "1074:n64": [
      {
        "id": "6dce9aa6…",
        "kind": "http",
        "filename": "Example Game (USA).z64",
        "locator": {"url": "https://downloads.example.com/n64/Example%20Game%20(USA).z64"},
        "size": 8388608,
        "region": "USA"
      }
    ]
  }
}
```

**Shard limits:**
- at most 1 MB;
- at most 1000 games;
- every key must belong in this shard;
- at most 200 sources per game, with unique `id`s.

### Source fields

| Field | Rule |
|---|---|
| `id` | Pattern `[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}`, unique within the game. The builder uses the SHA-256 of `{"kind","locator"}` (canonical JSON), so the same file always gets the same id. The app uses it to resume downloads. |
| `kind` | `internet_archive`, `http` or `torrent` |
| `filename` | The name the file is saved as. 1–500 characters, no `/`, `\` or control characters, not `.` or `..`. |
| `locator` | Depends on `kind` (see below). Only the listed fields are allowed. |
| `size` | Optional positive byte count. If present, the download must match it exactly. |
| `md5`, `sha1` | Optional hex checksums. If present, the downloaded file is verified. |
| `region` | Optional, at most 100 characters (for example `USA`, `Europe`). |

Always add `size`, and a checksum when you know it: a wrong or truncated file is then caught instead of installed.

### `internet_archive`

```json
"locator": {"item": "my-archive-item", "path": "Folder/Game (USA).zip"}
```

- **`item`:** the archive.org identifier, the part after `archive.org/details/`. Pattern `[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}`.
- **`path`:** the file inside the item. It must be relative: no leading `/`, no `\`, no empty, `.` or `..` segments, and at most 2000 characters. Write it unencoded; the app URL-encodes it.
- **Download URL:** the app downloads `https://archive.org/download/<item>/<path>`. Redirects may only go to `archive.org` or its subdomains, and `allowedHosts` doesn't need to list them.
- **Access:** items that need an archive.org login can't be downloaded by the app. Check that the file downloads in a private browser window first.

### `http`

```json
"locator": {"url": "https://downloads.example.com/n64/Game.z64"}
```

- **URL:** must be HTTPS, with no username, password or `#fragment`, and its host must be in `allowedHosts`.
- **Redirects:** every redirect must also stay inside `allowedHosts`. If a host redirects to a CDN, add the CDN's host name through `extraHosts`.
- **Faster downloads:** servers that support `Range` requests let the app resume and download in parallel.

### `torrent`

```json
"locator": {"infoHash": "0123…4567", "fileIndex": 12, "path": "Collection/N64/Game.zip"}
```

- **`infoHash`:** the 40-character BitTorrent **v1** info hash. v2-only torrents are not supported.
- **`fileIndex`:** the 0-based position of the file in the `.torrent`'s file list.
- **`path`:** the file's path inside the torrent, joined with `/`, **without** the torrent's own name. For a single-file torrent, it's the torrent name.
- **Getting the values:** `gameio_addon.py torrent FILE.torrent --csv` prints all three for every file.
- **Account needed:** torrent sources only work for users who connected a Real-Debrid account in Gameio.
- **How it resolves:**
  1. The app adds the magnet to the user's account, selecting only that file.
  2. It matches the file by `path`, never by file name alone.
  3. It downloads the single resulting link.
- **Size limit:** Real-Debrid limits torrents by their *total* size, so huge collections may be refused even when the chosen file is small.

## Hosting

Serve the manifest and shards from any static HTTPS host, for example Cloudflare Pages, GitHub Pages, an S3 or R2 bucket, or your own server. Requirements:

- **HTTPS:** a valid certificate on the default port.
- **Content:** shards served as-is, at most 1 MB each. `Content-Type: application/json` is recommended.
- **Redirects:** allowed only to hosts listed in `allowedHosts`.
- **Caching:** recommended: `Cache-Control: public, max-age=31536000, immutable` on shards in versioned folders, and `no-cache` on a manifest you update in place.

`tools/serve.py` is a small read-only server for this layout (put it behind an HTTPS proxy or tunnel).

Share the manifest file itself. Users import it in Gameio under **Settings → Add-ons → Import add-on**, or during first-run setup.
