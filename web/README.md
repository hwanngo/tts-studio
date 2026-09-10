# TTS Studio Web

The React client consumes the FastAPI Core API. Types in `src/generated/api.ts`
are generated from `app.openapi()` and must not be edited by hand.

From the repository root, regenerate the client after changing public routes:

```sh
uv run python scripts/generate_openapi_client.py
```

Check for drift with `pnpm --dir web api:check`. The renderer format is pinned
as `tts-studio-openapi-v1` in the root `pyproject.toml`; deterministic output
keeps generated changes reviewable and reproducible without a network download.

## Adding localized strings

User-facing strings must add the same key to both bundled catalogs:
`src/locales/en-US/main.json` and `src/locales/vi-VN/main.json`. Catalogs are bundled
at build time; the Web client does not fetch translations at runtime. Components must
access translated strings through `useTranslation()` rather than hard-coded text.

Run the recursive key-shape check after changing either catalog:

```sh
pnpm --dir web locale:check
```
