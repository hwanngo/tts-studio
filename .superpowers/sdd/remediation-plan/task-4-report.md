# Task 4 verification report — Web model/voice capabilities and accessibility

## Scope completed

- Added `fetchModelVoices`, a single authenticated API-helper path that concurrently obtains
  preset and Saved Voice catalogs, preserves catalog order as preset then saved, and permits one
  catalog to remain usable when the other fails. It does not manage or persist credentials;
  underlying requests continue through the existing browser-session helpers.
- Studio now consumes the merged catalog atomically. It keeps a selected Voice if it still exists;
  otherwise it selects the first merged Voice. A Saved-Voice-only Model Installation consequently
  selects its Saved Voice and submits `saved_voice_id`.
- Studio gates Generate with `streaming_synthesis` plus `preset_voices` for preset sources; a
  Saved Voice needs streaming only. It gates Cancel with `synthesis_cancellation`; speed, pitch,
  volume, and inline cue guidance remain directly driven by advertised runtime capabilities.
- Voices fetches runtime data and gates preset preview on both `streaming_synthesis` and
  `preset_voices`. Disabled Generate, Cancel, and Preview controls expose localized descriptive
  reasons through `aria-describedby`.
- Locale changes synchronize `document.documentElement.lang` at boot and on each i18n language
  event. The compact button variant now retains a 44 CSS-pixel minimum height.
- Updated `docs/http-api.md` with the browser's precise runtime-capability behavior.

## Test-first evidence

Added failing behavioral coverage before implementation for:

- preset-first and saved-first catalog response completion;
- a Saved-Voice-only model selection and generation payload;
- Studio generation/cancellation/cue/prosody gates;
- Voices preview gating; and
- dynamic document language.

The initial focused run failed as intended for all new behaviors. After implementation, the final
focused run passed: `14` files, `126` tests.

## Verification performed

From `web/`:

```console
pnpm test -- --run
# 14 files, 126 tests passed
pnpm check
# TypeScript and locale parity passed
pnpm build
# Vite production build passed
```

Also passed from the worktree root:

```console
git diff --check
```

## UI/accessibility review

Applied the local UI guidance for native semantic buttons, visible disabled state, accessible
descriptions, and compact target sizing. Generate, Cancel, and Preview stay native disabled
buttons; their reasons are text, not color-only state. The existing focus styles remain in place.

## Concerns

- Browser component coverage is mocked; no live Worker-backed manual preview was run.
- The English JSON catalog was normalized by `jq` while adding keys; this is formatting-only beyond
  the two capability-reason entries.

## Review remediation — catalog refresh and transient runtime facts

The review-focused tests were written and run before the follow-up implementation. The focused
run exposed four failures: a selected non-first Voice was replaced during model refresh; the
Generate reason did not identify missing `preset_voices`; disabled Preview rows shared an invalid
description ID; and a failed runtime probe left Preview disabled after the next successful runtime
response.

The remediation preserves the Voice candidate while a replacement catalog is being fetched, then
performs the existing atomic merged-catalog selection: retain the candidate when it exists in the
merged result, otherwise select the first result. Generate also now requires that the selected
candidate is present in the current catalog, so it cannot be enabled during that loading gap.

Runtime probes now have explicit `loading`, `unknown`, and `advertised` states. A transport or
transient failure enters the localized checking state and retries after 750 ms; it is not treated
as an empty capability advertisement. Successful Voice discovery requests a fresh probe as well.
Monotonic request tokens ignore stale probe completions, so an earlier failed request cannot
overwrite a later successful Worker result. This governs Generate, Cancel, Preview, prosody, and
cue guidance consistently.

Each disabled Preview button now describes a row-specific reason element. Tests assert two
disabled rows expose existing, distinct `aria-describedby` targets and that both targets contain
the localized capability explanation. Generation's reason distinguishes missing
`streaming_synthesis` from the stream-present/preset-absent combination. The Saved-Voice-only
test now dispatches the `POST /api/v1/generations` success response (rather than matching it as a
generic generation-list response), asserts the queued result, and verifies its accessible control
state.

### Follow-up verification

From `web/`:

```console
pnpm test -- --run src/features/studio/StudioPage.test.tsx src/features/voices/VoicesPage.test.tsx src/i18n.test.ts
# 14 files, 130 tests passed
pnpm test -- --run
# 14 files, 130 tests passed
pnpm check
# TypeScript and locale parity passed
pnpm build
# Vite production build passed
```

From the worktree root: `git diff --check` passed.
