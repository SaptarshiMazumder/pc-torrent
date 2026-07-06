# Handoff: Forge Desktop UI Redesign — "Aurora Glass"

## Overview
This is a full visual redesign of the **Forge** Blender render-farm desktop client. It reskins the existing app from the current "Graphite Dark" look into a warm, luminous **glassmorphism** theme ("Aurora Glass") with an orange ember accent, frosted-glass cards over an aurora-gradient background, and light + dark modes. **All existing functionality, pages, data, and copy stay the same** — this changes look-and-feel, layout polish, and adds richer data-viz on the Telemetry page. It is not a rewrite.

The target app is already scaffolded and matches this design's information architecture 1:1 (see Page Mapping below), so this is best implemented as a **theming + component-restyle pass**, page by page, not a from-scratch build.

## About the Design Files
The file in this bundle — **`Forge Redesign.dc.html`** — is a **design reference created in HTML**. It is a single-file interactive prototype showing the intended look, layout, spacing, colors, and behavior. It is **not production code to copy directly**: it uses a custom template runtime (`support.js`, `<x-dc>`, `{{ }}` holes) that is irrelevant to your app. The `support.js` file is the prototype's runtime only — **ignore/do not copy it.**

Your task is to **recreate this design inside the existing `desktop/` React app**, using its established patterns: React 19 function components, the `page`-state router in `App.jsx`, the CSS-variable + `App.css` styling system, `react-i18next` for all copy, and `recharts` (already a dependency) for charts. Read the inline styles in the HTML to lift exact values (hex, spacing, radii, font sizes) — then express them as CSS classes/variables in `App.css`, matching how the codebase already works. **Do not introduce inline styles or a CSS-in-JS library**; the app is className-based.

## Fidelity
**High-fidelity (hifi).** Colors, typography, spacing, radii, shadows, and interactions are final. Recreate pixel-accurately. Exact values are catalogued under Design Tokens; per-screen specifics are under Screens.

## Target codebase orientation
- **Location:** `desktop/` (Tauri v2 + Vite + React 19).
- **Router:** No react-router. `App.jsx` holds `const [page, setPage] = useState(...)` and conditionally renders one page component. Navigation is `onNavigate(pageId)` from `Sidebar`. **Keep this pattern.**
- **Two modes:** `mode` is `"rentee"` (Rent — a user submitting renders) or `"renter"` (Provide — a user offering their machine). The Sidebar shows a different nav list per mode (`RENTEE_PAGES` / `RENTER_PAGES` in `Sidebar.jsx`). Preserve this.
- **Styling:** Global CSS variables live in `:root` at the top of `src/App.css` (currently the "Graphite Dark" palette). Components use semantic classes (`.sidebar`, `.nav-item`, `.nav-item-active`, `.main-content`, `.sidebar-brand`, etc.). **The cleanest implementation is to rewrite the `:root` token block + affected component classes in `App.css`** and add a `[data-theme="light"]` / `[data-theme="dark"]` layer, rather than touching hundreds of JSX files.
- **Copy/i18n:** All visible strings go through `t("...")` with locale files in `src/i18n/locales/{en,ja}`. Any new labels (e.g. Telemetry card titles) must be added to those locale files, not hardcoded.
- **Charts:** `recharts@3` is already installed and used in `src/components/telemetry/*`. Build the new Telemetry visuals with recharts (AreaChart, BarChart, PieChart) styled to match — do not hand-roll SVG paths from the prototype verbatim.
- **3D backdrop:** `ForgeSceneBackdrop` (react-three-fiber) renders behind everything. The new theme's aurora-gradient background must sit **above** the 3D scene's scrim but the glass cards should still read over it. Coordinate the new `--bg` gradient with the existing `.forge-scene-scrim`.

## Page Mapping (design → existing `page` key → file)
The design's tabs map directly onto existing pages. Do **not** rename page keys.

- **Home / dashboard hero** → `page: "dashboard"` → `src/pages/DashboardPage.jsx` (renter/Provide mode landing)
- **Create Render** → `page: "create"` → `src/pages/CreateRenderPage.jsx` (rentee/Rent mode landing) — **now a first-class sidebar tab**, already is in `RENTEE_PAGES`
- **My Jobs** → `page: "myjobs"` → `src/pages/MyJobsPage.jsx`
- **Telemetry** → `page: "stats"` → `src/pages/TelemetryPage.jsx` (⚠ key is `"stats"`, label is "Telemetry")
- **Machines** → `page: "available"` → `src/pages/AvailableMachinesPage.jsx`
- **Downloads** → `page: "downloads"` → `src/pages/DownloadsPage.jsx`
- **Logs** → `page: "logs"` → `src/pages/LogsPage.jsx`
- **Configuration** (admin only) → `page: "configuration"` → keep as-is, restyle to match.
- **About** → `page: "about"` → keep as-is, restyle to match.

The prototype fully specifies **Home, Create Render, My Jobs, and Telemetry**. For the remaining pages (Machines, Downloads, Logs, Configuration, About) the prototype does not draw them yet — **apply the same design system** (glass cards, tokens, type scale, chart styling) to the existing content. Do not remove or fabricate content on those pages; only restyle.

## Design System

### Theme model
Two themes driven by a `data-theme` attribute on a top-level wrapper (`"light"` default, `"dark"`). In the prototype it's on the app root div; in your app, set it on `<html>` or the `.app` container and persist the choice to `localStorage` (mirror how `mode` is persisted). A theme toggle lives top-right of the content area.

### Color tokens

**Brand accent (both themes):**
- `--th` (accent / ember): `#ee5a29` — primary brand orange. This is the ONLY orange; used for the active nav pill, primary CTAs (as a gradient), brand mark, links, focus.
- `--th2` (accent-bright): `#ff8f45` — used only as the light stop of accent gradients: `linear-gradient(135deg, var(--th2), var(--th))`.
- `--soft`: `rgba(238,90,41,.10)` — tinted fill for active/hover surfaces.
- `--line`: `rgba(238,90,41,.20)` — accent hairline.

**Light theme:**
- `--bg` (page background): a layered aurora gradient over warm parchment —
  ```
  radial-gradient(760px 480px at 6% -8%, rgba(255,150,80,.24), transparent 60%),
  radial-gradient(620px 460px at 100% 2%, rgba(96,130,255,.18), transparent 58%),
  radial-gradient(680px 520px at 80% 110%, rgba(255,130,170,.15), transparent 58%),
  radial-gradient(560px 440px at 42% 66%, rgba(40,196,168,.12), transparent 62%),
  linear-gradient(180deg, #f7f3ec, #ece3d5)
  ```
- `--glass` (card surface): `linear-gradient(158deg, rgba(255,255,255,.6), rgba(255,255,255,.42))`
- `--gbrd` (glass border): `rgba(255,255,255,.75)`
- `--text`: `#211a15`
- `--muted`: `#9a8d7b`
- `--hair` (divider): `rgba(120,90,50,.12)`
- `--studio` (3D viewport bg): `radial-gradient(130% 95% at 50% 16%, #fdf4ea, #ebdac7 82%)`
- `--knob`: `#fff`

**Dark theme (`[data-theme="dark"]`):**
- `--bg`: `linear-gradient(180deg, #0b0b0b, #000)`
- `--glass`: `linear-gradient(158deg, #191919, #0e0e0e)`
- `--gbrd`: `rgba(255,255,255,.09)`
- `--text`: `#f3ece4`
- `--muted`: `#a99a8b`
- `--hair`: `rgba(255,255,255,.09)`
- `--studio`: `radial-gradient(130% 95% at 50% 16%, #181818, #000 82%)`
- `--knob`: `rgba(255,255,255,.2)`

**Data-viz palette (theme-independent; NEVER wear orange — orange is reserved for brand):**
- Teal: `rgb(14,165,183)`
- Blue: `rgb(63,116,255)`
- Violet: `rgb(139,108,240)`
- Green / positive delta: `#12a150` (success), also `rgb(18,161,80)`
- Semantic status may reuse the codebase's existing `--success #22c55e`, `--warning #f59e0b`, `--danger #ef4444` where those already appear.

### Glass card recipe
Every card/panel uses this exact treatment:
```css
background: var(--glass);
backdrop-filter: blur(28px) saturate(1.6);
-webkit-backdrop-filter: blur(28px) saturate(1.6);
border: 1px solid var(--gbrd);
border-radius: 20–22px;   /* 20 for stat cards, 22 for large panels */
box-shadow: 0 12px 34px -30px rgba(90,55,20,.18);
```
Stat/KPI cards add a soft colored outer glow matching their accent, e.g. `0 0 24px -20px rgba(14,165,183,.45)`. The left sidebar uses a stronger blur: `blur(30px) saturate(1.5)`.

### Typography
- **UI / display font:** Manrope (`'Google Sans','Product Sans','Manrope', system-ui, sans-serif` in the proto; in your app just use **Manrope** as the primary and keep the system fallback). Load weights 400/500/600/700/800. Note the current app uses Inter — switch the body font stack to Manrope.
- **Mono / data font:** **IBM Plex Mono**, weights 400/500/600. Used for all metric labels, axis ticks, table headers, numeric readouts, and small caps-y labels — usually `letter-spacing: .05–.08em`, uppercase, in `--muted`.
- Type scale seen in the design:
  - Big metric numbers: 25–28px / weight 700 / `letter-spacing: -1px`
  - Card/panel title: 15px / weight 700
  - Body: 13–14px / weight 500–600
  - Metric label (mono): 10px / weight 600 / uppercase / tracked
  - Nav label: 14px / weight 500 (700 when active)
  - Brand wordmark: 19px / weight 700 / `letter-spacing: -.3px`
- Enable `text-wrap: pretty` on prose.

### Radii
- Cards / large panels: 20–22px
- Nav items, mode button, chips: 13px
- Small icon tiles (30px): 10px
- Brand mark tile (34px): 11px
- Pills/badges: 7px; progress bars / bars: 3–4px; heatmap cells: 3px

### Sidebar spec
- Expanded width **240px**; collapsed rail **72px** (icon-only, 42×42 tiles). Prototype supports a collapse toggle — keep the existing app's behavior if it has one; otherwise expanded is fine.
- Glass background (`blur(30px) saturate(1.5)`), 1px right border `var(--gbrd)`.
- Brand row: 34px accent-gradient rounded tile with a hexagon/cube glyph + "Forge" wordmark.
- **Create Render is now a top nav item** (document-with-plus icon), not a separate CTA button. It sits first in the rentee nav and highlights like any other tab.
- Nav item: `display:flex; gap:13px; padding:10px 12px; border-radius:13px`. Active state = background `linear-gradient(120deg, var(--soft), rgba(238,90,41,.03))`, label weight 700 + `color: var(--text)`; inactive label weight 500 + `color: var(--muted)`. Icon stroke uses `var(--th)`.

### CTA / button
Primary button: `linear-gradient(135deg, var(--th2), var(--th))`, white text, weight 700, radius 14px, height ~46px. Secondary/ghost: transparent with `var(--gbrd)` border.

## Screens (design-specified)

### Home (dashboard)
- Top bar: page title + right-aligned controls (theme toggle, mode switch Rent/Provide).
- **KPI strip:** row of glass stat cards, each = colored 30px icon tile + mono label + big number + green delta + a small gradient sparkline (area under a 2px line, color-matched).
- **Hero render card:** large glass panel containing a CSS-3D spinning cube "viewport" over the `--studio` radial background, with a scan-sweep animation (`rendersweep` keyframes), the latest rendered frame, and a horizontal filmstrip of recent frames. In your app, this is the live render preview — wire it to the real current-job frame data (the `agent.currentJob` / frame cache already in `DashboardPage`). The spinning-cube is decorative; keep or simplify.
- **Fleet card:** summary of the user's machines/nodes.
- **Throughput + Recent jobs row** below.

### Create Render (`create`)
- Full dedicated page (already the rentee landing).
- **Drop zone** for new `.blend` / `.zip` files.
- **Saved drafts panel:** list of in-progress render configs that persist — each row shows filename, edited-timestamp, and a status chip (`READY` / `DRAFT`). Clicking a draft resumes it. Persist drafts to `localStorage` (or the app's existing store) so navigating away and back keeps them. Status chip colors follow the data-viz/semantic palette.
- **Render-settings form:** scene, view layer, camera, frame range, engine (Cycles/EEVEE), resolution, samples, output, priority.
- **Live cost estimate** + primary **Start render** CTA (accent gradient).

### My Jobs (`myjobs`)
- Restyle the existing jobs table/grid into glass cards. Keep existing columns, statuses, search, and view toggle. Status badges use semantic colors; progress cells use rounded (radius 4px) bars.

### Telemetry (`stats`) — the richest new page. Build with recharts.
- **KPI strip (4 cards):** Frames Rendered, GPU-Hours, Avg Frame Time, Success Rate — each with colored icon tile, mono label, big number, green delta, gradient sparkline. Accent colors: orange, teal, violet, green respectively.
- **GPU utilization** (2/3 width): 24h multi-series **area chart** — three GPU tiers (RTX 4090 = teal, RTX 3090 = blue, A5000 = violet), each a 2.5px line over a matching vertical gradient fill, faint horizontal gridlines (`var(--hair)`), mono time-axis ticks (00:00 → 24:00), legend top-right.
- **Engine mix** (1/3 width): **donut/pie** — Cycles (orange) 68% / EEVEE (blue) 32%, rounded-cap segments, center shows total jobs; legend rows below with color dot + label + mono %.
- **Frames per hour** (1/2 width): **bar chart**, bars filled `linear-gradient(180deg, rgb(255,143,69), rgb(238,90,41))`, radius `6px 6px 3px 3px`; the peak bar gets a glow `0 0 18px -4px rgba(238,90,41,.55)`; mono time-axis ticks.
- **Render activity** (1/2 width): GitHub-style **contribution heatmap**, 7 rows × ~20 cols, 5 orange intensity levels (`rgba(238,90,41, {.05,.24,.46,.68,.94})`), 5px gap, 3px radius cells, less→more legend.
- **Top machines leaderboard:** glass panel, mono column headers (`# / MACHINE / GPU / FRAMES SERVED / UPTIME / EARNED`), rows with rank-colored index, GPU tier badge (color-coded pill), a progress bar for frames served, green uptime %, and earnings in credits. Use the word "credits" spelled out.

Note: the current app already has telemetry components (`GpuUsagePanel`, `RendersLeaderboard`, `TelemetryChartCard`, `TelemetryGauge`, `TokenUsageChart`, etc.) and a `useUserTelemetry` hook — **reuse/restyle these and feed them the real data**; the numbers in the prototype are placeholders.

## Interactions & Behavior
- **Theme toggle** (top-right): switches `data-theme` between light/dark; persist to `localStorage`. All tokens swap via the CSS variable layer — no per-component logic.
- **Mode switch** (Rent/Provide): existing behavior — swaps the sidebar nav list and default page. Keep `handleModeChange` in `App.jsx`.
- **Sidebar collapse** (if kept): 240px ↔ 72px; icon-only rail when collapsed.
- **Nav:** click a tab → `onNavigate(pageId)`; active tab gets the accent-tinted pill.
- **Hover:** cards lift subtly / borders warm toward `--line`; nav items tint toward `--soft`. Keep transitions ~150–200ms ease.
- **Create Render drafts:** clicking a draft loads its settings into the form; drafts persist across navigation and app restarts.
- **Animations** (decorative, from prototype keyframes): `emberpulse` (status dot), `spin3d`/`bgspin` (cube viewport), `rendersweep` (scan line over the render preview), `bgfloat`. These are optional polish — prioritize correctness of data + layout.
- Respect `prefers-reduced-motion` by disabling the decorative loops.

## State Management
- **theme**: `"light" | "dark"`, in `App.jsx` state, persisted to `localStorage` (`pcrent_theme`). Applied as `data-theme` on the app root.
- **page / mode**: already exist in `App.jsx` — unchanged.
- **sidebar collapsed** (optional): boolean, persisted.
- **create-render drafts**: array of saved config objects, persisted; each `{ id, filename, updatedAt, status: "ready"|"draft", settings:{...} }`.
- **Telemetry data**: from the existing `useUserTelemetry` hook / jobs data — not new. Prototype values are placeholders to be replaced with live data.

## Design Tokens (quick reference)
Colors, radii, and type are fully listed under Design System. Summary of the variable layer to add to `:root` (light) and `[data-theme="dark"]` in `App.css`:
`--th #ee5a29`, `--th2 #ff8f45`, `--soft rgba(238,90,41,.10)`, `--line rgba(238,90,41,.20)`, plus theme-scoped `--bg --glass --gbrd --text --muted --hair --studio --knob` (values above). Data-viz: teal `rgb(14,165,183)`, blue `rgb(63,116,255)`, violet `rgb(139,108,240)`, green `#12a150`.
Fonts: **Manrope** (UI, 400–800) + **IBM Plex Mono** (data, 400–600), via Google Fonts.

## Assets
- **No raster/image assets** are required — all icons are inline SVG (Lucide-style, 2px stroke, `currentColor`), and the brand mark is an inline hexagon/cube SVG. Recreate them as small React icon components (the existing `Sidebar.jsx` already defines its nav icons this way — follow that pattern and swap the Create icon to a document-with-plus).
- Fonts load from Google Fonts (Manrope, IBM Plex Mono). If the app must work offline (Tauri), self-host these under `src/assets/` or `public/`.
- The existing 3D backdrop (`ForgeSceneBackdrop`, `.gif` loaders, `openexr` icon) stays; just ensure the new background gradient/scrim harmonizes with it.

## Files in this bundle
- **`Forge Redesign.dc.html`** — the interactive HTML design reference. Open it in a browser to see all four specified screens (use the sidebar to navigate; theme toggle and Rent/Provide switch are top-right). Read its inline `style="..."` attributes to lift exact values. **Ignore the `<x-dc>`, `{{ }}`, `<helmet>`, and any `support.js` reference — those are prototype-runtime scaffolding, not part of the design.**

## Suggested implementation order
1. **Token layer + fonts:** rewrite `:root` in `App.css` to the Aurora Glass light tokens, add `[data-theme="dark"]`, swap body font to Manrope, add IBM Plex Mono. Add theme state + toggle in `App.jsx`.
2. **Sidebar:** restyle `.sidebar` and nav to the glass spec; make Create Render a normal nav item (already in the list — just restyle, no orange CTA button).
3. **Global card/panel classes:** define a reusable `.glass-card` style and apply across pages.
4. **Home / dashboard**, then **Create Render** (drafts), then **My Jobs**.
5. **Telemetry** with recharts (biggest lift) — reuse existing telemetry components + `useUserTelemetry`.
6. **Restyle remaining pages** (Machines, Downloads, Logs, Configuration, About) with the same system.
7. **i18n:** add any new strings to `en`/`ja` locale files.

Implement page-by-page; verify each in `npm run dev` (or `npm run tauri dev`) before moving on. Keep all data wiring, props, and copy intact — this is a reskin, not a behavior change.
