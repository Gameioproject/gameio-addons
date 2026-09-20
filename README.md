# gameio-addons

Tools and docs for making **Gameio add-ons**: small JSON files that tell the Gameio app where to download games when a player opens them.

- `tools/gameio_addon.py`: build, validate and inspect add-ons (Python 3.10+, no dependencies)
- `tools/serve.py`: a tiny read-only server for published add-on files
- `docs/format.md`: the full `catalog-shards-v1` format, as enforced by the app
- `examples/starter/`: a starter add-on with placeholder links
- `.claude/skills/gameio-addon/`: a Claude skill that walks you through all of this

## Quick start

```bash
# 1. Create a project
python3 tools/gameio_addon.py init addons/homebrew --id com.you.homebrew --name "My homebrew" \
  --base-url https://files.example.com/homebrew/1

# 2. Add one row per file to addons/homebrew/sources.csv
#    Need IGDB ids?       python3 tools/gameio_addon.py find "game title" --platform snes
#    Need platform slugs? python3 tools/gameio_addon.py platforms
#    Have a .torrent?     python3 tools/gameio_addon.py torrent file.torrent --csv --platform snes
#    Check one game?      python3 tools/gameio_addon.py lookup manifest.json 1234 snes

# 3. Build and check
python3 tools/gameio_addon.py build --config addons/homebrew/addon.json --sources addons/homebrew/sources.csv
python3 tools/gameio_addon.py validate addons/homebrew/build/1/manifest.json

# 4. Upload build/1/00.json … ff.json so they appear at the manifest's urlTemplate, then
python3 tools/gameio_addon.py validate addons/homebrew/build/1/manifest.json --remote
```

Share `manifest.json` with players. They import it in Gameio under **Settings → Add-ons → Import add-on**.

## Publishing

Any static HTTPS host works: the manifest sits at the root, the shards live under
`<addon>/<version>/`, and the app never asks for anything else.

```
my-addon.json                     the manifest users import
my-addon/1/00.json … ff.json      that version's shards
```

`tools/serve.py` serves exactly that shape and 404s everything else, so several add-ons can
share one folder without exposing the rest of it:

```bash
ADDONS_ROOT=./public ADDONS_HOST=127.0.0.1 ADDONS_PORT=3100 python3 tools/serve.py
```

Put it behind HTTPS with a reverse proxy or a tunnel. The two published Gameio samples,
[gameio-archive.json](https://addons.playgameio.com/gameio-archive.json) (Internet Archive,
no account needed) and
[gameio-minerva-ra.json](https://addons.playgameio.com/gameio-minerva-ra.json) (Real-Debrid,
RetroAchievements-supported games only), are served this way and are worth reading as
finished examples.

## Using Claude

Open this repo in Claude Code and ask, for example, "make a Gameio add-on from these links" or "why does my add-on show no sources?". The `gameio-addon` skill loads automatically and follows the steps above.

## Source kinds

| Kind | What it points to | Player needs |
|---|---|---|
| `http` | A direct HTTPS file link | nothing |
| `internet_archive` | A file inside a public archive.org item | nothing |
| `torrent` | One file inside a BitTorrent v1 torrent | a connected Real-Debrid account |

Only publish files you have the right to distribute.

## Tests

```bash
python3 -m unittest discover -s tests
```
