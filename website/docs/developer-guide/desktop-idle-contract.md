---
sidebar_label: "Desktop idle contract"
title: "Desktop idle contract: idle costs nothing"
description: "What periodic work the Hermes Desktop renderer may run while idle or hidden, which primitive each kind uses, and the lint gates that enforce it."
---

# Desktop idle contract: idle costs nothing

An app that sits open and does nothing must cost close to nothing: no heat, no
fan, no battery. This is a contract, not a tuning goal. Idle burn has come back
in pieces for months (tracker #127647). Each time, a feature added some
periodic work that seemed reasonable where it was written, and nothing measured
the total.

**The states.** Two questions, asked separately:

- *Can anyone see it?* viewed (visible and focused) / visible but unfocused /
  hidden or minimized.
- *Is work running?* A turn, tool, subagent or background process is live /
  nothing is running.

**What may run, by kind:**

| Kind | Primitive | Runs when |
|---|---|---|
| UI clock (countdown, elapsed label) | `useViewedInterval` (`src/hooks/use-viewed-interval.ts`), `enabled` tied to the thing it counts | viewed, and only while its subject is live |
| Decorative JS animation (rAF, canvas) | `createBudgetedLoop` (`src/lib/budgeted-loop.ts`) with an `idleWhen` | observable, and parked when there is nothing to animate |
| Infinite CSS animation | a class covered by the `:root[data-renderer-animations-paused]` rule in `src/styles.css` | not hidden or minimized |
| Server data | a backend push event (`sessions.changed`, stream events); a poll is a slow backstop, never the primary path | as the event arrives |

Rules that follow:

- An indicator that animates because work is running must stop when the work
  stops, including when the terminal `busy: false` never arrives. Reconcile on
  reconnect (`reconcileBusyStatesOnReconnect`), never trust a latch.
- Nothing that only decorates may run forever. A placeholder resolves and
  holds (`DecodeText` defaults to `loop: false`).
- An animation property that forces layout or paint (`left`, `width`,
  `background-position`) never runs `infinite`. Animate `transform` or
  `opacity`, which the compositor handles without main-thread work.
- A poll declares its period, and its cost is per process, not per session or
  per connection. Before adding a loop, check whether an existing one already
  runs at that cadence.

**What the lint lane enforces** (`npm run lint`):

- `no-restricted-globals` / `no-restricted-properties` reject a raw
  `setInterval` in `src/`. Existing call sites are frozen by count in
  `eslint-suppressions.json`. A file that gains one fails. A file that loses one
  also fails until `npx eslint src/ electron/ --prune-suppressions` shrinks the
  baseline, so it only goes down. If a raw interval is genuinely right, use
  `eslint-disable-next-line` with the gate that stops it.
- `scripts/check-idle-animations.mjs` rejects an infinite animation that the
  hidden-window pause rule cannot reach. It reads `src/` CSS plus every
  stylesheet it `@import`s, transitively (codicons, tw-shimmer, katex,
  Tailwind), and fails if an import does not resolve. It follows `var(--x)`
  chains, `@apply` and `@utility`, derives infinite utilities from
  `--animate-*` theme variables, and scans markup for those utilities
  in markup and `@apply`, variants included (`animation-play-state` is not
  inherited, so the pause names the element that animates: the `svg` for
  `[&_svg]:`, children for `*:`, `::before` for `before:`),
  `animate-[…]` / `animate-(--x)` values and inline `animation: '…'` styles.
  Only a rule outside any condition (`@layer` aside) whose every
  `animation-play-state` entry is `paused` counts as a pause.
- Run ESLint from `apps/desktop` (as `npm run lint` does). The suppressions
  file is found relative to the working directory, so running it from the
  repo root reports every frozen site as an error.

Blind spots the gates do not cover, so review them by hand: Web Animations
started from script (`element.animate(…, { iterations: Infinity })`), class
names assembled at runtime from fragments, inline animation values held in a
variable (`style={{ animation: spin }}`), and windows that never install the
pause state (the overlay, quick and wake windows).

**Open decision, recorded so it is made once.** Should a visible but unfocused
window keep decorative animations running? #86587 paused them on blur;
#106225 restored them for visible windows (#96219 asks for the same). The
table above follows current main: decorative CSS runs whenever the window is
not hidden. #122413 measured the visible-idle case at about 0.3 of a core in
the renderer alone. Revisit this row with a profile, not by preference.
