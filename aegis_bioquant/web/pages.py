"""
web/pages.py
-------------
HTML for the hosted site, as Jinja templates.

Autoescaping is on for every template: anything a user typed (an email
address, a form value) is escaped when rendered, so it cannot inject
markup. The dashboard's own styles and sections are shared with the
local viewer (telemetry/dashboard.py) rather than copied.

The wording on the landing page is placeholder copy. It describes what
the software does and makes no performance claims; the "not investment
advice" footer is a standard placeholder, not legal review.
"""

from __future__ import annotations

from jinja2 import DictLoader, Environment, select_autoescape

from aegis_bioquant.telemetry.dashboard_ui import DASHBOARD_SECTIONS, FAVICON_LINK, FONT_LINKS, LOGO_SVG, PAGE_STYLE

_SITE_STYLE = """
  /* ---- top bar ---- */
  .topline { position: sticky; top: 0; z-index: 20; background: rgba(8,11,16,.82); backdrop-filter: blur(14px);
             -webkit-backdrop-filter: blur(14px); border-bottom: 1px solid var(--line); }
  .nav { display: flex; align-items: center; gap: 6px 22px; padding: 12px 20px; max-width: 1240px; margin: 0 auto; flex-wrap: wrap; }
  .nav .brand { display: inline-flex; align-items: center; gap: 10px; font-weight: 700; font-size: 16px; letter-spacing: -0.01em;
                color: var(--text); margin-right: auto; }
  .nav .brand:hover { text-decoration: none; }
  .nav .brand em { font-style: normal; color: var(--muted); font-weight: 500; }
  .nav a { color: var(--muted); font-weight: 500; }
  .nav a:hover { color: var(--text); text-decoration: none; }
  .nav a.here { color: var(--text); }
  .nav a.here::after { content: ""; display: block; height: 2px; margin-top: 3px; border-radius: 2px;
                       background: linear-gradient(90deg, var(--brand-1), var(--brand-2)); }
  .nav form { margin: 0; }

  /* ---- buttons and forms ---- */
  button, .button { font: inherit; font-weight: 600; cursor: pointer; border-radius: 10px; padding: 10px 18px; border: 0;
                    color: #06101f; background: linear-gradient(135deg, var(--brand-1), var(--brand-2));
                    text-decoration: none; display: inline-block; box-shadow: 0 6px 20px -8px rgba(91,157,255,.7); }
  button:hover, .button:hover { filter: brightness(1.08); text-decoration: none; }
  .nav a.button, .nav a.button:hover { color: #06101f; padding: 8px 16px; }
  button.plain, .button.plain { background: var(--panel-2); color: var(--text); border: 1px solid var(--line-2); box-shadow: none; }
  button.link { background: none; border: 0; padding: 0; color: var(--muted); font-weight: 500; box-shadow: none; }
  button.link:hover { color: var(--text); filter: none; }
  .notice button.link { color: var(--equity); text-decoration: underline; }
  button.tab, button.chip, button.row { box-shadow: none; }
  button.tab { background: none; color: var(--muted); border-radius: 0; }
  button.tab.on, button.tab:hover { color: var(--text); filter: none; }
  button.chip { background: var(--panel); color: var(--muted); border: 1px solid var(--line); border-radius: 999px; padding: 6px 13px; font-weight: 500; }
  button.chip.on { color: var(--text); background: rgba(91,157,255,.14); border-color: rgba(91,157,255,.5); }
  button.chip:hover, button.row:hover { filter: none; }
  button.row { background: none; color: inherit; border-radius: 0; font-weight: 400; }
  label { display: block; font-size: 13px; color: var(--muted); margin: 14px 0 6px; font-weight: 500; }
  input[type=email], input[type=password], input[type=url], input[type=text] {
    width: 100%; font: inherit; color: var(--text); background: var(--bg-2); border: 1px solid var(--line-2);
    border-radius: 10px; padding: 11px 13px; }
  input[type=date] { font: inherit; color: var(--text); background: var(--bg-2); border: 1px solid var(--line-2);
                     border-radius: 10px; padding: 9px 11px; }
  input[type=radio] { accent-color: var(--brand-1); }
  input:focus-visible, button:focus-visible, a:focus-visible { outline: 2px solid var(--brand-1); outline-offset: 2px; }
  .field-hint { font-size: 12px; color: var(--muted); margin: 6px 0 0; }
  .form-actions { margin-top: 18px; display: flex; flex-wrap: wrap; align-items: center; gap: 14px; }
  .inline { display: inline; }

  /* ---- shared blocks ---- */
  .notice { border-radius: 12px; padding: 12px 16px; margin: 0 0 18px; border: 1px solid var(--line-2); background: var(--panel-2); }
  .notice.error { border-color: rgba(244,89,108,.5); background: rgba(244,89,108,.08); }
  .notice.ok { border-color: rgba(47,208,138,.45); background: rgba(47,208,138,.08); }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 20px 22px; }
  .panel h3 { margin: 0 0 6px; font-size: 15px; }
  .panel p { margin: 0; color: var(--muted); }
  .setting { margin-bottom: 16px; }
  .setting h2, .review h2 { font-size: 17px; text-transform: none; letter-spacing: -0.01em; color: var(--text); margin: 0 0 6px; }
  .setting p, .review p { color: var(--text); }
  .setting p.muted, .review p.muted, .review .facts { color: var(--muted); }
  .state { font-weight: 600; }
  .state.on { color: var(--long); } .state.off { color: var(--avoid); } .state.none { color: var(--muted); }
  .pagehead { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; margin-bottom: 22px; }
  .pagehead h1 { font-size: 26px; font-weight: 700; letter-spacing: -0.02em; margin: 0; }
  .subnav { margin: -10px 0 22px; color: var(--muted); }
  .review .facts { margin: 0 0 12px; }
  .evidence { border-left: 3px solid var(--line-2); padding: 4px 0 4px 14px; margin: 8px 0 12px; color: var(--muted) !important; }
  .choices { display: grid; gap: 6px; margin: 8px 0; }
  .choices label { margin: 0; color: var(--text); font-size: 14px; display: flex; gap: 8px; align-items: baseline; font-weight: 400; }
  .option { display: flex; gap: 10px; align-items: flex-start; padding: 10px 0; border-top: 1px solid var(--line); font-weight: 400; }
  .option input { margin-top: 4px; }
  .option .meta { color: var(--muted); font-size: 12px; }
  .option .warn { color: var(--avoid); }
  .narrow { max-width: 420px; margin: 48px auto; }
  .authcard { background: linear-gradient(180deg, var(--panel-2), var(--panel)); border: 1px solid var(--line); border-radius: 18px;
              padding: 30px 30px 26px; box-shadow: 0 30px 80px -40px rgba(0,0,0,.9); }
  .authcard h1 { font-size: 24px; letter-spacing: -0.02em; margin: 0 0 4px; }
  .authcard .lead { color: var(--muted); margin: 0 0 12px; }
  .authcard button[type=submit] { width: 100%; padding: 12px; }
  .narrow h1 { font-size: 24px; margin: 0 0 12px; letter-spacing: -0.02em; }

  /* ---- landing page ---- */
  .hero { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, .95fr); gap: 48px; align-items: center; padding: 56px 0 64px; }
  .eyebrow { display: inline-flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 600; letter-spacing: 0.08em;
             text-transform: uppercase; color: var(--brand-2); background: rgba(47,214,181,.08);
             border: 1px solid rgba(47,214,181,.3); border-radius: 999px; padding: 5px 12px; }
  .hero h1 { font-size: clamp(34px, 5.2vw, 58px); line-height: 1.04; letter-spacing: -0.035em; margin: 18px 0 18px; font-weight: 800;
             text-wrap: balance; }
  .hero h1 span { background: linear-gradient(90deg, var(--brand-1), var(--brand-2)); -webkit-background-clip: text;
                  background-clip: text; color: transparent; }
  .hero p.lead { font-size: 18px; color: var(--muted); margin: 0 0 28px; max-width: 52ch; }
  .actions { display: flex; flex-wrap: wrap; gap: 12px; }
  .actions .button { padding: 13px 22px; font-size: 15px; }
  .proof { display: flex; flex-wrap: wrap; gap: 8px 26px; margin-top: 28px; color: var(--muted); font-size: 13px; }
  .proof b { color: var(--text); font-family: var(--mono); font-weight: 600; }
  .preview { background: linear-gradient(180deg, var(--panel-2), var(--panel)); border: 1px solid var(--line-2); border-radius: 18px;
             padding: 18px; box-shadow: 0 40px 120px -50px rgba(91,157,255,.55), 0 30px 60px -40px rgba(0,0,0,.9); }
  .preview .cap { display: flex; justify-content: space-between; align-items: center; color: var(--muted); font-size: 12px; margin-bottom: 12px; }
  .preview .cap span:last-child { border: 1px solid var(--line-2); border-radius: 999px; padding: 2px 9px; }
  .prow { display: grid; grid-template-columns: 1.5fr 1fr 1fr auto; gap: 12px; align-items: center; padding: 12px 10px;
          border-top: 1px solid var(--line); }
  .prow .t { font-weight: 700; } .prow .c { color: var(--muted); font-size: 12px; }
  .prow .n { font-family: var(--mono); font-weight: 600; } .prow .l { color: var(--dim); font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
  .band { border-top: 1px solid var(--line); padding: 56px 0; }
  .band > h2 { font-size: 30px; text-transform: none; letter-spacing: -0.025em; color: var(--text); margin: 0 0 8px; font-weight: 700; }
  .band > p.lead { color: var(--muted); font-size: 16px; margin: 0 0 28px; max-width: 62ch; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 14px; }
  .feature { background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 22px; }
  .feature .ico { width: 38px; height: 38px; border-radius: 11px; display: grid; place-items: center; margin-bottom: 14px;
                  background: rgba(91,157,255,.12); border: 1px solid rgba(91,157,255,.3); color: var(--brand-1); }
  .feature h3 { margin: 0 0 6px; font-size: 16px; letter-spacing: -0.01em; }
  .feature p { margin: 0; color: var(--muted); }
  .steps { counter-reset: step; list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 14px; }
  .steps li { counter-increment: step; background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 22px; color: var(--muted); }
  .steps li::before { content: "0" counter(step); display: block; font-family: var(--mono); font-weight: 600; font-size: 13px;
                      color: var(--brand-2); margin-bottom: 10px; letter-spacing: .04em; }
  .steps li strong { display: block; color: var(--text); font-size: 16px; margin-bottom: 4px; }
  .cta { text-align: center; background: linear-gradient(180deg, rgba(91,157,255,.10), rgba(47,214,181,.04)); border: 1px solid var(--line-2);
         border-radius: 22px; padding: 48px 24px; margin: 8px 0 24px; }
  .cta h2 { font-size: 30px; text-transform: none; letter-spacing: -0.025em; color: var(--text); margin: 0 0 8px; font-weight: 700; }
  .cta p { color: var(--muted); margin: 0 0 22px; }
  .cta .actions { justify-content: center; }

  footer { max-width: 1240px; margin: 0 auto; padding: 26px 20px 44px; color: var(--dim); font-size: 12px; border-top: 1px solid var(--line); }
  @media (max-width: 900px) { .hero { grid-template-columns: 1fr; padding: 36px 0 44px; gap: 32px; } .band { padding: 40px 0; } }
"""

_BASE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}FDAOdds · FDA catalyst intelligence{% endblock %}</title>
{{ font_links|safe }}
<style>{{ page_style|safe }}{{ site_style|safe }}</style>
</head>
<body>
<div class="topline">
  <nav class="nav">
    <a class="brand" href="/">{{ logo_svg|safe }}<span>FDA<em>Odds</em></span></a>
    {% if session %}
      <a href="/dashboard"{% if here == "dashboard" %} class="here"{% endif %}>Dashboard</a>
      <a href="/settings"{% if here == "settings" %} class="here"{% endif %}>Alert settings</a>
      {% if is_admin(session) %}<a href="/review"{% if here == "review" %} class="here"{% endif %}>Review</a>{% endif %}
      <form method="post" action="/logout">
        <input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
        <button class="link" type="submit">Log out</button>
      </form>
    {% else %}
      <a href="/login"{% if here == "login" %} class="here"{% endif %}>Log in</a>
      <a class="button" href="/signup">Create account</a>
    {% endif %}
  </nav>
</div>
<main>
{% if session and verify_needed %}
  <div class="notice" role="status">
    Confirm your email address to connect alert channels. We sent a link to {{ session.email }}.
    <form class="inline" method="post" action="/verify/resend">
      <input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
      <button class="link" type="submit">Send it again</button>
    </form>
  </div>
{% endif %}
{% block content %}{% endblock %}
</main>
<footer>
  FDAOdds publishes automated, model-generated signals for information only. It is not investment
  advice, and nothing here is a recommendation to buy or sell any security. Trading options and biotech
  stocks around regulatory events can lose your entire position. FDAOdds is independent and is not affiliated with, or endorsed by, the U.S. Food and Drug Administration.
</footer>
{% block scripts %}{% endblock %}
</body>
</html>
"""

_LANDING = """{% extends "base" %}
{% block content %}
<div class="hero">
  <div>
    <span class="eyebrow">Early access</span>
    <h1>Know which FDA decisions matter, <span>before the market does.</span></h1>
    <p class="lead">FDAOdds tracks upcoming FDA decisions at small and mid-sized drug makers, scores each drug's
       trial evidence, and reads what the options market expects. When strong evidence meets options that are still
       cheap, you get an alert on Telegram or Discord.</p>
    <div class="actions">
      {% if session %}
        <a class="button" href="/dashboard">Open dashboard</a>
      {% else %}
        <a class="button" href="/signup">Create account</a>
        <a class="button plain" href="/login">Log in</a>
      {% endif %}
    </div>
    <div class="proof">
      <span><b>Daily</b> scan in market hours</span>
      <span><b>SEC + FDA</b> source filings</span>
      <span><b>Telegram</b> and <b>Discord</b> alerts</span>
    </div>
  </div>
  <div class="preview" aria-label="Example of the dashboard">
    <div class="cap"><span>Upcoming FDA decisions</span><span>Illustration</span></div>
    <div class="prow"><div><div class="t">ABCD</div><div class="c">in 41 days</div></div>
      <div><div class="l">Evidence</div><div class="n">88</div></div>
      <div><div class="l">Expected move</div><div class="n">&plusmn;24%</div></div>
      <span class="status signal">Buy setup</span></div>
    <div class="prow"><div><div class="t">EFGH</div><div class="c">in 62 days</div></div>
      <div><div class="l">Evidence</div><div class="n">86</div></div>
      <div><div class="l">Expected move</div><div class="n">&plusmn;18%</div></div>
      <span class="status setup">Setting up</span></div>
    <div class="prow"><div><div class="t">IJKL</div><div class="c">in 38 days</div></div>
      <div><div class="l">Evidence</div><div class="n">83</div></div>
      <div><div class="l">Expected move</div><div class="n">None</div></div>
      <span class="status">No setup</span></div>
    <div class="prow"><div><div class="t">MNOP</div><div class="c">in 44 days</div></div>
      <div><div class="l">Evidence</div><div class="n">31</div></div>
      <div><div class="l">Expected move</div><div class="n">&plusmn;35%</div></div>
      <span class="status avoid">Avoid</span></div>
  </div>
</div>

<div class="band">
  <h2>Three questions, answered for every decision</h2>
  <p class="lead">Each upcoming FDA decision gets one row with the three things that decide whether it is worth your attention.</p>
  <div class="grid">
    <div class="feature">
      <div class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6v5l4 9a3 3 0 0 1-3 4H8a3 3 0 0 1-3-4l4-9z"/><path d="M8 14h8"/></svg></div>
      <h3>How strong is the evidence?</h3>
      <p>An evidence score from 0 to 100, built from the trial's published results: how clearly it worked, and what went wrong on safety.</p>
    </div>
    <div class="feature">
      <div class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l5-6 4 3 6-8"/><path d="M15 6h3v3"/></svg></div>
      <h3>How big a move is expected?</h3>
      <p>The move the options market is pricing for the decision itself, separated from the stock's everyday swings.</p>
    </div>
    <div class="feature">
      <div class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></div>
      <h3>Is it still early?</h3>
      <p>Whether options are cheap or expensive against the stock's own past year, so you can see if the crowd has arrived yet.</p>
    </div>
  </div>
</div>

<div class="band">
  <h2>How it works</h2>
  <p class="lead">No setup beyond connecting a chat. The scan runs on its own every trading day.</p>
  <ol class="steps">
    <li><strong>We find the dates</strong>FDA decision dates are collected from company filings with the SEC and from the FDA's own calendar.</li>
    <li><strong>We score each one</strong>Trial evidence, the move options are pricing, and whether those options are still cheap.</li>
    <li><strong>You get one verdict per company</strong>Buy setup, Setting up, Close, No setup or Avoid. When one becomes a Buy setup it also goes to your Telegram or Discord, with the reasons.</li>
  </ol>
</div>

<div class="cta">
  <h2>See every upcoming FDA decision in one place</h2>
  <p>Create an account to open the dashboard and connect your alerts.</p>
  <div class="actions">
    {% if session %}<a class="button" href="/dashboard">Open dashboard</a>
    {% else %}<a class="button" href="/signup">Create account</a><a class="button plain" href="/login">Log in</a>{% endif %}
  </div>
</div>
{% endblock %}
"""

_AUTH = """{% extends "base" %}
{% block title %}{{ heading }} · FDAOdds{% endblock %}
{% block content %}
<div class="narrow">
 <div class="authcard">
  <h1>{{ heading }}</h1>
  <p class="lead">{{ "Welcome back." if action == "/login" else "Free during early access. No card needed." }}</p>
  {% if notice %}<div class="notice {{ notice.kind }}" role="status">{{ notice.text }}</div>{% endif %}
  {% if error %}<div class="notice error" role="alert">{{ error }}</div>{% endif %}
  <form method="post" action="{{ action }}">
    <label for="email">Email</label>
    <input id="email" name="email" type="email" autocomplete="email" required value="{{ email }}">
    <label for="password">Password</label>
    <input id="password" name="password" type="password" required
           autocomplete="{{ 'new-password' if action == '/signup' else 'current-password' }}"
           {% if action == '/signup' %}minlength="{{ password_min }}"{% endif %}>
    {% if action == '/signup' %}<p class="field-hint">At least {{ password_min }} characters.</p>{% endif %}
    <div class="form-actions">
      <button type="submit">{{ heading }}</button>
      {% if action == '/signup' %}<a href="/login">I already have an account</a>
      {% else %}<a href="/signup">Create an account</a>
        {% if reset_available %}<a href="/forgot">Forgot password?</a>{% endif %}{% endif %}
    </div>
  </form>
 </div>
</div>
{% endblock %}
"""

_FORGOT = """{% extends "base" %}
{% block title %}Reset password · FDAOdds{% endblock %}
{% block content %}
<div class="narrow">
  <h1>Reset your password</h1>
  <p class="muted">Enter your account's email address and we'll send a link to choose a new password.</p>
  <form method="post" action="/forgot">
    <label for="email">Email</label>
    <input id="email" name="email" type="email" autocomplete="email" required>
    <div class="form-actions">
      <button type="submit">Send reset link</button>
      <a href="/login">Back to log in</a>
    </div>
  </form>
</div>
{% endblock %}
"""

_RESET = """{% extends "base" %}
{% block title %}Choose a new password · FDAOdds{% endblock %}
{% block content %}
<div class="narrow">
  <h1>Choose a new password</h1>
  {% if error %}<div class="notice error" role="alert">{{ error }}</div>{% endif %}
  <form method="post" action="/reset">
    <input type="hidden" name="token" value="{{ token }}">
    <label for="password">New password</label>
    <input id="password" name="password" type="password" required autocomplete="new-password"
           minlength="{{ password_min }}">
    <p class="field-hint">At least {{ password_min }} characters. You'll be logged out everywhere else.</p>
    <div class="form-actions"><button type="submit">Set new password</button></div>
  </form>
</div>
{% endblock %}
"""

_MESSAGE = """{% extends "base" %}
{% block title %}{{ heading }} · FDAOdds{% endblock %}
{% block content %}
<div class="narrow">
  <h1>{{ heading }}</h1>
  <p>{{ text }}</p>
  <div class="form-actions"><a class="button" href="{{ link_url }}">{{ link_text }}</a></div>
</div>
{% endblock %}
"""

_DASHBOARD = """{% extends "base" %}
{% block title %}Dashboard · FDAOdds{% endblock %}
{% block content %}
{% if not has_destination %}
  <div class="notice">You have not connected Telegram or Discord yet, so alerts will only appear on this page.
    <a href="/settings">Connect one now</a></div>
{% endif %}
{{ dashboard_sections|safe }}
{% endblock %}
{% block scripts %}<script src="/static/dashboard.js"></script>{% endblock %}
"""

_SETTINGS = """{% extends "base" %}
{% block title %}Alert settings · FDAOdds{% endblock %}
{% block content %}
<div class="pagehead"><h1>Alert settings</h1><span class="muted">{{ session.email }}</span></div>
{% if notice %}<div class="notice {{ notice.kind }}" role="status">{{ notice.text }}</div>{% endif %}
<p class="muted">Choose where your alerts are sent. You can connect one or both. An alert goes out the day a company qualifies, with the reasons.</p>

<div class="panel setting">
  <h2>Telegram</h2>
  {% if telegram %}
    {% if telegram.enabled %}
      <p><span class="state on">Connected.</span> Alerts are sent to your Telegram chat. To stop them, press Disconnect here or send /stop to the bot.</p>
    {% else %}
      <p><span class="state off">Switched off.</span> Several alerts in a row could not be delivered to your
         chat. Connect again to turn it back on.</p>
    {% endif %}
  {% else %}
    <p><span class="state none">Not connected.</span>
       {% if telegram_available %}Connecting opens Telegram; press Start there to finish.{% endif %}</p>
  {% endif %}
  <div class="form-actions">
    {% if verify_needed %}
      <span class="muted">Confirm your email address to connect Telegram.</span>
    {% elif telegram_available %}
      <form class="inline" method="post" action="/settings/telegram/connect">
        <input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
        <button type="submit">{{ "Reconnect Telegram" if telegram else "Connect Telegram" }}</button>
      </form>
    {% else %}
      <span class="muted">Telegram alerts aren't available yet.</span>
    {% endif %}
    {% if telegram %}
      <form class="inline" method="post" action="/settings/telegram/remove">
        <input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
        <button class="plain" type="submit">Disconnect</button>
      </form>
    {% endif %}
  </div>
</div>

<div class="panel setting">
  <h2>Discord</h2>
  {% if discord %}
    {% if discord.enabled %}
      <p><span class="state on">Connected.</span> Alerts are posted to your Discord channel (webhook {{ discord_label }}).</p>
    {% else %}
      <p><span class="state off">Switched off.</span> Several alerts in a row could not be delivered to
         webhook {{ discord_label }}. Save a webhook again to turn it back on.</p>
    {% endif %}
  {% else %}
    <p><span class="state none">Not connected.</span></p>
  {% endif %}
  {% if verify_needed %}
    <p class="muted">Confirm your email address to connect Discord.</p>
  {% else %}
  <form method="post" action="/settings/discord">
    <input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
    <label for="webhook_url">Discord webhook URL</label>
    <input id="webhook_url" name="webhook_url" type="url" required autocomplete="off"
           placeholder="https://discord.com/api/webhooks/…">
    <p class="field-hint">A webhook is a private address that lets us post into one channel. In Discord: open the channel's settings, then Integrations, Webhooks, New Webhook, Copy Webhook URL, and paste it here.</p>
    <div class="form-actions"><button type="submit">Save webhook</button></div>
  </form>
  {% endif %}
  {% if discord %}
    <form method="post" action="/settings/discord/remove" class="form-actions">
      <input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
      <button class="plain" type="submit">Remove webhook</button>
    </form>
  {% endif %}
</div>
{% endblock %}
"""

_REVIEW = """{% extends "base" %}
{% block title %}Review · FDAOdds{% endblock %}
{% block content %}
<div class="pagehead"><h1>Review</h1>
  <span class="muted">Confirm which clinical trial each catalyst is about. Confirmed catalysts are scored at the next scan.</span></div>
<p class="subnav"><strong>Trials</strong> · <a href="/review/outcomes">Outcomes</a></p>
{% if notice %}<div class="notice {{ notice.kind }}" role="status">{{ notice.text }}</div>{% endif %}
{% if not items %}
  <div class="empty">Nothing in an entry window as of the last scan.</div>
{% endif %}
{% for item in items %}
<div class="panel setting review">
  <h2>{{ item.ticker }} · {{ item.event_type }} {{ item.catalyst_date }}</h2>
  <p class="facts">{{ item.company_name or "Company not recorded" }}
    {% if item.candidate %} · the filing mentions “{{ item.candidate }}”{% endif %}
    {% if item.source_url %} · <a href="{{ item.source_url }}" target="_blank" rel="noopener noreferrer">source</a>{% endif %}</p>

  {% if item.confirmed %}
    <p><span class="state on">Confirmed:</span> {{ item.confirmed.nct_id }} — {{ item.confirmed.trial_title }}</p>
    <form method="post" action="/review/remove" class="form-actions">
      <input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
      <input type="hidden" name="ticker" value="{{ item.ticker }}">
      <input type="hidden" name="event_type" value="{{ item.event_type }}">
      <input type="hidden" name="catalyst_date" value="{{ item.catalyst_date }}">
      <button class="plain" type="submit">Remove confirmation</button>
    </form>
  {% else %}
    {% if item.auto %}
      <p><span class="state on">Matched automatically</span> to {{ item.current_nct }}. Confirm a different trial below only if that is wrong.</p>
    {% elif item.current_nct %}
      <p><span class="state off">Unconfirmed guess:</span> {{ item.current_nct }}. Not scored until a trial is confirmed.</p>
    {% else %}
      <p><span class="state none">No trial matched.</span> Not scored until a trial is confirmed.</p>
    {% endif %}
    <form method="post" action="/review/confirm">
      <input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
      <input type="hidden" name="ticker" value="{{ item.ticker }}">
      <input type="hidden" name="event_type" value="{{ item.event_type }}">
      <input type="hidden" name="catalyst_date" value="{{ item.catalyst_date }}">
      {% if item.options %}
        {% for o in item.options %}
          <label class="option" style="margin:0">
            <input type="radio" name="nct_choice" value="{{ o.nct_id }}"{% if o.nct_id == item.current_nct %} checked{% endif %}>
            <span>
              <strong>{{ o.nct_id }}</strong>{% if o.acronym %} · {{ o.acronym }}{% endif %} — {{ o.brief_title }}
              <span class="meta"><br>{{ o.phases|join(", ")|replace("PHASE", "Phase ") or "Phase not stated" }}
                · {{ (o.overall_status or "status unknown")|replace("_", " ")|lower }}
                {% if o.primary_completion %} · primary completion {{ o.primary_completion }}{% endif %}
                · {% if o.has_results %}results posted{% else %}<span class="warn">no results posted — the score would be the bare base rate</span>{% endif %}
              </span>
            </span>
          </label>
        {% endfor %}
      {% elif item.lookup_failed %}
        <p class="muted">The trial registry could not be reached, so no suggestions are shown. You can still enter a trial ID.</p>
      {% else %}
        <p class="muted">The registry lists no trials for this company under that name.</p>
      {% endif %}
      <label for="nct-{{ loop.index }}">Or enter a trial ID</label>
      <input id="nct-{{ loop.index }}" name="nct_manual" type="text" placeholder="NCT01234567" autocomplete="off"
             pattern="[Nn][Cc][Tt][0-9]{8}">
      <p class="field-hint">A typed ID is used instead of the selection above.</p>
      <div class="form-actions"><button type="submit">Confirm trial</button></div>
    </form>
  {% endif %}
</div>
{% endfor %}
{% endblock %}
"""

_OUTCOMES = """{% extends "base" %}
{% block title %}Outcomes · FDAOdds{% endblock %}
{% block content %}
<div class="pagehead"><h1>Review</h1>
  <span class="muted">Record what the FDA decided. Only outcomes confirmed here count in the track record.</span></div>
<p class="subnav"><a href="/review">Trials</a> · <strong>Outcomes</strong></p>
{% if notice %}<div class="notice {{ notice.kind }}" role="status">{{ notice.text }}</div>{% endif %}

<h2>Waiting for an outcome</h2>
{% if not items %}
  <div class="empty">No catalyst has reached its decision date without an outcome recorded.</div>
{% endif %}
{% for item in items %}
<div class="panel setting review">
  <h2>{{ item.ticker }} · PDUFA {{ item.catalyst_date }}</h2>
  <p class="facts">{{ item.company_name or "Company not recorded" }}
    {% if item.candidate %} · “{{ item.candidate }}”{% endif %}
    · {% if item.days_past >= 0 %}decision date was {{ item.days_past }} day(s) ago{% else %}decision date is in {{ -item.days_past }} day(s){% endif %}
    {% if item.source_url %} · <a href="{{ item.source_url }}" target="_blank" rel="noopener noreferrer">filing that gave the date</a>{% endif %}</p>

  {% if item.suggestion %}
    <p><span class="state on">Found in a filing dated {{ item.suggestion.decision_date }}:</span>
       this looks like <strong>{{ labels[item.suggestion.outcome] }}</strong>. Read it before confirming — it is a text match, and it can be about a different drug.</p>
    <p class="evidence">“{{ item.suggestion.text }}”
      {% if item.suggestion.url %}<br><a href="{{ item.suggestion.url }}" target="_blank" rel="noopener noreferrer">open the filing</a>{% endif %}</p>
  {% elif item.checked %}
    <p><span class="state none">No decision found in the company's filings yet.</span> It is looked up again every couple of days.</p>
  {% else %}
    <p><span class="state none">Not looked up yet.</span> The next scan checks the company's filings.</p>
  {% endif %}

  <form method="post" action="/review/outcomes/confirm">
    <input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
    <input type="hidden" name="ticker" value="{{ item.ticker }}">
    <input type="hidden" name="catalyst_date" value="{{ item.catalyst_date }}">
    <div class="choices">
      {% for value in outcome_values %}
        <label><input type="radio" name="outcome" value="{{ value }}" required
               {% if item.suggestion and item.suggestion.outcome == value %}checked{% endif %}> {{ labels[value] }}</label>
      {% endfor %}
    </div>
    <label for="when-{{ loop.index }}">Date of the decision</label>
    <input id="when-{{ loop.index }}" name="decision_date" type="date" required max="{{ today }}"
           value="{{ item.suggestion.decision_date if item.suggestion else (item.catalyst_date if item.days_past >= 0 else today) }}">
    <p class="field-hint">The day the FDA acted, which can be before the PDUFA date. The stock's reaction is measured around this day.</p>
    <div class="form-actions"><button type="submit">Record outcome</button></div>
  </form>
</div>
{% endfor %}

<h2 style="margin-top:32px">Recorded</h2>
{% if not recorded %}
  <div class="empty">No outcomes recorded yet.</div>
{% else %}
<div class="scroll"><table>
  <tr><th>Ticker</th><th>PDUFA date</th><th>Outcome</th><th>Decided</th><th>Stock reaction</th><th></th></tr>
  {% for r in recorded %}
  <tr>
    <td>{{ r.ticker }}</td><td>{{ r.catalyst_date }}</td><td>{{ labels[r.outcome] }}</td><td>{{ r.decision_date }}</td>
    <td>{% if r.price_change_pct is not none %}{{ "%+.1f"|format(r.price_change_pct * 100) }}%{% elif r.outcome in ("APPROVED", "CRL") %}pending{% else %}n/a{% endif %}</td>
    <td><form class="inline" method="post" action="/review/outcomes/undo">
      <input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
      <input type="hidden" name="ticker" value="{{ r.ticker }}">
      <input type="hidden" name="catalyst_date" value="{{ r.catalyst_date }}">
      <button class="link" type="submit">Undo</button></form></td>
  </tr>
  {% endfor %}
</table></div>
{% endif %}
{% endblock %}
"""

_env = Environment(
    loader=DictLoader(
        {
            "base": _BASE,
            "landing": _LANDING,
            "auth": _AUTH,
            "forgot": _FORGOT,
            "reset": _RESET,
            "message": _MESSAGE,
            "review": _REVIEW,
            "outcomes": _OUTCOMES,
            "dashboard": _DASHBOARD,
            "settings": _SETTINGS,
        }
    ),
    autoescape=select_autoescape(default=True, default_for_string=True),
)


# Whether a session belongs to an administrator is decided by web/app.py
# (it depends on settings); the nav bar on every page needs the answer.
# Registered as a template function so no page has to pass it in.
_env.globals["is_admin"] = lambda session: False


def set_admin_check(check) -> None:
    _env.globals["is_admin"] = check


def render(template: str, **context) -> str:
    return _env.get_template(template).render(
        page_style=PAGE_STYLE,
        site_style=_SITE_STYLE,
        font_links=FONT_LINKS + FAVICON_LINK,
        logo_svg=LOGO_SVG,
        dashboard_sections=DASHBOARD_SECTIONS,
        **context,
    )
