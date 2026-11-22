---
version: alpha
name: Hardbox personal chat
description: Quiet private login before the existing llama.cpp chat, for phone and desktop.
colors:
  primary: "#b6db8c"
  background: "#101918"
  surface: "#172421"
  foreground: "#f0f4ec"
  muted: "#aabbb0"
  border: "#394b42"
  danger: "#ffb4a8"
typography:
  sans:
    fontFamily: '"Trebuchet MS", "Segoe UI", sans-serif'
  display:
    fontFamily: 'Georgia, "Times New Roman", serif'
rounded:
  DEFAULT: "0.5rem"
  panel: "1rem"
spacing:
  page-max: "78rem"
  panel: "2.5rem"
components:
  login:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.panel}"
  button:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.background}"
    rounded: "{rounded.DEFAULT}"
    height: "3.1rem"
  help:
    textColor: "{colors.muted}"
  error:
    textColor: "{colors.danger}"
---

# Hardbox

## Overview

Product for one Russian-speaking owner using a phone or desktop outside home.
The login resembles a quiet reading room: dark green surfaces, pale green focus,
serif headline, practical form. It should not resemble a marketing dashboard.
User request and docs/README.md own product scope. No JavaScript framework,
external font, server database, or separate chat runtime is introduced.

The existing bundled llama.cpp chat owns chat rendering, message editing,
history, stop controls and streaming. The login is the only new visual surface.
Its canonical runtime tokens are :root in web/login.html; this document mirrors
them. Upstream chat colors and primitives remain owned by llama.cpp, rather
than copied or overridden locally.

## Colors

Login uses the concrete colors above through CSS variables. Errors use danger;
primary is reserved for action and keyboard focus. Forced colors use native
system colors and visible borders. Dark is the owned login theme.

## Typography

Locally available fonts support Russian without a network font download.
Georgia is only the introductory headline. Body and forms use the sans stack.
Body copy is at least 16px; technical wordmark uses Courier New.

## Layout

Two columns on desktop, one column below 760px. Inputs and actions remain
reachable with document scrolling and mobile safe-area padding. No fixed height
clips the password manager, error text or mobile keyboard.

## Elevation & Depth

One bordered form surface, without blur or ornamental shadows.

## Shapes

Half-rem fields/buttons; one-rem form panel. Global visible scrollbars inherit
thumb and track tokens, with platform fallbacks and forced-color override.

## Components

Native labeled inputs and buttons own login semantics. Form feedback reserves
space, reports validation through live text and aria-invalid, blocks duplicate
submits, and preserves username on failure. Password masking has a show/hide
button. Authentication remains on the server, not in UI state.

Chat ownership and authentication behavior are recorded in UX-CONTRACT.md.

## Do's and Don'ts

- Preserve upstream chat functionality when validating mobile layout.
- Show real connection and authentication failures without losing the username.
- Never put an API key into the page, browser history, analytics or an error.
- Never mix the dashboard PAM session with this separate chat session.
