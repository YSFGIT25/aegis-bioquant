"""
telemetry/dashboard_ui.py
--------------------------
The dashboard's look and behaviour: stylesheet, page sections and
script. Shared by the local viewer (telemetry/dashboard.py) and the
hosted site (web/pages.py, web/app.py), so both always look the same.

DESIGN INTENT, so later changes keep to it:
  - ONE main list. Every upcoming FDA decision is a single row that
    answers, left to right: who, when, how strong is the evidence, how
    big a move is expected, are options cheap, and what is this
    system's status on it. Everything else is behind a tab.
  - PLAIN WORDS FIRST. "FDA decision", "Evidence score", "Expected
    move", "Options cost" — the trade terms (PDUFA, IV Rank) appear only
    in the guide tab and in row details.
  - NOTHING UNEXPLAINED. A row with no signal says why; a blank number
    says what it is waiting for.
  - DARK ONLY, deliberately: it is styled as a trading terminal, and one
    theme done well beats two done halfway.

SAFETY: everything from /api/state is put on the page with textContent
or as attribute values checked first (links must start with https://) —
never innerHTML — so text scraped from filings or sent to the webhook
cannot inject markup.
"""

from urllib.parse import quote

FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600&display=swap">'
)

# The mark: a shield holding a rising line drawn as linked atoms. The
# files in branding/ are the same drawing at full size.
_LOGO_GRADIENT = (
    '<defs><linearGradient id="lg" x1="12" y1="6" x2="52" y2="58" gradientUnits="userSpaceOnUse">'
    '<stop offset="0" stop-color="#5b9dff"/><stop offset="1" stop-color="#2fd6b5"/></linearGradient></defs>'
)
_LOGO_SHIELD = "M32 5 53 12.5V29.5C53 43.2 44.8 53.2 32 59 19.2 53.2 11 43.2 11 29.5V12.5Z"
_LOGO_MARK = (
    f'<path d="{_LOGO_SHIELD}" fill="url(#lg)" fill-opacity=".14"/>'
    f'<path d="{_LOGO_SHIELD}" fill="none" stroke="url(#lg)" stroke-width="3" stroke-linejoin="round"/>'
    '<path d="M20.5 40.5 28 31.5 36.5 36 45 23.5M28 31.5 25.5 21.5" fill="none" stroke="url(#lg)" '
    'stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/>'
    '<g fill="#0c1017" stroke="url(#lg)" stroke-width="2.4"><circle cx="20.5" cy="40.5" r="3.4"/>'
    '<circle cx="28" cy="31.5" r="3.4"/><circle cx="36.5" cy="36" r="3.4"/><circle cx="25.5" cy="21.5" r="2.6"/></g>'
    '<circle cx="45" cy="23.5" r="4.2" fill="url(#lg)"/>'
)

LOGO_SVG = f'<svg class="logo" viewBox="7 3 50 58" width="30" height="35" aria-hidden="true">{_LOGO_GRADIENT}{_LOGO_MARK}</svg>'

# The browser-tab icon: the mark on the page's dark square, carried in the
# page itself so it needs no extra file or request.
FAVICON_LINK = '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,' + quote(
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">{_LOGO_GRADIENT}'
    f'<rect width="64" height="64" rx="14" fill="#0c1017"/><g transform="translate(3.2 3.2) scale(.9)">{_LOGO_MARK}</g></svg>'
) + '">'

PAGE_STYLE = """
  :root {
    color-scheme: dark;
    --bg: #080b10; --bg-2: #0c1017; --panel: #10151d; --panel-2: #151b25; --line: #1e2633; --line-2: #2a3445;
    --text: #edf1f6; --muted: #8b96a8; --dim: #5d6779;
    --long: #2fd08a; --equity: #5b9dff; --avoid: #f4596c; --warn: #f5b544; --none: #7a8496;
    --brand-1: #5b9dff; --brand-2: #2fd6b5;
    --sans: Inter, "SF Pro Text", "Segoe UI", system-ui, -apple-system, sans-serif;
    --mono: "JetBrains Mono", "SF Mono", ui-monospace, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  html { background: var(--bg); }
  body { margin: 0; color: var(--text); font: 14px/1.55 var(--sans); -webkit-font-smoothing: antialiased;
         background: radial-gradient(1200px 500px at 70% -10%, rgba(91,157,255,.10), transparent 60%),
                     radial-gradient(900px 420px at 0% 0%, rgba(47,214,181,.06), transparent 55%), var(--bg);
         background-attachment: fixed; min-height: 100vh; }
  main { max-width: 1240px; margin: 0 auto; padding: 28px 20px 64px; }
  a { color: var(--equity); text-decoration: none; }
  a:hover { text-decoration: underline; }
  h1 { font-size: 22px; margin: 0; letter-spacing: -0.01em; }
  h2 { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin: 0 0 10px; }
  .hint { color: var(--muted); font-size: 13px; margin: 0 0 14px; max-width: 80ch; }
  .muted { color: var(--muted); }
  .num { font-family: var(--mono); font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
  .up { color: var(--long); } .down { color: var(--avoid); }
  .empty { background: var(--panel); border: 1px dashed var(--line-2); border-radius: 14px; padding: 28px;
           color: var(--muted); text-align: center; }
  .empty strong { display: block; color: var(--text); font-size: 15px; margin-bottom: 4px; }

  /* ---- top of the dashboard ---- */
  .toolbar { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 12px 24px; margin-bottom: 20px; }
  .page-title { font-size: 26px; font-weight: 700; letter-spacing: -0.02em; }
  .page-sub { margin: 4px 0 0; color: var(--muted); }
  .toolbar-right { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  #status { color: var(--dim); font-size: 12px; }
  #status.bad { color: var(--avoid); }
  .pill { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; font-weight: 600; padding: 5px 11px;
          border-radius: 999px; background: var(--panel-2); border: 1px solid var(--line-2); color: var(--muted); white-space: nowrap; }
  .pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--dim); }
  .pill.open { color: var(--long); border-color: rgba(47,208,138,.35); background: rgba(47,208,138,.08); }
  .pill.open::before { background: var(--long); box-shadow: 0 0 0 3px rgba(47,208,138,.2); }

  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .kpi { background: linear-gradient(180deg, var(--panel-2), var(--panel)); border: 1px solid var(--line); border-radius: 14px;
         padding: 16px 18px; position: relative; overflow: hidden; }
  .kpi::after { content: ""; position: absolute; inset: 0 0 auto 0; height: 2px;
                background: linear-gradient(90deg, var(--brand-1), var(--brand-2)); opacity: .55; }
  .kpi .k { color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; }
  .kpi .v { font-size: 28px; font-weight: 700; letter-spacing: -0.03em; margin-top: 6px; line-height: 1.15; }
  .kpi .s { color: var(--muted); font-size: 12px; margin-top: 4px; }

  /* ---- tabs and filters ---- */
  .tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--line); margin-bottom: 18px; overflow-x: auto; scrollbar-width: none; }
  .tabs::-webkit-scrollbar { display: none; }
  .tab { font: inherit; font-weight: 600; color: var(--muted); background: none; border: 0; border-bottom: 2px solid transparent;
         padding: 10px 14px; cursor: pointer; white-space: nowrap; margin-bottom: -1px; }
  .tab:hover { color: var(--text); }
  .tab.on { color: var(--text); border-bottom-color: var(--brand-1); }
  .tab .count { font-family: var(--mono); font-size: 11px; color: var(--muted); background: var(--panel-2);
                border: 1px solid var(--line); border-radius: 999px; padding: 1px 7px; margin-left: 6px; }
  .tab .count:empty { display: none; }
  .filters { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between; margin-bottom: 12px; }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { font: inherit; font-size: 13px; font-weight: 500; color: var(--muted); background: var(--panel); border: 1px solid var(--line);
          border-radius: 999px; padding: 6px 13px; cursor: pointer; }
  .chip:hover { color: var(--text); border-color: var(--line-2); }
  .chip.on { color: var(--text); background: rgba(91,157,255,.14); border-color: rgba(91,157,255,.5); }
  #search { font: inherit; color: var(--text); background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
            padding: 8px 12px; min-width: 240px; }
  #search::placeholder { color: var(--dim); }
  #search:focus-visible, .tab:focus-visible, .chip:focus-visible, .row:focus-visible { outline: 2px solid var(--brand-1); outline-offset: 2px; }

  /* ---- the main list ---- */
  .list { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }
  .cols, .row { display: grid; grid-template-columns: minmax(200px, 2.2fr) minmax(150px, 1.3fr) minmax(120px, 1fr) minmax(120px, 1fr) minmax(130px, 1.1fr) minmax(150px, 1.2fr) 20px;
                gap: 14px; align-items: center; padding: 0 18px; }
  .cols { color: var(--dim); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em;
          padding-top: 12px; padding-bottom: 12px; border-bottom: 1px solid var(--line); background: var(--bg-2); }
  .row { width: 100%; font: inherit; color: inherit; text-align: left; background: none; border: 0; border-bottom: 1px solid var(--line);
         padding-top: 14px; padding-bottom: 14px; cursor: pointer; }
  .row:hover { background: rgba(255,255,255,.025); }
  .row.open { background: rgba(91,157,255,.05); }
  .item:last-child .row:not(.open) { border-bottom: 0; }
  .who { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .avatar { flex: none; width: 36px; height: 36px; border-radius: 10px; display: grid; place-items: center; font-weight: 700;
            font-size: 12px; letter-spacing: 0.02em; color: #fff; }
  .who .t { font-weight: 700; font-size: 15px; letter-spacing: 0.01em; }
  .who .c { color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .who > div { min-width: 0; }
  .cell .big { font-size: 15px; font-weight: 600; }
  .cell .sub { color: var(--muted); font-size: 12px; }
  .cell .label { display: none; color: var(--dim); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; }
  .bar { height: 4px; border-radius: 999px; background: var(--line-2); margin-top: 6px; max-width: 110px; overflow: hidden; }
  .bar i { display: block; height: 100%; border-radius: 999px; background: linear-gradient(90deg, var(--brand-1), var(--brand-2)); }
  .chev { color: var(--dim); transition: transform .15s ease; font-size: 12px; }
  .row.open .chev { transform: rotate(90deg); color: var(--text); }
  @media (prefers-reduced-motion: reduce) { .chev { transition: none; } }

  .status { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; padding: 4px 10px;
            border-radius: 999px; border: 1px solid var(--line-2); color: var(--muted); background: var(--panel-2); white-space: nowrap; }
  .status.signal { color: var(--long); border-color: rgba(47,208,138,.4); background: rgba(47,208,138,.1); }
  .status.avoid { color: var(--avoid); border-color: rgba(244,89,108,.4); background: rgba(244,89,108,.1); }
  .status.setup { color: var(--equity); border-color: rgba(91,157,255,.35); background: rgba(91,157,255,.08); }
  .status.stock { color: var(--equity); }
  .status.close { color: var(--warn); border-color: rgba(245,181,68,.35); background: rgba(245,181,68,.08); }
  .status.filtered, .status.late, .status.unscored { color: var(--dim); }

  /* one of the three checks: a mark, the answer, and what it rests on */
  .check { display: flex; align-items: flex-start; gap: 9px; }
  .mark { flex: none; width: 20px; height: 20px; border-radius: 50%; display: grid; place-items: center; font-size: 12px;
          font-weight: 700; margin-top: 1px; border: 1px solid var(--line-2); color: var(--dim); }
  .check.pass .mark { color: #06130d; background: var(--long); border-color: var(--long); }
  .check.soft .mark { color: var(--long); border-color: rgba(47,208,138,.6); background: rgba(47,208,138,.1); }
  .check.fail .mark { color: var(--avoid); border-color: rgba(244,89,108,.45); background: rgba(244,89,108,.08); }
  .check.fail .big, .check.unknown .big { color: var(--muted); }
  /* ---- the introduction and the "today" line ---- */
  .intro { position: relative; background: var(--panel); border: 1px solid var(--line-2); border-radius: 14px;
           padding: 22px 24px; margin-bottom: 16px; }
  .intro h2 { margin: 0 0 6px; font-size: 18px; letter-spacing: -0.01em; }
  .intro > p { margin: 0 0 16px; color: var(--muted); max-width: 820px; line-height: 1.55; }
  .intro .x { position: absolute; top: 14px; right: 14px; }
  .intro-steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; margin-bottom: 16px; }
  .intro-step { background: var(--bg-2); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
  .intro-step b { display: block; margin-bottom: 4px; }
  .intro-step span { color: var(--muted); font-size: 13px; line-height: 1.5; }
  .intro-step .n { display: inline-grid; place-items: center; width: 22px; height: 22px; border-radius: 50%; margin-right: 8px;
                   font-size: 12px; font-weight: 700; color: #06130d; background: var(--long); }
  .verdicts { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 8px 20px; }
  .verdicts div { display: flex; align-items: baseline; gap: 10px; color: var(--muted); font-size: 13px; line-height: 1.5; }
  .verdicts .status { flex: none; min-width: 108px; justify-content: center; }
  .intro .foot { margin: 16px 0 0; color: var(--dim); font-size: 12px; }
  .today { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; background: var(--panel); border: 1px solid var(--line);
           border-left: 3px solid var(--line-2); border-radius: 12px; padding: 14px 18px; margin-bottom: 16px; }
  .today.act { border-left-color: var(--long); }
  .today.watch { border-left-color: var(--equity); }
  .today b { font-size: 15px; }
  .today span { color: var(--muted); font-size: 14px; }
  .evreview { grid-column: 1 / -1; }
  .evreview .head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 14px; margin-bottom: 8px; }
  .evreview .score { font-family: var(--mono); font-size: 26px; font-weight: 700; color: var(--text); }
  .evreview .two { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 4px 28px; margin-top: 10px; }
  .evreview h4 { margin: 8px 0 4px; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--dim); }
  .evreview ul { margin: 0; padding-left: 18px; color: var(--muted); font-size: 13px; line-height: 1.55; }
  .evreview .for li::marker { color: var(--long); }
  .evreview .against li::marker { color: var(--avoid); }
  .cell .ai { color: var(--equity); font-size: 12px; margin-top: 2px; }
  .cell .ai.warn { color: var(--warn); font-weight: 600; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 18px; color: var(--muted); font-size: 13px; margin: 0 0 14px; }
  .legend .check { align-items: center; gap: 7px; }
  .legend .mark { margin-top: 0; }

  .detail { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; padding: 16px 18px 20px;
            border-bottom: 1px solid var(--line); background: rgba(91,157,255,.03); }
  .item:last-child .detail { border-bottom: 0; }
  .fact { background: var(--panel-2); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
  .fact h3 { margin: 0 0 6px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }
  .fact p { margin: 0; }
  .fact p + p { margin-top: 6px; color: var(--muted); font-size: 13px; }

  /* ---- signal cards ---- */
  .card { background: var(--panel); border: 1px solid var(--line); border-left: 3px solid var(--none); border-radius: 14px;
          padding: 18px 20px; margin-bottom: 12px; }
  .card.LONG_RUN_UP_ALPHA, .card.LONG_TERM_EQUITY_ACCUMULATION { border-left-color: var(--long); }
  .card.STRUCTURAL_COLLAPSE_AVOID { border-left-color: var(--avoid); }
  .card-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
  .ticker { font-size: 20px; font-weight: 700; letter-spacing: 0.01em; }
  .when { color: var(--dim); font-size: 12px; margin-left: auto; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 14px; margin: 16px 0 6px; }
  .stat .k { color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; }
  .stat .v { font-size: 18px; font-weight: 600; margin-top: 2px; }
  .stat .s { color: var(--muted); font-size: 12px; }
  details { margin-top: 10px; }
  summary { cursor: pointer; color: var(--muted); font-size: 13px; }
  details ul { margin: 8px 0 0; padding-left: 18px; }
  details p { margin: 8px 0 0; }

  /* ---- plain tables (track record, watch list) ---- */
  .scroll { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 14px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 12px 16px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  th { color: var(--dim); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; background: var(--bg-2); }
  tr:last-child td { border-bottom: 0; }
  tbody tr:hover td, table tr:not(:first-child):hover td { background: rgba(255,255,255,.02); }
  td.wrap { white-space: normal; min-width: 200px; }

  .guide { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
  .guide .fact p { color: var(--muted); }
  .guide .fact h3 { color: var(--text); font-size: 14px; text-transform: none; letter-spacing: 0; }
  .fineprint { color: var(--dim); font-size: 12px; margin-top: 18px; max-width: 90ch; }

  @media (max-width: 860px) {
    .cols { display: none; }
    .row { grid-template-columns: 1fr 1fr; gap: 12px 16px; padding-top: 16px; padding-bottom: 16px; }
    .row .who { grid-column: 1 / -1; }
    .row .chev { display: none; }
    .cell .label { display: block; margin-bottom: 2px; }
    #search { min-width: 0; flex: 1; }
    .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .kpi { padding: 14px; }
    .kpi .v { font-size: 22px; }
  }
"""

DASHBOARD_SECTIONS = """
  <div class="toolbar">
    <div>
      <h1 class="page-title">FDA catalyst dashboard</h1>
      <p class="page-sub">Upcoming FDA decisions at small and mid-sized drug makers, checked every trading day.</p>
    </div>
    <div class="toolbar-right">
      <button class="chip" id="intro-open" type="button" hidden>How this works</button>
      <span id="market" class="pill">US market</span>
      <span id="status">Loading…</span>
    </div>
  </div>

  <div class="intro" id="intro" hidden>
    <button class="chip x" id="intro-close" type="button">Got it, hide this</button>
    <h2>New here? Start with this.</h2>
    <p>When the FDA approves or rejects a small drug company's medicine, the stock can jump or collapse overnight. The date of each
       decision is known months ahead. This page lists every one coming up and checks each for one specific situation: good odds of
       approval, a big move expected, and options that have not become expensive yet.</p>
    <div class="intro-steps">
      <div class="intro-step"><b><span class="n">1</span>Read the Verdict column</b>
        <span>It is the answer for each company, and the list is sorted with the best at the top. If nothing says “Buy setup”, there is nothing to act on today. That is the usual case.</span></div>
      <div class="intro-step"><b><span class="n">2</span>Click a row to see why</b>
        <span>Each company gets the same three checks: trial evidence, expected move, options cost. The row opens to explain each one in a sentence and tells you what to do with it.</span></div>
      <div class="intro-step"><b><span class="n">3</span>Let the alerts do the watching</b>
        <span>Connect Telegram or Discord under Alert settings. You get a message the day a company becomes a Buy setup, so you do not need to keep checking this page.</span></div>
    </div>
    <div class="verdicts">
      <div><span class="status signal">Buy setup</span><span>All three checks pass and the timing is right (35 to 45 days before the decision). The one to look at.</span></div>
      <div><span class="status setup">Setting up</span><span>All three pass, but it is too early. Worth watching.</span></div>
      <div><span class="status stock">Strong evidence, stock only</span><span>Very strong evidence (90 or more), but options are expensive or barely traded. A case for the shares, not options.</span></div>
      <div><span class="status close">Close: 2 of 3</span><span>One check is missing. Nothing to do.</span></div>
      <div><span class="status">No setup</span><span>Does not fit. Most rows say this.</span></div>
      <div><span class="status avoid">Avoid</span><span>A warning: weak evidence, a big move expected, and options already expensive.</span></div>
      <div><span class="status late">Filtered out / Too late</span><span>The company is too large or its options too thinly traded, or fewer than 35 days remain.</span></div>
    </div>
    <p class="foot">The other tabs: Alerts keeps every alert sent. Track record shows past FDA decisions and how the stock reacted. Trials finishing soon lists late-stage trials about to report. How to read this defines each term.</p>
  </div>

  <div class="today" id="today"></div>

  <div class="kpis" id="kpis"></div>

  <div class="tabs" role="tablist" aria-label="Dashboard sections">
    <button class="tab on" role="tab" aria-selected="true" data-tab="catalysts">Upcoming decisions<span class="count" id="count-catalysts"></span></button>
    <button class="tab" role="tab" aria-selected="false" data-tab="signals">Alerts<span class="count" id="count-signals"></span></button>
    <button class="tab" role="tab" aria-selected="false" data-tab="record">Track record<span class="count" id="count-record"></span></button>
    <button class="tab" role="tab" aria-selected="false" data-tab="phase3">Trials finishing soon<span class="count" id="count-phase3"></span></button>
    <button class="tab" role="tab" aria-selected="false" data-tab="guide">How to read this</button>
  </div>

  <section data-panel="catalysts">
    <div class="filters">
      <div class="chips" id="filters">
        <button class="chip on" data-filter="all">All</button>
        <button class="chip" data-filter="best">Best setups</button>
        <button class="chip" data-filter="window">In entry window</button>
        <button class="chip" data-filter="priced">Market expects a move</button>
      </div>
      <input id="search" type="search" placeholder="Search ticker or company" aria-label="Search ticker or company">
    </div>
    <p class="hint">Every company gets the same three checks. Pass all three and it is a setup; the best ones are listed first. Click a row for the reasons.</p>
    <div class="legend" id="legend"></div>
    <div id="catalysts"></div>
  </section>

  <section data-panel="signals" hidden>
    <p class="hint">Every alert sent so far, newest first. An alert goes out when a company inside its entry window (35 to 45 days before the decision) passes all three checks, has very strong evidence but expensive options (stock only), or trips the Avoid warning.</p>
    <div id="alerts"></div>
  </section>

  <section data-panel="record" hidden>
    <p class="hint">Every FDA decision we have recorded, with how the stock reacted and what this system said beforehand. Scores are only shown if they were made before the decision.</p>
    <div id="trackrecord"></div>
  </section>

  <section data-panel="phase3" hidden>
    <p class="hint">Late-stage (Phase 3) trials about to finish at listed companies. A watchlist only: these are not scored and never raise an alert. The date is when data collection ends; results usually follow weeks later.</p>
    <div id="phase3"></div>
  </section>

  <section data-panel="guide" hidden>
    <div class="guide">
      <div class="fact"><h3>FDA decision</h3><p>The date by which the FDA has committed to approve or reject a new drug (the industry calls it a PDUFA date). For a small company it can move the stock sharply in either direction.</p></div>
      <div class="fact"><h3>Trial evidence</h3><p>Check 1. A score from 0 to 100 for how strong the case for approval looks. It is written by an AI model after reading the company's announcements and annual report, the trial's public registry entry and, where those say little, published results found on the web. It starts from 83, roughly how often drugs at this stage are approved, and moves up or down with what it finds. 75 or more passes. “Typical” means little has been published about this drug, so the score stays near 83. It is a model's reading, not a proven probability; open a row to see its reasons and sources.</p></div>
      <div class="fact"><h3>Expected move</h3><p>Check 2. How big a jump or drop traders are betting on for the decision itself, read from options prices. 10% or more passes. “None” means traders are not treating the decision as a major event for that company.</p></div>
      <div class="fact"><h3>Options cost</h3><p>Check 3. Whether the stock's options are cheap or expensive compared with its own past year, on a 0 to 100 scale. Under 30 passes. Where it says “estimate”, it is measured against how much the stock itself has moved over the past year; after 30 trading days of our own readings it switches to the standard measure (traders call it IV Rank). The estimate leans toward calling options expensive.</p></div>
      <div class="fact"><h3>Entry window</h3><p>The 35 to 45 days before an FDA decision. It is the only period in which an alert can go out: early enough to act before attention builds.</p></div>
      <div class="fact"><h3>Verdicts</h3><p>“Setting up” means all three checks pass but the entry window has not opened. “Buy setup” means they pass inside the window, and alerts go out. “Strong evidence, stock only” means a score of 90 or more with options that are expensive or barely traded: a case for the shares, with its own alert inside the window. “Close” means two of three. “Avoid” is the reverse of a setup: weak evidence, a big expected move and expensive options.</p></div>
    </div>
    <p class="fineprint">FDAOdds publishes automated, model-generated information. It is not investment advice. FDAOdds is independent and is not affiliated with, or endorsed by, the U.S. Food and Drug Administration. The evidence score has no long track record yet; see the Track record tab for what has been measured so far.</p>
  </section>
"""

DASHBOARD_SCRIPT = """
const REFRESH_MS = 30000;
const view = { tab: "catalysts", filter: "all", query: "", open: new Set(), state: null };

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}
const pct0 = (v) => (v * 100).toFixed(0) + "%";
const money = (v) => (v === null || v === undefined) ? "n/a"
  : "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const words = (s) => String(s || "").replaceAll("_", " ").toLowerCase();
const has = (v) => v !== null && v !== undefined;

function niceDate(iso) {
  const d = new Date(iso + "T12:00:00");
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}
function countdown(days) {
  if (days > 1) return "in " + days + " days";
  if (days === 1) return "tomorrow";
  if (days === 0) return "today";
  return (-days) + (days === -1 ? " day ago" : " days ago");
}
function ago(iso) {
  const mins = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (mins < 2) return "just now";
  if (mins < 60) return mins + " minutes ago";
  if (mins < 48 * 60) return Math.round(mins / 60) + " hours ago";
  return Math.round(mins / 1440) + " days ago";
}
function safeLink(url, text, prefix) {
  if (typeof url !== "string" || !url.startsWith(prefix || "https://")) return null;
  const a = el("a", null, text); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
  return a;
}
function avatar(ticker) {
  let h = 0; for (const ch of ticker) h = (h * 31 + ch.charCodeAt(0)) % 360;
  const a = el("span", "avatar", ticker.slice(0, 2));
  a.style.background = "linear-gradient(135deg, hsl(" + h + " 62% 46%), hsl(" + ((h + 40) % 360) + " 62% 34%))";
  return a;
}
function empty(title, text) {
  const box = el("div", "empty");
  box.append(el("strong", null, title), document.createTextNode(text || ""));
  return box;
}
function table(headers, rows) {
  const wrap = el("div", "scroll"), t = el("table"), head = el("tr");
  headers.forEach(h => head.append(el("th", null, h)));
  t.append(head);
  rows.forEach(cells => {
    const tr = el("tr");
    cells.forEach(c => {
      const td = el("td");
      if (c instanceof Node) td.append(c);
      else if (c && c.wrap) { td.className = "wrap"; td.textContent = c.text; }
      else if (c && c.cls) { td.className = c.cls; td.textContent = c.text; }
      else td.textContent = c;
      tr.append(td);
    });
    t.append(tr);
  });
  wrap.append(t);
  return wrap;
}

// ---- wording shared by the list, the details and the signal cards ----
function moveText(m) {
  if (!has(m)) return "\\u2014";
  return m === 0 ? "None priced" : "\\u00b1" + pct0(m);
}
function costText(rank, estimated) {
  if (!has(rank)) return { big: "No reading", sub: "needs live option prices" };
  const word = rank < 30 ? "Cheap" : (rank > 50 ? "Expensive" : "Not cheap");
  return { big: word, sub: Math.round(rank) + " of 100" + (estimated ? ", estimate" : "") };
}
const MARKS = { pass: "\\u2713", soft: "\\u2713", fail: "\\u2715", unknown: "?" };
function checkCell(label, k) {
  const box = el("div", "cell check " + k.state), text = el("div");
  text.append(el("div", "label", label), el("div", "big", k.big), el("div", "sub", k.sub));
  box.append(el("span", "mark", MARKS[k.state] || "?"), text);
  return box;
}
function reviewFact(c) {
  const a = c.review, box = el("div", "fact evreview");
  box.append(el("h3", null, c.review_drives ? "Why this evidence score" : "Our evidence review"));
  const head = el("div", "head");
  head.append(el("span", "score", a.score + " / 100"),
              el("span", "muted", [a.drug, a.indication].filter(Boolean).join(" for ")));
  box.append(head, el("p", null, a.summary), el("p", "muted", a.endpoint + " " + a.quality));
  const two = el("div", "two");
  [["for", "In favour", a.positives], ["against", "Risks", a.risks]].forEach(([cls, title, items]) => {
    if (!items.length) return;
    const col = el("div", cls), ul = el("ul");
    items.forEach(t => ul.append(el("li", null, t)));
    col.append(el("h4", null, title), ul); two.append(col);
  });
  box.append(two);
  const src = el("p", "muted");
  src.append(document.createTextNode((c.review_drives ? "This is the evidence score the verdict uses. " : "It becomes the evidence score at the next scan. ")
    + "It is written by an AI model reading the sources below; it is a reading, not a measured probability. Sources: "));
  let filings = 0;
  a.sources.forEach((u, i) => {
    let name;
    try { const host = new URL(u).hostname.replace(/^www\\./, ""); name = host === "sec.gov" ? "company filing " + (++filings) : host; } catch (e) { return; }
    const l = safeLink(u, name); if (!l) return;
    if (i) src.append(document.createTextNode(", ")); src.append(l);
  });
  src.append(document.createTextNode(". Most of what is published about an unapproved drug comes from the company, which presents its results favourably. Reviewed " + ago(a.reviewed_at) + "."));
  box.append(src);
  return box;
}
function legendItem(state, text) {
  const box = el("span", "check " + state);
  box.append(el("span", "mark", MARKS[state]), document.createTextNode(text));
  return box;
}
function statusPill(c) { return el("span", "status " + c.status, c.status_label); }

function cell(label, big, sub, extra) {
  const box = el("div", "cell");
  box.append(el("div", "label", label), el("div", "big num", big));
  if (sub) box.append(el("div", "sub", sub));
  if (extra) box.append(extra);
  return box;
}

function catalystItem(c, summary) {
  const item = el("div", "item");
  const key = c.ticker + "|" + c.date + "|" + c.event_type;
  const open = view.open.has(key);
  const row = el("button", "row" + (open ? " open" : ""));
  row.type = "button";
  row.setAttribute("aria-expanded", open ? "true" : "false");

  const who = el("div", "who"), names = el("div");
  names.append(el("div", "t", c.ticker), el("div", "c", c.company || ""));
  who.append(avatar(c.ticker), names);

  const statusCell = el("div", "cell"); statusCell.append(el("div", "label", "Verdict"), statusPill(c));
  const when = c.phase === "window" ? "entry window open" : (has(c.window_opens_in) ? "entry window opens in " + c.window_opens_in + (c.window_opens_in === 1 ? " day" : " days") : null);
  const eventCell = cell(c.event_label, niceDate(c.date), countdown(c.days_until));
  if (when) eventCell.append(el("div", "sub", when));

  const evidenceCell = checkCell("Trial evidence", c.checks[0]);
  if (c.review && !c.review_drives) evidenceCell.lastChild.append(el("div", "ai" + (c.review_disagrees ? " warn" : ""), "Evidence review: " + c.review.score + (c.review_disagrees ? " \u26a0" : "")));

  row.append(
    who,
    eventCell,
    evidenceCell,
    checkCell("Expected move", c.checks[1]),
    checkCell("Options cost", c.checks[2]),
    statusCell,
    el("span", "chev", "\\u25b6"),
  );
  row.addEventListener("click", () => { open ? view.open.delete(key) : view.open.add(key); render(); });
  item.append(row);
  if (open) item.append(catalystDetail(c, summary));
  return item;
}

function fact(title, lines) {
  const box = el("div", "fact");
  box.append(el("h3", null, title));
  lines.filter(Boolean).forEach(line => {
    if (line instanceof Node) { const p = el("p"); p.append(line); box.append(p); }
    else box.append(el("p", null, line));
  });
  return box;
}

function catalystDetail(c, summary) {
  const d = el("div", "detail");
  d.append(fact("Verdict: " + c.status_label, [c.status_detail,
    c.review_disagrees ? "Caution: the evidence check passes on our score, but our evidence review of what has been published is only " + c.review.score + " out of 100. See its reasons below before relying on this." : null,
    c.status_action,
    c.checks_passed + " of 3 checks pass." + (c.phase === "window" ? " It is inside its entry window." : "")]));

  const kind = c.event_type === "ADCOM" ? "an FDA advisory committee meeting" : "an FDA decision";
  d.append(fact("The event", [
    (c.company || c.ticker) + " has " + kind + " on " + niceDate(c.date) + " (" + countdown(c.days_until) + ").",
    c.drug ? "Drug or trial named in the filing: " + c.drug + "." : null,
    safeLink(c.source_url, "Read the company filing"),
  ]));

  const trial = c.nct_id && /^NCT\\d{8}$/.test(c.nct_id) ? safeLink("https://clinicaltrials.gov/study/" + c.nct_id, "Trial " + c.nct_id + " on ClinicalTrials.gov") : null;
  d.append(fact("Trial evidence", [
    has(c.evidence) ? "Evidence score " + Math.round(c.evidence * 100) + " out of 100." : "Not scored yet.",
    c.trial_note,
    trial,
  ]));

  if (c.review) d.append(reviewFact(c));

  let options;
  if (!has(c.event_move)) options = "No reading yet. It needs live prices on the options that expire just before and just after the decision.";
  else if (c.event_move === 0) options = "The options market is pricing no extra move for this decision. Traders are not treating it as a major event for the company.";
  else options = "The options market is pricing an extra move of about " + moveText(c.event_move) + " on the decision, in either direction.";
  const cost = costText(c.iv_rank, c.iv_rank_estimated);
  d.append(fact("Options market", [
    options,
    has(c.iv_rank) ? "Options cost: " + cost.big.toLowerCase() + " (" + Math.round(c.iv_rank) + " of 100) against the stock's own past year."
                   : "No reading of what its options cost yet. It needs live option prices at the next scan.",
    c.iv_rank_estimated ? "This is an estimate: today's option prices measured against how much the stock itself moved over the past year. It leans toward calling options expensive. Our own daily readings replace it after " + summary.iv_days_needed + " trading days." : null,
  ]));
  return d;
}

function matches(c) {
  if (view.filter === "window" && c.phase !== "window") return false;
  if (view.filter === "best" && !["signal", "avoid", "setup", "close"].includes(c.status)) return false;
  if (view.filter === "priced" && !(c.event_move > 0)) return false;
  const q = view.query.trim().toLowerCase();
  return !q || c.ticker.toLowerCase().includes(q) || (c.company || "").toLowerCase().includes(q);
}

function renderCatalysts(s) {
  const box = document.getElementById("catalysts");
  const rows = s.catalysts.filter(matches);
  if (!s.catalysts.length) { box.replaceChildren(empty("No upcoming FDA decisions on record yet.", "They appear after the next scan.")); return; }
  if (!rows.length) { box.replaceChildren(empty("Nothing matches.", "Try a different filter or clear the search.")); return; }
  const list = el("div", "list"), cols = el("div", "cols");
  ["Company", "Event", "1 \\u00b7 Trial evidence", "2 \\u00b7 Expected move", "3 \\u00b7 Options cost", "Verdict", ""].forEach(h => cols.append(el("div", null, h)));
  list.append(cols);
  rows.forEach(c => list.append(catalystItem(c, s.summary)));
  box.replaceChildren(list);
}

// ---- one sentence at the top: is there anything to act on today? ----
function tickers(list) {
  const names = [...new Set(list.map(c => c.ticker))];
  return names.slice(0, 4).join(", ") + (names.length > 4 ? " and " + (names.length - 4) + " more" : "");
}
function renderToday(s) {
  const box = document.getElementById("today"), rows = s.catalysts;
  const of = (status) => rows.filter(c => c.status === status);
  const buys = of("signal").filter(c => c.status_label === "Buy setup"), stockOnly = of("signal").filter(c => c.status_label !== "Buy setup");
  const setups = of("setup"), close = of("close");
  let cls = "", head, text;
  if (!s.summary.scored) {
    head = "Nothing scored yet.";
    text = "Scores appear after the next scan, which runs every trading day late in the US session.";
  } else if (buys.length) {
    cls = "act";
    head = "Today: " + buys.length + (buys.length === 1 ? " buy setup" : " buy setups") + " (" + tickers(buys) + ").";
    text = "All three checks pass inside the entry window. They are at the top of the list. Click one to see why.";
  } else if (stockOnly.length) {
    cls = "act";
    head = "Today: " + stockOnly.length + (stockOnly.length === 1 ? " stock-only alert" : " stock-only alerts") + " (" + tickers(stockOnly) + ").";
    text = "Very strong trial evidence, but options are expensive or barely traded, so not all three checks pass. Click it to see why.";
  } else if (setups.length) {
    cls = "watch";
    head = "Today: nothing to act on.";
    text = "Worth watching: " + tickers(setups) + (setups.length === 1 ? " passes" : " pass") + " all three checks, but too early. You are alerted if that still holds when the entry window opens.";
  } else if (close.length) {
    head = "Today: nothing to act on.";
    text = close.length + (close.length === 1 ? " company passes" : " companies pass") + " two of the three checks (" + tickers(close) + "). None passes all three.";
  } else {
    head = "Today: nothing to act on.";
    text = "No company is close to a setup. That is common. Most FDA decisions never become one.";
  }
  box.className = "today " + cls;
  box.replaceChildren(el("b", null, head), el("span", null, text));
}

// The introduction shows until dismissed; the choice is remembered in this
// browser only, and the page works the same if storage is unavailable.
const INTRO_KEY = "aegis.intro.hidden";
function showIntro(show, remember) {
  document.getElementById("intro").hidden = !show;
  document.getElementById("intro-open").hidden = show;
  if (remember) { try { localStorage.setItem(INTRO_KEY, show ? "0" : "1"); } catch (e) { /* private window */ } }
}
let introHidden = false;
try { introHidden = localStorage.getItem(INTRO_KEY) === "1"; } catch (e) { /* private window */ }
showIntro(!introHidden, false);
document.getElementById("intro-close").addEventListener("click", () => showIntro(false, true));
document.getElementById("intro-open").addEventListener("click", () => showIntro(true, true));

function kpi(label, value, sub) {
  const box = el("div", "kpi");
  box.append(el("div", "k", label), el("div", "v num", value), el("div", "s", sub));
  return box;
}

function renderKpis(s) {
  const m = s.summary, t = s.track_record;
  const next = m.next;
  document.getElementById("kpis").replaceChildren(
    kpi("FDA decisions tracked", String(m.tracked), m.scored + " scored, at companies under $20B"),
    kpi("Active alerts", String(m.signals), m.signals ? "inside their entry window today" : "no company qualifies today"),
    kpi("Setting up", String(m.setting_up), m.setting_up ? "pass all 3 checks, window not open yet" : m.close + " more pass 2 of 3 checks"),
    kpi("Next decision", next ? next.ticker : "\\u2014", next ? niceDate(next.date) + " \\u00b7 " + countdown(next.days_until) : "none scheduled"),
    kpi("Track record", t.decisions ? t.approved + " of " + t.decisions : "\\u2014", t.decisions ? "recorded FDA decisions were approvals" : "no decisions recorded yet"),
  );
  const market = document.getElementById("market");
  market.className = "pill" + (m.market_open ? " open" : "");
  market.textContent = m.market_open ? "US market open" : "US market closed";
}

function stat(k, v, s) {
  const box = el("div", "stat");
  box.append(el("div", "k", k), el("div", "v num", v));
  if (s) box.append(el("div", "s", s));
  return box;
}

function alertCard(a, s) {
  const p = a.payload, sig = p.signal.arbitrage_signal;
  const card = el("div", "card " + sig);
  const head = el("div", "card-head");
  const label = s.signal_labels[sig] || words(sig);
  head.append(el("span", "ticker", p.ticker), el("span", "status " + (sig === "STRUCTURAL_COLLAPSE_AVOID" ? "avoid" : "signal"), label),
              el("span", "muted", "FDA decision " + niceDate(p.catalyst.date)), el("span", "when", "raised " + ago(a.received_at)));
  card.append(head);

  const cost = costText(p.market.iv_rank, p.market.iv_rank_estimated);
  const stats = el("div", "stats");
  stats.append(
    stat("Trial evidence", String(Math.round(p.clinical.p_fundamental * 100)), "out of 100"),
    stat("Expected move", moveText(p.market.event_implied_move), "read from options prices"),
    stat("Options cost", cost.big, cost.sub),
    stat("Stock price", money(p.market.spot_price), "when the alert was sent"),
    stat("Size ceiling", (p.execution.allocation_pct * 100).toFixed(1) + "%", "of an account, at most"),
  );
  card.append(stats);

  const det = el("details");
  det.append(el("summary", null, "Why, and the plan"));
  const reviewed = p.clinical.data_completeness === "EVIDENCE_REVIEW";
  const adj = el("ul");
  p.clinical.adjustments.forEach(x => adj.append(el("li", null, reviewed ? x.reason : (x.delta >= 0 ? "+" : "") + Math.round(x.delta * 100) + " points: " + x.reason)));
  if (p.clinical.adjustments.length) det.append(el("p", "muted", reviewed ? "Why the evidence score:" : "Evidence score starts at " + Math.round(p.clinical.base_rate * 100) + ", then:"), adj);
  const exits = el("ul");
  p.execution.exit_triggers.filter(t => t.active).forEach(t =>
    exits.append(el("li", null, t.trigger_date ? "Exit by " + niceDate(t.trigger_date) : t.description)));
  if (has(p.execution.trailing_stop_pct)) exits.append(el("li", null, "Stop: sell if the price falls " + pct0(p.execution.trailing_stop_pct) + " from its highest point"));
  if (exits.children.length) det.append(el("p", "muted", "The system's plan, for reference (" + (p.execution.use_options ? "options" : "shares, not options") + "):"), exits);
  card.append(det);
  return card;
}

function renderSignals(s) {
  const box = document.getElementById("alerts"), m = s.summary;
  if (s.alerts.length) { box.replaceChildren(...s.alerts.map(a => alertCard(a, s))); return; }
  box.replaceChildren(empty("No alerts yet.",
    "No company has qualified inside its entry window. " + m.setting_up + " pass all three ahead of their window and " + m.close
    + " pass two of three; they are at the top of the Upcoming decisions list."));
}

function renderRecord(s) {
  const box = document.getElementById("trackrecord"), t = s.track_record;
  if (!t.decisions) { box.replaceChildren(empty("No FDA decisions recorded yet.", "They are added as each decision date passes.")); return; }
  const kp = el("div", "kpis");
  const moves = t.rows.filter(r => has(r.price_change_pct));
  const fell = moves.filter(r => r.outcome === "APPROVED" && r.price_change_pct < 0).length;
  const approvedMoves = moves.filter(r => r.outcome === "APPROVED").length;
  kp.append(
    kpi("Decisions recorded", String(t.decisions), "since tracking began"),
    kpi("Approved", String(t.approved), Math.round(100 * t.approved / t.decisions) + "% of decisions"),
    kpi("Rejected", String(t.rejected), "the FDA said no"),
    kpi("Fell after approval", approvedMoves ? fell + " of " + approvedMoves : "\\u2014", "approved stocks that dropped anyway"),
    kpi("Scored beforehand", String(t.scored), t.scored ? "average evidence score " + Math.round(t.average_score * 100) : "accuracy can't be judged yet"),
  );
  const rows = table(
    ["Company", "Decided", "Outcome", "Stock reaction", "Evidence score before", "Expected move before"],
    t.rows.map(r => {
      const outcome = el("span", "status " + (r.outcome === "APPROVED" ? "signal" : "avoid"), r.outcome === "APPROVED" ? "Approved" : "Rejected");
      const move = !has(r.price_change_pct) ? "pending" : (r.price_change_pct >= 0 ? "+" : "") + (r.price_change_pct * 100).toFixed(1) + "%";
      return [
        { cls: "num", text: r.ticker }, niceDate(r.decision_date), outcome,
        { cls: "num " + (!has(r.price_change_pct) ? "muted" : (r.price_change_pct >= 0 ? "up" : "down")), text: move },
        { cls: "num", text: has(r.p_fundamental) ? String(Math.round(r.p_fundamental * 100)) : "not scored" },
        { cls: "num", text: has(r.event_move_priced) ? moveText(r.event_move_priced) : "no reading" },
      ];
    }));
  box.replaceChildren(kp, rows);
}

function renderPhase3(s) {
  const box = document.getElementById("phase3"), list = s.phase3_watch || [];
  if (!list.length) { box.replaceChildren(empty("Nothing on the watchlist yet.", "")); return; }
  box.replaceChildren(table(
    ["Company", "Data collection ends", "Trial", "Registry"],
    list.map(t => [
      { wrap: true, text: (t.ticker || "") + "  \\u00b7  " + (t.company_name || "") },
      niceDate(t.completion_date),
      { wrap: true, text: (t.trial ? t.trial + ": " : "") + (t.title || "") },
      safeLink(t.source_url, t.nct_id || "open", "https://clinicaltrials.gov/") || (t.nct_id || "n/a"),
    ])));
}

function render() {
  const s = view.state;
  if (!s) return;
  renderToday(s); renderKpis(s); renderCatalysts(s); renderSignals(s); renderRecord(s); renderPhase3(s);
  document.getElementById("legend").replaceChildren(
    legendItem("pass", "passes"), legendItem("soft", "passes, but on limited information"),
    legendItem("fail", "does not pass"), legendItem("unknown", "no reading yet"));
  document.getElementById("count-catalysts").textContent = s.catalysts.length || "";
  document.getElementById("count-signals").textContent = s.alerts.length || "";
  document.getElementById("count-record").textContent = s.track_record.decisions || "";
  document.getElementById("count-phase3").textContent = (s.phase3_watch || []).length || "";
}

function showTab(name) {
  view.tab = name;
  document.querySelectorAll(".tab").forEach(b => { const on = b.dataset.tab === name; b.classList.toggle("on", on); b.setAttribute("aria-selected", on ? "true" : "false"); });
  document.querySelectorAll("[data-panel]").forEach(p => { p.hidden = p.dataset.panel !== name; });
}
document.querySelectorAll(".tab").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
document.querySelectorAll("#filters .chip").forEach(b => b.addEventListener("click", () => {
  view.filter = b.dataset.filter;
  document.querySelectorAll("#filters .chip").forEach(x => x.classList.toggle("on", x === b));
  render();
}));
document.getElementById("search").addEventListener("input", (e) => { view.query = e.target.value; if (view.state) renderCatalysts(view.state); });

async function refresh() {
  const status = document.getElementById("status");
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    if (r.status === 401) { window.location.href = "/login"; return; }
    if (!r.ok) throw new Error("HTTP " + r.status);
    view.state = await r.json();
    render();
    status.className = "";
    status.textContent = view.state.summary.last_scan ? "Last scan " + ago(view.state.summary.last_scan) : "No scan yet";
  } catch (e) {
    status.className = "bad";
    status.textContent = "Can't reach the server. Retrying\\u2026";
  }
}
refresh();
setInterval(refresh, REFRESH_MS);
"""
