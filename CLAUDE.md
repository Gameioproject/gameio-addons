# gameio-addons

- Building or debugging a Gameio add-on: follow `.claude/skills/gameio-addon/SKILL.md`.
- Format rules live in `docs/format.md` and must match the Gameio app's `AddonFormat.kt`; change both together.
- `tools/gameio_addon.py` stays standard-library only. Run `python3 -m unittest discover -s tests` after changing it.
- Never commit real download data, tokens or build output (`build/` is ignored).
