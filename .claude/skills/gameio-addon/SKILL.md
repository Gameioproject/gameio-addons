---
name: gameio-addon
description: Create, update, validate and publish Gameio add-ons (catalog-shards-v1 manifests and shards). Use when someone wants to make a Gameio add-on, turn a list of download links, an archive.org item or a .torrent into an add-on, find IGDB ids or platform slugs for games, debug an add-on that fails to import or shows no sources, or publish a new add-on version.
---

# Making Gameio add-ons

A Gameio add-on is a **manifest** (one JSON file users import) plus **256 shard files** served over HTTPS. The full rules are in `docs/format.md`; read it before changing anything format-related. Do all building and checking with `tools/gameio_addon.py` (Python 3.10+, standard library only) instead of writing JSON by hand. The tool's validation matches the app exactly.

## Workflow

Work through these steps in order and show the user the tool output at each step.

1. **Set up the project.** For a new add-on:
   ```bash
   python3 tools/gameio_addon.py init addons/<name> --id com.<owner>.<name> --name "<Display name>" --base-url https://<host>/<name>/1
   ```
   - The id must be lowercase and must never change later. Re-importing a manifest with the same id replaces the add-on on the device.
   - If the user doesn't know where they'll host it yet, keep the placeholder base URL and set it before building.

2. **Collect sources into `sources.csv`,** one row per downloadable file. The columns are:
   `igdb_id, platform, kind, filename, url, ia_item, path, info_hash, file_index, size, md5, sha1, region`

   | kind | Fill in | Leave empty |
   |---|---|---|
   | `http` | `url` (HTTPS) | `ia_item`, `path`, `info_hash`, `file_index` |
   | `internet_archive` | `ia_item` (identifier after `archive.org/details/`), `path` (file inside the item, unencoded) | `url`, `info_hash`, `file_index` |
   | `torrent` | `info_hash`, `file_index`, `path` | `url`, `ia_item` |

   Guidance for this step:
   - **`filename`:** can be left empty, in which case it's derived from the URL or path.
   - **`size` and checksums:** always fill in `size` when known, and `md5`/`sha1` when available; the app verifies them.
   - **`region`:** use short labels such as `USA`, `Europe` or `Japan`.
   - **Torrents:** get exact rows with `python3 tools/gameio_addon.py torrent FILE.torrent --csv --platform <slug> [--match REGEX]`, then fill in `igdb_id`. Never guess `file_index` or `path`: the app refuses a path that doesn't match the torrent exactly.
   - **archive.org:** file lists are at `https://archive.org/metadata/<item>` (the `files[].name`, `size`, `md5` and `sha1` fields). Items that need a login can't be downloaded by the app.
   - **Large lists:** a JSON list with the same field names also works (`--sources list.json`). For big lists, write a small script that produces the CSV rather than editing by hand.

3. **Find the game identity.** Every row needs the game's **IGDB id** and the **Gameio platform slug**.
   - **Platform slugs:** `python3 tools/gameio_addon.py platforms` lists them. The slug must be one Gameio uses (for example `psx`, not `ps1`; `ngc`, not `gamecube`), or the game never matches.
   - **IGDB ids:** `python3 tools/gameio_addon.py find "<title>" --platform <slug>` searches the Gameio catalog.
     - It asks for the user's Gameio username and password, or reads `GAMEIO_TOKEN`.
     - Let the user type their password themselves. Never ask them to paste it into the chat, and never store it.
     - Pick the match whose name, year and platform list fit, and ask the user when several are plausible (remasters, regional titles).
   - **One id per game:** a game released on several platforms uses the same IGDB id with different slugs.

4. **Build** into a new version folder:
   ```bash
   python3 tools/gameio_addon.py build --config addons/<name>/addon.json --sources addons/<name>/sources.csv
   ```
   - The output goes to `addons/<name>/build/<version>/`.
   - The tool refuses to overwrite an existing folder. To publish a change, raise `version` in `addon.json` and move `baseUrl` to a matching new folder (for example `…/<name>/2`).
   - If an `http` host redirects to another host (a CDN), add that host to `extraHosts`.

5. **Validate:**
   ```bash
   python3 tools/gameio_addon.py validate addons/<name>/build/<version>/manifest.json
   python3 tools/gameio_addon.py lookup addons/<name>/build/<version>/manifest.json <igdb_id> <slug> --shards addons/<name>/build/<version>
   ```
   Use `lookup` on a few real games to confirm they appear with the expected files.

6. **Publish.**
   1. Upload the 256 shard files (`00.json`–`ff.json`) so they're served at the `urlTemplate` in the manifest. Hosting options are in `docs/format.md` → Hosting. `tools/serve.py` works behind an HTTPS proxy or tunnel.
   2. Check the live copy: `python3 tools/gameio_addon.py validate <manifest> --remote`.
   3. Share `manifest.json`, renamed to something friendly if the user likes. Users import it under **Settings → Add-ons → Import add-on** in Gameio.

## Troubleshooting

| Symptom in the app | Likely cause | Check |
|---|---|---|
| Import fails | Manifest breaks a rule, or is over 64 KB | `validate <manifest>` |
| Game shows no sources | Wrong IGDB id or platform slug, or the shard wasn't uploaded | `lookup … --remote` with the id and slug the app uses |
| Network error for some games | A shard file is missing or not HTTPS, or a redirect leaves `allowedHosts` | `validate --remote` |
| Download blocked as untrusted | `http` URL or redirect host missing from `allowedHosts` | add the host to `extraHosts` and rebuild |
| "Needs your Real-Debrid account" | `torrent` source and no account connected | expected; offer an `http` or `internet_archive` source too |
| Integrity error after download | `size`, `md5` or `sha1` doesn't match the file served | re-check the values against the real file |
| Changes don't show up | `version` unchanged, so the app keeps cached shards (up to 24 h) | raise `version`, publish to a new folder, re-import |

## Rules for Claude

- **Only use sources the user supplies or confirms they may distribute** (their own files, homebrew, public-domain or licensed content). Don't search for or suggest download sources for commercial games.
- **Never edit shard JSON by hand.** Change the sources and rebuild, so ids, sorting and shard placement stay correct.
- **Never reuse a published version folder,** because users' devices cache shards by manifest fingerprint.
- **Keep secrets out of the repo:** add-on files must not contain tokens, passwords or signed URLs that expire.
- **After changing `tools/gameio_addon.py`,** run `python3 -m unittest discover -s tests`.
