---
version: alpha
name: Deck View Local
description: A local Hearthstone deck-image workbench with a brass action and a large card canvas.
colors:
  background: "#14252d"
  surface: "#1b303a"
  recessed: "#10212a"
  border: "#38515c"
  text: "#f3ede2"
  muted: "#a8b6bb"
  primary: "#e5be73"
  primary-hover: "#f1cd8e"
  on-primary: "#19272d"
  focus: "#83bbdb"
  success: "#9fcfbc"
  danger: "#ffb5ad"
typography:
  display:
    fontFamily: 'Georgia, "Times New Roman", serif'
  body:
    fontFamily: '"Segoe UI", system-ui, sans-serif'
  mono:
    fontFamily: 'Consolas, "Courier New", monospace'
rounded:
  DEFAULT: "12px"
  control: "8px"
spacing:
  page-max: "1440px"
  panel-gap: "28px"
  control-height: "48px"
components:
  button:
    borderRadius: "8px"
  input:
    borderRadius: "8px"
  preview:
    borderRadius: "12px"
---

# Deck View Local Design System

## Overview

The brief is a personal Windows tool: paste a Hearthstone deck code and export a full-card PNG like the upstream Deck Viewer. The interface is a card workbench, inspired by the game's blue mana crystal and brass card frames. A small brass-framed crystal emblem and the actual deck preview carry the identity. The controls remain quiet and familiar.

The interface is Russian; card images support ruRU and enUS. Locale does not imply a geographic market. This is a single-screen product, without accounts, messaging, publication, or payment. Existing upstream code has no application UI or sibling screen to preserve.

Token ownership uses model A: this file is the source; `generate_tokens.py` exports frontmatter colors, type families, radius and spacing to `web/tokens.css`. `web/styles.css` and all controls consume these variables. Generated CSS must not be edited by hand.

## Colors

Deep slate-blue makes card art legible, while brass marks the primary action. Off-white text and muted blue-grey text maintain contrast. A blue outline marks focus. Mint and pale coral communicate completion and errors together with text. There is one dark theme; forced-colors uses system colors and real borders. Scrollbars consume muted and recessed colors globally.

## Typography

Georgia supplies a restrained Cyrillic-capable display face. Segoe UI handles labels and copy, at 15–16px and line-height 1.5. Consolas is reserved for deck codes and keyboard hints. Stats use tabular numerals; numeric values use Russian formatting. No remote fonts or font-loading layout shifts.

## Layout

Use a 1440px content limit with a 340px input column and flexible image canvas. On widths below 920px, stack controls before the preview. Document scrolling owns vertical overflow. The preview reserves the upstream image's 3000:2344 ratio; controls retain their footprint through loading, success, and failure. Errors appear in the reserved form feedback region.

## Elevation & Depth

Surface tone and structural borders establish hierarchy. There are no decorative shadows, gradients, or nested card grids. Art supplies the visual complexity.

## Shapes

Containers use 12px corners, controls use 8px. The emblem uses the angular crystal geometry of the subject. Inline SVG icons are 20px and accompany readable action labels.

## Components

Primary buttons use brass with dark text; hover brightens brass and active shifts by 1px. Neutral controls have border and hover surface changes. Every interactive target has focus-visible treatment; disabled controls are semantically disabled. Busy button dimensions remain fixed.

The form uses a real label, `novalidate`, inline associated errors and focus recovery. A native radio group chooses card language; there is no dropdown popup. A status live region communicates actual server progress. Only one generation per form can run; the previous generated PNG remains accessible until a new one succeeds. Input is preserved on error. Ctrl+Enter submits, excluding IME composition. Long exported deck text grows to a bounded textarea, with a visible scrollbar.

Empty preview displays the upstream image explicitly labeled as a sample. Download and full-image links appear only after real generation. PNG output is the source of truth; the page does not invent card information. There are no destructive actions or modal dialogs. Code drafts stay in session storage, with guarded storage access.

All copy describes user actions plainly. The loader is a small app-owned spinner with progress text, not a skeleton. Motion lasts 150ms, and reduced motion removes animation and transitions.

## Do's and Don'ts

- Do prioritize readable cards and a direct paste-to-PNG workflow.
- Do preserve entered codes and successful output during recoverable errors.
- Don't imply that the sample image was generated from the user's input.
- Don't add messaging, accounts, analytics, or cloud hosting to this local tool.
