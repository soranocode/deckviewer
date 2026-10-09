# Deck View Local behavior

Source: the user's request to paste a deck code locally and generate an image; upstream renderer supplies the export format. HearthSim deckstrings and HearthstoneJSON supply decoding and metadata/image contracts.

## Canonical UI Map

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
| --- | --- | --- | --- | --- |
| Form | web/app.js form handler and labeled native fields | This contract | deck export text | Browser success/error/keyboard |
| Scrollbar | web/styles.css global baseline | DESIGN.md generated tokens | bounded textarea | Computed styles and narrow viewport |
| Toast | web/app.js status live region | This contract | persistent inline progress/error/success | Browser state inspection |

Language uses a native radio group, not a single-select/dropdown. No Date, Table Selection, CRUD, authentication, destructive actions, or sibling routes apply.

## Flow

Paste code or full game export → submit → one asynchronous local job → visible actual progress → image preview and download. Invalid input preserves draft and focuses the code field. Network/cache/image failures preserve any previous output, show the reason, and allow retry. Pending generation disables input/options/submit to prevent duplicate requests. Every fetch has a timeout; polling ends after five minutes with a recoverable message. Stale request tokens cannot overwrite a newer result.

The server binds only to 127.0.0.1. It validates Host/Origin, JSON size and locale, and only serves whitelisted app assets and named generated PNGs. Deck codes decode on this computer. Metadata and card render requests do not send deck codes; no tokens or bot platforms are involved. Cache and generated output are local files. First use of a card/language needs network access; cached data/images can be reused offline.

Drafts are stored in guarded sessionStorage for refresh recovery, not in URLs. The generated image is saved locally automatically and can be downloaded again. No deletion or irreversible operation exists.

## Evidence

Engine and HTTP integration tests verify input/error, paths, job completion, and local-origin restrictions. Browser verification in QA.md records empty/loading/error/success, input labeling, Ctrl+Enter, native radio keyboard, narrow layout and reduced-motion behavior. There are no comparable sibling screens in this new single-screen tool.
