PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Pune Rain &amp; Safety</title>
<style>
@property --tint{syntax:"<color>";inherits:true;initial-value:#7a8b98}
:root{--bg1:#f6f8fb;--bg2:#e8eef3;--ink:#10202b;--muted:#5b6d7a;--paper:#ffffff;--card:rgba(255,255,255,.74);--line:rgba(16,32,43,.09);--shadow:0 1px 2px rgba(16,32,43,.05),0 14px 34px rgba(16,32,43,.08);--track:rgba(16,32,43,.08);--tint:#7a8b98;--ease:cubic-bezier(.22,1,.36,1)}
@media (prefers-color-scheme:dark){:root{--bg1:#090e13;--bg2:#121b23;--ink:#eef4f8;--muted:#98a8b4;--paper:#1a242d;--card:rgba(28,38,48,.68);--line:rgba(255,255,255,.09);--shadow:0 1px 2px rgba(0,0,0,.35),0 18px 42px rgba(0,0,0,.38);--track:rgba(255,255,255,.10)}}
body{--tint:#7a8b98;transition:--tint .9s ease}
body[data-band="Low"]{--tint:#139a93}
body[data-band="Watch"]{--tint:#d98a1f}
body[data-band="High"]{--tint:#d6453d}
@media (prefers-color-scheme:dark){body[data-band="Low"]{--tint:#38c7bd}body[data-band="Watch"]{--tint:#f2a93c}body[data-band="High"]{--tint:#ff6f63}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;color:var(--ink);background:linear-gradient(180deg,var(--bg1),var(--bg2)) fixed;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Segoe UI Variable Display","Segoe UI",Roboto,system-ui,sans-serif;-webkit-font-smoothing:antialiased;line-height:1.45}
.wrap{max-width:680px;margin:0 auto;padding:max(16px,env(safe-area-inset-top)) 18px max(44px,env(safe-area-inset-bottom))}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:6px 4px 10px}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:15px;letter-spacing:-.01em}
.brand svg{width:20px;height:20px;fill:var(--tint);transition:fill .9s ease}
.top nav{display:flex;gap:16px;font-size:14px;white-space:nowrap}
.brand{min-width:0}
a{color:var(--ink);text-decoration:none;font-weight:600;opacity:.8}
a:hover{opacity:1}
a:focus-visible,button:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--tint);outline-offset:3px;border-radius:10px}
.card{background:var(--card);-webkit-backdrop-filter:blur(20px) saturate(1.25);backdrop-filter:blur(20px) saturate(1.25);border:1px solid var(--line);border-radius:28px;box-shadow:var(--shadow);padding:22px;margin-top:14px}
.hero{text-align:center;padding:24px 20px 24px;background:radial-gradient(120% 70% at 50% 0%,color-mix(in srgb,var(--tint) 17%,transparent),transparent 62%),var(--card);transition:background .9s ease}
.banner{display:flex;align-items:center;justify-content:space-between;gap:10px;text-align:left;background:var(--ink);color:var(--paper);border-radius:16px;padding:10px 12px 10px 16px;margin-bottom:16px;font-size:14px;font-weight:650}
.banner[hidden]{display:none}
.banner button{flex:none;white-space:nowrap;border:0;border-radius:999px;padding:7px 13px;background:var(--paper);color:var(--ink);font:inherit;font-size:13px;font-weight:700;cursor:pointer;transition:transform .2s var(--ease)}
.banner button:active{transform:scale(.95)}
.orb{position:relative;width:min(62vw,236px);aspect-ratio:1;margin:4px auto 6px}
.orb svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
.orbBg{fill:color-mix(in srgb,var(--tint) 8%,var(--paper));transition:fill .9s ease}
.orbRing{fill:none;stroke:color-mix(in srgb,var(--tint) 50%,transparent);stroke-width:1.4}
#water{transition:transform 1.9s var(--ease)}
.wave{animation:drift 9s linear infinite}
.wave.b{animation-duration:14s;animation-direction:reverse}
@keyframes drift{from{transform:translateX(0)}to{transform:translateX(-100px)}}
.orbText{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;pointer-events:none}
.pct{font-size:clamp(54px,15vw,74px);font-weight:300;letter-spacing:-.035em;line-height:1;font-variant-numeric:tabular-nums}
.pct small{font-size:.42em;font-weight:400;margin-left:2px;letter-spacing:0}
.pctLabel{margin-top:8px;font-size:12.5px;font-weight:650;letter-spacing:.02em;color:var(--ink);opacity:.72;line-height:1.25}
h1{font-size:clamp(25px,6.6vw,33px);font-weight:700;letter-spacing:-.025em;line-height:1.12;margin:16px 0 8px}
.sub{margin:0 auto;max-width:36ch;color:var(--muted);font-size:16.5px}
.chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:16px}
.chip{display:inline-flex;align-items:center;gap:7px;border:0;border-radius:999px;padding:7px 13px;background:color-mix(in srgb,var(--tint) 15%,transparent);color:var(--ink);font:inherit;font-size:13px;font-weight:650;transition:background .9s ease}
.chip.plain{background:var(--track);font-weight:600;color:var(--muted)}
.chip i{width:8px;height:8px;border-radius:50%;background:var(--tint);transition:background .9s ease}
button.chip{cursor:pointer;transition:transform .2s var(--ease),background .9s ease}
button.chip:active{transform:scale(.95)}
.eyebrow{margin:0 0 16px;font-size:12.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--muted)}
.gauges{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.gauge{display:flex;align-items:flex-end;gap:14px}
.tube{position:relative;flex:none;width:16px;height:96px;border-radius:10px;background:var(--track);overflow:hidden}
.tube::after{content:"";position:absolute;inset:0;background:repeating-linear-gradient(180deg,transparent 0,transparent 23px,color-mix(in srgb,var(--paper) 70%,transparent) 23px,color-mix(in srgb,var(--paper) 70%,transparent) 24px);pointer-events:none}
.tube i{position:absolute;left:0;right:0;bottom:0;height:0;border-radius:10px;background:linear-gradient(180deg,color-mix(in srgb,var(--tint) 65%,#fff),var(--tint));transition:height 1.5s var(--ease),background .9s ease}
.gauge.sm .tube{height:66px;width:14px}
.gv{font-size:36px;font-weight:300;letter-spacing:-.025em;line-height:1;font-variant-numeric:tabular-nums}
.gv small{font-size:15px;font-weight:500;color:var(--muted);margin-left:3px;letter-spacing:0}
.gauge.sm .gv{font-size:28px}
.gl{margin-top:6px;font-size:14px;color:var(--muted);font-weight:500}
.note{margin:16px 0 0;font-size:13px;color:var(--muted)}
select{width:100%;appearance:none;-webkit-appearance:none;border:1px solid var(--line);border-radius:16px;background:var(--paper) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='9' fill='none' stroke='%238a9aa6' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M1 1.5l6 6 6-6'/%3E%3C/svg%3E") no-repeat right 16px center;color:var(--ink);font:inherit;font-size:17px;padding:14px 42px 14px 16px;margin-bottom:18px;transition:border-color .2s}
select:hover{border-color:color-mix(in srgb,var(--tint) 50%,var(--line))}
.tips{list-style:none;margin:0;padding:0;display:grid;gap:12px}
.tips li{position:relative;padding-left:30px;font-size:16.5px}
.tips li::before{content:"";position:absolute;left:2px;top:.5em;width:12px;height:12px;border-radius:50%;background:color-mix(in srgb,var(--tint) 24%,transparent);box-shadow:inset 0 0 0 3px var(--tint);transition:all .9s ease}
.tips.std li{font-size:15.5px;color:var(--muted)}
.tips.std li::before{background:var(--track);box-shadow:none}
.rule{height:1px;background:var(--line);margin:18px 0}
.official{display:flex;gap:12px;align-items:flex-start}
.official .dot{flex:none;width:10px;height:10px;border-radius:50%;margin-top:7px;background:#98a8b4}
.official .dot.good{background:#2f9e6b}.official .dot.watch{background:#d98a1f}
.official h3{margin:0 0 4px;font-size:17px;font-weight:650;letter-spacing:-.01em}
.official p{margin:0;color:var(--muted);font-size:15px;white-space:pre-line}
.official a{display:inline-block;margin-top:10px;font-size:15px;color:var(--tint)}
details summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;align-items:center;font-weight:650;font-size:17px}
details summary::-webkit-details-marker{display:none}
details summary::after{content:"+";font-size:22px;font-weight:300;color:var(--muted);transition:transform .35s var(--ease)}
details[open] summary::after{transform:rotate(45deg)}
dl{display:grid;grid-template-columns:1fr auto;gap:10px 16px;margin:18px 0 0;font-size:15px}
dt{color:var(--muted)}dd{margin:0;text-align:right;font-weight:650;font-variant-numeric:tabular-nums}
.fine{margin:16px 0 0;color:var(--muted);font-size:13.5px}
.replay{display:flex;gap:10px;flex-wrap:wrap}
.replay select{flex:1;min-width:220px;margin:0;font-size:16px;padding:12px 40px 12px 14px}
.btn{border:0;border-radius:14px;padding:12px 20px;background:var(--ink);color:var(--paper);font:inherit;font-size:16px;font-weight:650;cursor:pointer;transition:transform .2s var(--ease),opacity .2s}
.btn:active{transform:scale(.96)}
.btn:disabled{opacity:.45;cursor:default}
.dim{opacity:.5}
.mapHead{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.mapHead .eyebrow{margin:0}
.seg{display:inline-flex;background:var(--track);border-radius:999px;padding:3px}
.seg button{border:0;background:transparent;color:var(--muted);font:inherit;font-size:12.5px;font-weight:650;padding:7px 11px;white-space:nowrap;border-radius:999px;cursor:pointer;transition:background .35s var(--ease),color .35s}
.seg button.on{background:var(--ink);color:var(--paper)}
.mapBox{position:relative;border-radius:22px;overflow:hidden;aspect-ratio:1000/860;background:radial-gradient(110% 90% at 50% 0%,#16262f 0%,#0a1219 72%);border:1px solid rgba(255,255,255,.07);box-shadow:inset 0 0 60px rgba(0,0,0,.4);touch-action:pan-y;user-select:none;-webkit-user-select:none}
.mapBox svg{position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:pan-y}
#vp{transform-origin:0 0}
#riversG path{fill:none;stroke:rgba(110,185,230,.5);stroke-width:1.1;vector-effect:non-scaling-stroke;stroke-linecap:round;stroke-linejoin:round}
.w{fill:#22313b;stroke:rgba(255,255,255,.17);stroke-width:.7;vector-effect:non-scaling-stroke;fill-rule:evenodd;cursor:pointer;transition:fill .9s ease;animation:wardIn .8s ease both}
@keyframes wardIn{from{opacity:0}to{opacity:1}}
#hlHover,#hlSel{fill:none;vector-effect:non-scaling-stroke;pointer-events:none;stroke-linejoin:round;fill-rule:evenodd}
#hlHover{stroke:rgba(255,255,255,.85);stroke-width:1.5}
#hlSel{stroke:#fff;stroke-width:2.3;filter:drop-shadow(0 0 5px rgba(255,255,255,.55))}
#labelsG{opacity:0;transition:opacity .45s ease;pointer-events:none}
#labelsG text{text-anchor:middle;dominant-baseline:middle;fill:rgba(240,247,252,.92);paint-order:stroke;stroke:rgba(8,14,20,.8);stroke-linejoin:round;font-family:inherit;font-weight:600}
.mePulse{fill:rgba(90,200,250,.4);transform-box:fill-box;transform-origin:center;animation:pulse 2.2s ease-out infinite}
.meDot{fill:#5ac8fa;stroke:#fff;stroke-width:2;vector-effect:non-scaling-stroke}
@keyframes pulse{from{transform:scale(.6);opacity:.9}to{transform:scale(3.2);opacity:0}}
.tip{position:absolute;z-index:4;pointer-events:none;background:rgba(14,22,30,.88);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);color:#eef4f8;border:1px solid rgba(255,255,255,.1);border-radius:12px;padding:8px 11px;font-size:13px;line-height:1.35;max-width:220px}
.tip b{display:block;font-size:13.5px}
.tip[hidden]{display:none}
.mapBtns{position:absolute;right:12px;top:12px;z-index:3;display:grid;gap:8px}
.mapBtns button{width:38px;height:38px;border-radius:12px;border:1px solid rgba(255,255,255,.13);background:rgba(14,22,30,.74);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);color:#eef4f8;font:inherit;font-size:18px;line-height:1;cursor:pointer;transition:transform .2s var(--ease),background .2s}
.mapBtns button:active{transform:scale(.92)}
.mapBtns button:hover{background:rgba(30,44,56,.85)}
.legend{position:absolute;left:12px;bottom:12px;z-index:3;color:#cfdce6;font-size:11.5px;font-weight:600;background:rgba(14,22,30,.68);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);padding:8px 11px 9px;border-radius:12px;border:1px solid rgba(255,255,255,.08)}
.legend .lbar{width:128px;height:6px;border-radius:6px;margin:5px 0 4px;background:linear-gradient(90deg,#183642,#1f5d69,#e0b252,#f08a3c,#e5473d)}
.legend .lrow{display:flex;justify-content:space-between;font-weight:500;color:#9fb2c0}
.mapTag{position:absolute;left:12px;top:12px;z-index:3;background:#fff;color:#10202b;border-radius:999px;padding:6px 12px;font-size:12.5px;font-weight:700;white-space:nowrap;max-width:calc(100% - 70px);overflow:hidden;text-overflow:ellipsis}
.mapTag[hidden]{display:none}
.mapMsg{position:absolute;inset:0;z-index:2;display:flex;align-items:center;justify-content:center;text-align:center;padding:24px;color:#9fb2c0;font-size:15px}
.mapMsg[hidden]{display:none}
.repBar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-top:12px;font-size:14px;color:var(--muted)}
.repBar a{color:var(--tint);font-weight:650;white-space:nowrap}
#repG circle.pin{stroke:#fff;stroke-width:1.6;vector-effect:non-scaling-stroke}
#repG circle.ring{fill:none;stroke-width:1.4;vector-effect:non-scaling-stroke;transform-box:fill-box;transform-origin:center;animation:ringPulse 2.4s ease-out infinite}
@keyframes ringPulse{from{transform:scale(1);opacity:.85}to{transform:scale(3.2);opacity:0}}
.wardPanel{margin-top:14px;padding:16px 18px 18px;border-radius:20px;background:var(--track);min-height:70px}
.wardPanel .hint{color:var(--muted);font-size:15px}
.wardPanel .ph{display:flex;justify-content:space-between;align-items:center;gap:10px}
.wardPanel h3{margin:0;font-size:19px;font-weight:700;letter-spacing:-.015em}
.wardPanel .sum{margin:8px 0 0;color:var(--muted);font-size:15px}
.prow{display:grid;grid-template-columns:1fr auto;gap:6px 12px;align-items:center;margin-top:14px;font-size:14.5px}
.prow span:first-child{color:var(--muted)}
.prow strong{font-variant-numeric:tabular-nums;font-weight:650}
.bar{grid-column:1/-1;height:6px;border-radius:6px;background:rgba(127,140,150,.24);overflow:hidden}
.bar i{display:block;height:100%;width:0;border-radius:6px;transition:width 1s var(--ease),background .6s}
.swapIn{animation:swapIn .5s var(--ease) both}
@keyframes swapIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){.w,.mePulse,.swapIn,#repG circle.ring{animation:none}.bar i,#labelsG,.w{transition:none}}
.foot{margin:26px 4px 0;text-align:center;color:var(--muted);font-size:13px}
.js .reveal{opacity:0;transform:translateY(18px)}
.js .reveal.in{opacity:1;transform:none;transition:opacity .8s ease,transform .9s var(--ease)}
@media (max-width:420px){.card{padding:20px 18px;border-radius:26px}.gv{font-size:32px}.legend{left:8px;bottom:8px;padding:6px 8px 7px;font-size:10.5px}.legend .lbar{width:84px}.seg button{padding:7px 10px;font-size:12.5px;white-space:nowrap}.mapBtns{right:8px;top:8px}.mapBtns button{width:34px;height:34px}}
@media (prefers-reduced-motion:reduce){.wave{animation:none}#water,.tube i,body,.hero,.orbBg,.chip,.chip i{transition:none}.js .reveal,.js .reveal.in{opacity:1;transform:none;transition:none}}
</style>
</head>
<body data-band="none">
<script>document.documentElement.classList.add("js")</script>
<div class="wrap">
  <header class="top reveal">
    <div class="brand"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.2c3.6 4.5 6.4 8 6.4 11.4a6.4 6.4 0 0 1-12.8 0C5.6 10.2 8.4 6.7 12 2.2z"/></svg>Pune Rain &amp; Safety</div>
    <nav><a href="/">Console</a><a href="https://mausam.imd.gov.in/" target="_blank" rel="noopener noreferrer">Official IMD ↗</a></nav>
  </header>

  <section class="card hero reveal" aria-live="polite">
    <div id="banner" class="banner" hidden><span id="bannerText"></span><button id="backLive" type="button">Back to live</button></div>
    <div class="orb" id="orb" role="img" aria-label="Chance of heavy rain">
      <svg viewBox="0 0 200 200" aria-hidden="true">
        <defs>
          <clipPath id="clip"><circle cx="100" cy="100" r="92"/></clipPath>
          <linearGradient id="wg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--tint)" stop-opacity=".40"/><stop offset="1" stop-color="var(--tint)" stop-opacity=".66"/></linearGradient>
        </defs>
        <circle class="orbBg" cx="100" cy="100" r="92"/>
        <g clip-path="url(#clip)">
          <g id="water" style="transform:translateY(212px)">
            <g class="wave b"><path d="M-100 20 Q-75 6 -50 20 T0 20 T50 20 T100 20 T150 20 T200 20 T250 20 T300 20 V240 H-100 Z" fill="url(#wg)" opacity=".55"/></g>
            <g class="wave"><path d="M-100 14 Q-75 0 -50 14 T0 14 T50 14 T100 14 T150 14 T200 14 T250 14 T300 14 V240 H-100 Z" fill="url(#wg)"/></g>
          </g>
        </g>
        <circle class="orbRing" cx="100" cy="100" r="92"/>
      </svg>
      <div class="orbText"><div class="pct"><span id="pctNum">–</span><small id="pctSign">%</small></div><div class="pctLabel">chance of heavy rain<br>next 24 hours</div></div>
    </div>
    <h1 id="headline">Checking the forecast…</h1>
    <p class="sub" id="subline">Fetching the latest model forecast.</p>
    <div class="chips"><span class="chip" id="bandChip"><i></i><span id="bandText">Checking</span></span><span class="chip plain" id="metaChip"></span><button class="chip plain" id="refreshBtn" type="button">↻ Refresh</button></div>
  </section>

  <section class="card reveal" id="obsCard" hidden>
    <p class="eyebrow">Rain recorded · last 24 hours</p>
    <div id="obsRows"></div>
    <p class="note" id="obsNote"></p>
  </section>

  <section class="card reveal">
    <p class="eyebrow">Expected rain · Pune average</p>
    <div class="gauges">
      <div class="gauge"><div class="tube"><i id="tube6"></i></div><div><div class="gv" id="val6">—</div><div class="gl">next 6 hours</div></div></div>
      <div class="gauge"><div class="tube"><i id="tube24"></i></div><div><div class="gv" id="val24">—</div><div class="gl" id="lab24">next 24 hours</div></div></div>
    </div>
    <p class="note">Gauges fill up to 65 mm, close to the level where our experimental rain watch begins.</p>
  </section>

  <section class="card reveal" id="mapCard">
    <div class="mapHead">
      <p class="eyebrow">Risk map</p>
      <div class="seg" role="group" aria-label="Map view"><button type="button" class="on" data-mode="now">Right now</button><button type="button" data-mode="base">Always vulnerable</button><button type="button" data-mode="learned">From reports</button></div>
    </div>
    <div class="mapBox" id="mapBox">
      <svg id="mapSvg" viewBox="0 0 1000 860" role="img" aria-label="Map of Pune wards coloured by flood-risk index. Use the ward selector for keyboard access."></svg>
      <div class="mapTag" id="mapTag" hidden></div>
      <div class="mapBtns"><button type="button" id="zIn" aria-label="Zoom in">+</button><button type="button" id="zOut" aria-label="Zoom out">−</button><button type="button" id="zFit" aria-label="Reset view">⤢</button><button type="button" id="locate" aria-label="Find my ward">◎</button></div>
      <div class="legend"><div id="legCap">Risk index</div><div class="lbar"></div><div class="lrow"><span>Lower</span><span>Higher</span></div></div>
      <div class="tip" id="mapTip" hidden></div>
      <div class="mapMsg" id="mapMsg">Loading map…</div>
    </div>
    <div class="repBar"><span id="repText">Checking citizen reports…</span><a id="repLink" href="#" target="_blank" rel="noopener noreferrer" hidden>Report on Telegram ↗</a></div>
    <div class="wardPanel" id="wardPanel" aria-live="polite"><p class="hint">Hover or tap a ward to see its risk.</p></div>
    <p class="note" id="mapNote">Colours show a model-estimated advisory index, not an official flood map. Ctrl + scroll or the + / − buttons zoom the map.</p>
  </section>

  <section class="card reveal" id="areaCard">
    <p class="eyebrow">Your area</p>
    <label for="ward" class="gl" style="display:block;margin:0 0 8px">Choose a Pune ward</label>
    <select id="ward"><option value="">Choose a Pune ward</option></select>
    <div class="gauges">
      <div class="gauge sm"><div class="tube"><i id="wtube6"></i></div><div><div class="gv" id="wval6">—</div><div class="gl">next 6 hours</div></div></div>
      <div class="gauge sm"><div class="tube"><i id="wtube24"></i></div><div><div class="gv" id="wval24">—</div><div class="gl">next 24 hours</div></div></div>
    </div>
    <p class="note" id="wardNote">Ward-centre estimate, not an exact street reading.</p>
  </section>

  <section class="card reveal">
    <p class="eyebrow">What to do</p>
    <ul class="tips" id="tips"></ul>
    <div class="rule"></div>
    <ul class="tips std">
      <li>Never walk or drive through floodwater, and avoid underpasses.</li>
      <li>Stay away from riverbanks and fast-flowing water.</li>
      <li>Follow IMD and local-authority advice; move to higher ground if water is rising.</li>
    </ul>
  </section>

  <section class="card reveal" aria-live="polite">
    <p class="eyebrow">Official information</p>
    <div class="official"><span class="dot" id="imdDot"></span><div><h3 id="imdTitle">Checking IMD…</h3><p id="imdText"></p><a href="https://mausam.imd.gov.in/" target="_blank" rel="noopener noreferrer">Open IMD updates ↗</a></div></div>
  </section>

  <details class="card reveal" id="tech">
    <summary>Under the hood</summary>
    <dl id="techList"></dl>
    <p class="fine" id="techNote"></p>
  </details>

  <section class="card reveal">
    <p class="eyebrow">Demo · replay a past event</p>
    <div class="replay"><select id="replaySel" aria-label="Past event to replay"><option value="">Loading events…</option></select><button class="btn" id="replayGo" type="button" disabled>Replay</button></div>
    <p class="note">Replays a real past day using stored forecasts. Nothing is sent to anyone.</p>
  </section>

  <p class="foot">Forecasts are estimates, not rain-gauge readings. This is not an official warning. Check IMD and local authorities during severe weather.</p>
</div>

<script>
(() => {
  const $ = id => document.getElementById(id);
  const reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const SCALE = 65;
  const state = {fc: null, ml: null, wards: [], imd: null, replay: null};
  let shownPct = 0, lastTick = null;

  const num = v => (v === null || v === undefined || v === "" || !Number.isFinite(Number(v))) ? null : Number(v);
  const mm = v => num(v) === null ? "—" : Number(v).toFixed(1);
  const mmHtml = v => num(v) === null ? "—" : Number(v).toFixed(1) + "<small>mm</small>";
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const timeOnly = v => { if (!v) return null; const d = new Date(v); return isNaN(d) ? null : d.toLocaleTimeString("en-IN", {hour: "numeric", minute: "2-digit"}); };
  const dateTime = v => { if (!v) return null; const d = new Date(v); return isNaN(d) ? String(v) : d.toLocaleString("en-IN", {day: "numeric", month: "short", hour: "numeric", minute: "2-digit"}); };
  async function getJson(url) { const r = await fetch(url, {cache: "no-store"}); const d = await r.json().catch(() => { throw new Error("bad response"); }); if (!r.ok) { const e = new Error(d && d.reason || "failed"); e.data = d; throw e; } return d; }

  function setTube(id, v) {
    const el = $(id); const val = num(v);
    const h = val === null ? 0 : clamp(val / SCALE, 0, 1) * 100;
    if (reduce) { el.style.height = h + "%"; return; }
    requestAnimationFrame(() => requestAnimationFrame(() => { el.style.height = h + "%"; }));
  }

  function setWater(p) {
    const lvl = p === null ? 0 : 0.05 + 0.87 * Math.sqrt(clamp(p, 0, 1));
    const surface = 192 - lvl * 184;
    $("water").style.transform = "translateY(" + (surface - 14) + "px)";
  }

  function showPct(n) {
    const el = $("pctNum"), sign = $("pctSign");
    if (lastTick) cancelAnimationFrame(lastTick);
    if (n === null) { el.textContent = "–"; sign.style.visibility = "hidden"; shownPct = 0; return; }
    sign.style.visibility = "visible";
    if (n === 0) { el.textContent = "<1"; shownPct = 0; return; }
    if (reduce) { el.textContent = String(n); shownPct = n; return; }
    const from = shownPct, t0 = performance.now(), dur = 1100;
    const step = now => {
      const k = clamp((now - t0) / dur, 0, 1), e = 1 - Math.pow(1 - k, 3);
      el.textContent = String(Math.round(from + (n - from) * e));
      if (k < 1) lastTick = requestAnimationFrame(step); else shownPct = n;
    };
    lastTick = requestAnimationFrame(step);
  }

  const TIPS = {
    dry: ["Nothing special needed today.", "Rain can still surprise you. Check back if the sky darkens."],
    Low: ["Light rain is possible. Keep an umbrella handy.", "Heavy rain looks unlikely, but check back later in the day."],
    Watch: ["Carry rain gear and allow extra travel time.", "Avoid known waterlogging spots and low underpasses.", "Keep phones charged and check updates before heading out."],
    High: ["Avoid underpasses, low-lying roads and riverside paths.", "Delay non-essential travel, especially during peak rain.", "Keep phones charged and follow IMD and local-authority updates."],
    none: ["No estimate is available right now.", "Check official IMD updates before travelling."]
  };

  function applyHero(h) {
    document.body.dataset.band = h.tint || "none";
    $("headline").textContent = h.headline;
    $("subline").textContent = h.sub;
    $("bandText").textContent = h.chip;
    $("orb").setAttribute("aria-label", h.aria);
    showPct(h.pct);
    setWater(h.p);
    const tips = TIPS[h.tips] || TIPS.none;
    $("tips").replaceChildren(...tips.map(t => { const li = document.createElement("li"); li.textContent = t; return li; }));
  }

  function liveHero() {
    const f = state.fc, ml = state.ml;
    const input = f && f.inputs || {};
    const reasons = (f && f.advisory_preview && f.advisory_preview.trigger_reason_codes) || [];
    const watch = reasons.includes("LOCAL_RAINFALL_WATCH") || reasons.includes("UPSTREAM_CATCHMENT_RAIN_WATCH");
    const city24 = num(input.average_forecast_24h_mm);
    const has = ml && ml.available;
    const hc = has ? ml.heavy_rain_chance : null;
    const band = hc ? hc.band : null;
    const pct = hc ? hc.percent : null;
    const label = hc ? hc.percent_label : "no estimate";
    let headline, sub, tint, chip, tips;
    if (watch) {
      headline = "Rain watch in effect"; sub = "Forecast rain crosses our experimental watch level in parts of Pune or its dam catchments. This is not an official warning.";
      tint = band === "High" ? "High" : "Watch"; chip = "Rain watch"; tips = band === "High" ? "High" : "Watch";
    } else if (band === "High") {
      headline = "Heavy rain is likely"; sub = "Plan travel carefully and avoid low-lying roads today."; tint = "High"; chip = "High chance"; tips = "High";
    } else if (band === "Watch") {
      headline = "Rain is on the way"; sub = "A good day to carry rain gear and allow extra travel time."; tint = "Watch"; chip = "Moderate chance"; tips = "Watch";
    } else if (band === "Low") {
      const dry = city24 !== null && city24 < 1;
      headline = dry ? "A dry day ahead" : "Light rain possible"; sub = "Heavy rain looks unlikely across Pune in the next 24 hours."; tint = "Low"; chip = "Low chance"; tips = dry ? "dry" : "Low";
    } else {
      headline = "Outlook unavailable"; sub = "No estimate right now. Please check official IMD updates."; tint = "none"; chip = "No estimate"; tips = "none";
    }
    const ob = OBS.stations.reduce((a, s) => !a || s.mm > a.mm ? s : a, null);
    if (ob && ob.mm >= 15 && (tint === "Low" || tint === "none")) {
      const heavy = ob.mm >= 30;
      headline = heavy ? "Heavy rain has fallen" : "Rain has fallen";
      sub = ob.station + " recorded " + ob.mm.toFixed(1) + " mm in the last 24 hours. " + (heavy ? "Expect waterlogging and slow traffic." : "Roads may be wet in places.");
      chip = heavy ? "Heavy rain recorded" : "Rain recorded";
      tint = ob.mm >= 60 ? "High" : heavy ? "Watch" : "Low";
      tips = tint === "none" ? "Low" : tint;
    }
    const rr = REP.rainRecent;
    if (rr && rr.people >= 3 && rr.wards >= 2 && (tint === "Low" || tint === "none") && headline !== "Heavy rain has fallen" && headline !== "Rain has fallen") {
      headline = "Residents report heavy rain";
      sub = rr.people + " residents in " + rr.wards + " wards report steady or heavy rain in the last 90 minutes. Unverified, and not in the forecast.";
      chip = "Resident reports"; tint = "Watch"; tips = "Watch";
    }
    return {headline, sub, tint, chip, tips, pct, p: hc ? hc.probability : null, aria: "Chance of heavy rain in the next 24 hours: " + label};
  }

  function replayHero(d) {
    const hc = d.heavy_rain_chance;
    const map = {High: ["Heavy rain was likely", "High chance"], Watch: ["Rain was likely", "Moderate chance"], Low: ["Heavy rain looked unlikely", "Low chance"]};
    const m = map[hc.band];
    return {
      headline: m[0], sub: "What actually fell: regional average " + d.actual.regional_mean_mm + " mm, wettest area " + d.actual.highest_cell_mm + " mm.",
      tint: hc.band, chip: m[1], tips: hc.band, pct: hc.percent, p: hc.probability, aria: "Replay. Chance of heavy rain: " + hc.percent_label
    };
  }

  function renderGauges() {
    const r = state.replay;
    if (r) {
      $("val6").textContent = "—"; setTube("tube6", null);
      $("val24").innerHTML = mmHtml(r.inputs.local_24h_mm); setTube("tube24", r.inputs.local_24h_mm);
      $("lab24").textContent = "forecast that day";
    } else {
      const input = state.fc && state.fc.inputs || {};
      $("val6").innerHTML = mmHtml(input.average_forecast_6h_mm); setTube("tube6", input.average_forecast_6h_mm);
      $("val24").innerHTML = mmHtml(input.average_forecast_24h_mm); setTube("tube24", input.average_forecast_24h_mm);
      $("lab24").textContent = "next 24 hours";
    }
  }

  function renderWard() {
    const w = state.wards.find(x => String(x.wardnum) === $("ward").value);
    $("wval6").innerHTML = w ? mmHtml(w.forecast_6h_mm) : "—"; setTube("wtube6", w ? w.forecast_6h_mm : null);
    $("wval24").innerHTML = w ? mmHtml(w.forecast_24h_mm) : "—"; setTube("wtube24", w ? w.forecast_24h_mm : null);
    $("wardNote").textContent = w ? w.name + " · Ward " + w.wardnum + ". Ward-centre estimate, not an exact street reading." : "Ward-centre estimate, not an exact street reading.";
  }

  function renderTech() {
    const rows = [];
    const r = state.replay;
    const src = r || state.ml;
    const input = state.fc && state.fc.inputs || {};
    if (!r) { rows.push(["City average, next 6 h", mm(input.average_forecast_6h_mm) + " mm"]); rows.push(["City average, next 24 h", mm(input.average_forecast_24h_mm) + " mm"]); }
    if (src && src.available) {
      if (r) rows.push(["City average forecast that day", mm(src.inputs.local_24h_mm) + " mm"]);
      rows.push(["Highest dam-side point, 24 h", mm(src.inputs.upstream_max_24h_mm) + " mm"]);
      rows.push(["Chance: regional average 20 mm or more", src.heavy_rain_chance.percent_label]);
      if (src.upstream_heavy_rain_chance) rows.push(["Chance: any dam-side area 64.5 mm or more", src.upstream_heavy_rain_chance.percent_label]);
      const m = src.model || {};
      if (m.trained_days) rows.push(["Model", "Logistic regression, " + m.trained_days + " days"]);
      if (r) rows.push(["Observed that day", r.actual.regional_mean_mm + " mm avg · " + r.actual.highest_cell_mm + " mm max"]);
    }
    rows.push(["Rain watch thresholds (experimental)", "64.5 mm / 24 h · 28.9 mm / 6 h"]);
    const t = timeOnly(input.fetched_at_local);
    if (!r && t) rows.push(["Forecast fetched", t]);
    $("techList").replaceChildren(...rows.flatMap(([k, v]) => { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = k; dd.textContent = v; return [dt, dd]; }));
    $("techNote").textContent = (src && src.note ? src.note + " " : "") + "The model estimates heavy rain, not flooding, and cannot judge extreme rainfall. Thresholds are provisional.";
  }

  function render() {
    const r = state.replay;
    $("banner").hidden = !r;
    $("areaCard").classList.toggle("dim", !!r);
    if (r) $("bannerText").textContent = "Replay · " + r.label;
    applyHero(r ? replayHero(r) : liveHero());
    const t = timeOnly(state.fc && state.fc.inputs && state.fc.inputs.fetched_at_local);
    $("metaChip").textContent = r ? "Past day · nothing sent" : (t ? "Updated " + t : "");
    renderGauges(); renderTech(); renderReportBar(); drawReports(); renderObs();
  }

  function renderOfficial() {
    const wd = state.imd && state.imd.warning, nc = state.imd && state.imd.nowcast;
    let title = "Check IMD directly for current warnings.", tone = "", bits = [];
    const records = wd && Array.isArray(wd.records) ? wd.records : [];
    if (wd && wd.available && records.length) {
      const r = records[0];
      if (r.issue_age_assessment !== "issued_within_24h" || !r.issued_at_local) { title = "The latest IMD bulletin is old or unverified."; tone = "watch"; bits.push("Check IMD directly."); }
      else {
        const labels = (r.days || []).filter(d => d.warning_present).flatMap(d => (d.warnings || []).filter(w => w.code !== 1).map(w => "Day " + d.day_number + ": " + w.label));
        title = labels.length ? "IMD district bulletin: " + labels.join(" · ") : "Latest IMD district bulletin lists no coded warnings.";
        tone = labels.length ? "watch" : "good"; bits.push("Issued " + dateTime(r.issued_at_local) + " · district level.");
      }
    } else if (wd && wd.status !== "authentication_required") { title = "Official warning data is temporarily unavailable."; }
    const recs = nc && Array.isArray(nc.records) ? nc.records : [];
    if (nc && nc.available && recs.length) {
      const fresh = recs.filter(r => r.freshness === "recent_issue_time" && !r.expired_by_validity_time && r.issue_time_local);
      if (fresh.length) {
        const labels = fresh.flatMap(r => (r.categories || []).filter(c => c.category_number !== 1).map(c => c.label));
        bits.push("District nowcast: " + (labels.length ? [...new Set(labels)].join(" · ") : "no coded warning") + " (issued " + dateTime(fresh[0].issue_time_local) + ").");
      } else bits.push("District nowcast is old or unverified.");
    }
    $("imdDot").className = "dot " + tone;
    $("imdTitle").textContent = title;
    $("imdText").textContent = bits.length ? bits.join("\n") : "Official information can change. Confirm directly with IMD.";
  }

  function fillWards() {
    const sel = $("ward"), old = sel.value;
    sel.replaceChildren();
    const ph = document.createElement("option"); ph.value = ""; ph.textContent = "Choose a Pune ward"; sel.append(ph);
    state.wards.slice().sort((a, b) => String(a.name).localeCompare(String(b.name))).forEach(w => { const o = document.createElement("option"); o.value = String(w.wardnum); o.textContent = w.name; sel.append(o); });
    if (state.wards.some(w => String(w.wardnum) === old)) sel.value = old;
    renderWard();
  }

  async function refresh() {
    const btn = $("refreshBtn"); btn.disabled = true; btn.textContent = "Refreshing…";
    try {
      const res = await Promise.allSettled([getJson("/prediction/forecast"), getJson("/ml/outlook"), getJson("/nowcast/imd/district-warning"), getJson("/nowcast/imd")]);
      state.fc = res[0].status === "fulfilled" ? res[0].value : null;
      state.ml = res[1].status === "fulfilled" ? res[1].value : null;
      state.imd = {warning: res[2].status === "fulfilled" ? res[2].value : {available: false, status: "source_error"}, nowcast: res[3].status === "fulfilled" ? res[3].value : {available: false, status: "source_error"}};
      state.wards = state.fc && Array.isArray(state.fc.wards) ? state.fc.wards : [];
      fillWards(); renderOfficial();
      if (!state.replay) loadMapScores();
      loadReports();
      loadObservations();
      if (!state.replay) render();
    } finally { btn.disabled = false; btn.textContent = "↻ Refresh"; }
  }

  async function loadEvents() {
    const sel = $("replaySel");
    try {
      const d = await getJson("/ml/replay/events");
      sel.replaceChildren();
      d.events.forEach(ev => { const o = document.createElement("option"); o.value = ev.date; o.textContent = ev.label; sel.append(o); });
      $("replayGo").disabled = !d.events.length;
    } catch (e) {
      sel.replaceChildren(new Option("Replay data not found", "")); $("replayGo").disabled = true;
    }
  }

  async function startReplay() {
    const v = $("replaySel").value; if (!v) return;
    const btn = $("replayGo"); btn.disabled = true;
    try {
      state.replay = await getJson("/ml/replay?date=" + encodeURIComponent(v));
      render(); loadMapScores(v); window.scrollTo({top: 0, behavior: reduce ? "auto" : "smooth"});
    } catch (e) { $("subline").textContent = "Replay unavailable: " + (e.message || "error"); }
    finally { btn.disabled = false; }
  }

  function backToLive() { state.replay = null; render(); loadMapScores(); }

  function setupReveal() {
    const els = [...document.querySelectorAll(".reveal")];
    if (reduce || !("IntersectionObserver" in window)) { els.forEach(e => e.classList.add("in")); return; }
    els.forEach((e, i) => { e.style.transitionDelay = Math.min(i, 5) * 70 + "ms"; });
    const io = new IntersectionObserver(entries => entries.forEach(en => { if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); } }), {threshold: 0.08});
    els.forEach(e => io.observe(e));
  }

  const OBS = {stations: [], source: ""};
  const REP = {wards: {}, total: 0, bot: null, rain: {}, rainTotal: 0, rainRecent: {people: 0, wards: 0}};
  const RAIN_WORDS = {1: "light rain", 2: "steady rain", 3: "heavy downpour"};
  const REP_COLORS = {1: "#f2c14e", 2: "#f08a3c", 3: "#e5473d"};
  const MAP = {shape: null, scores: null, replay: false, mode: "now", sel: null, k: 1, tx: 0, ty: 0, W: 1000, H: 860, proj: null, paths: new Map(), bbox: new Map(), d: new Map(), promise: null, moved: false, me: null, note: ""};
  const NS = "http://www.w3.org/2000/svg";
  const RAMP = [[0, [24, 54, 66]], [0.15, [31, 93, 105]], [0.35, [224, 178, 82]], [0.6, [240, 138, 60]], [1, [229, 71, 61]]];
  function ramp(t) {
    t = clamp(t, 0, 1);
    for (let i = 1; i < RAMP.length; i++) {
      if (t <= RAMP[i][0]) { const a = RAMP[i - 1], b = RAMP[i], u = (t - a[0]) / (b[0] - a[0]); return "rgb(" + a[1].map((c, j) => Math.round(c + (b[1][j] - c) * u)).join(",") + ")"; }
    }
    return "rgb(" + RAMP[RAMP.length - 1][1].join(",") + ")";
  }
  const polysOf = g => g.type === "Polygon" ? [g.coordinates] : g.coordinates;
  const levelWord = (l, s) => l ? String(l).charAt(0).toUpperCase() + String(l).slice(1).toLowerCase() : (s === null || s === undefined ? "Unknown" : s < 0.2 ? "Low" : s < 0.4 ? "Guarded" : s < 0.6 ? "Elevated" : s < 0.8 ? "High" : "Severe");
  const topWord = v => v === null || v === undefined ? "unknown" : "top " + Math.max(1, Math.round((1 - v) * 100)) + "%";
  const vulnWord = v => v === null || v === undefined ? "Unknown" : v < 0.34 ? "Low" : v < 0.67 ? "Moderate" : "High";

  function buildProjection(wards) {
    let minx = 1e9, miny = 1e9, maxx = -1e9, maxy = -1e9;
    wards.forEach(w => polysOf(w.g).forEach(poly => poly.forEach(ring => ring.forEach(p => { minx = Math.min(minx, p[0]); maxx = Math.max(maxx, p[0]); miny = Math.min(miny, p[1]); maxy = Math.max(maxy, p[1]); }))));
    const cLon = (minx + maxx) / 2, cLat = (miny + maxy) / 2, kx = Math.cos(cLat * Math.PI / 180), pad = 34;
    const sc = Math.min((MAP.W - 2 * pad) / ((maxx - minx) * kx), (MAP.H - 2 * pad) / (maxy - miny));
    return (lon, lat) => [MAP.W / 2 + (lon - cLon) * kx * sc, MAP.H / 2 - (lat - cLat) * sc];
  }

  function ringPath(ring) { return "M" + ring.map(p => { const q = MAP.proj(p[0], p[1]); return q[0].toFixed(1) + " " + q[1].toFixed(1); }).join("L") + "Z"; }

  function buildMap() {
    const svg = $("mapSvg");
    svg.replaceChildren();
    const vp = document.createElementNS(NS, "g"); vp.id = "vp";
    const rg = document.createElementNS(NS, "g"); rg.id = "riversG";
    const wg = document.createElementNS(NS, "g"); wg.id = "wardsG";
    const hs = document.createElementNS(NS, "path"); hs.id = "hlSel";
    const hh = document.createElementNS(NS, "path"); hh.id = "hlHover";
    const lg = document.createElementNS(NS, "g"); lg.id = "labelsG";
    const mg = document.createElementNS(NS, "g"); mg.id = "meG";
    const rp = document.createElementNS(NS, "g"); rp.id = "repG";
    vp.append(rg, wg, hs, hh, lg, rp, mg); svg.append(vp);
    const wards = MAP.shape.wards;
    MAP.proj = buildProjection(wards);
    (MAP.shape.rivers || []).forEach(line => {
      const p = document.createElementNS(NS, "path");
      p.setAttribute("d", "M" + line.map(pt => { const q = MAP.proj(pt[0], pt[1]); return q[0].toFixed(1) + " " + q[1].toFixed(1); }).join("L"));
      rg.append(p);
    });
    MAP.paths.clear(); MAP.bbox.clear(); MAP.d.clear();
    wards.forEach((w, i) => {
      const polys = polysOf(w.g);
      const d = polys.map(poly => poly.map(ringPath).join("")).join("");
      MAP.d.set(String(w.id), d);
      const p = document.createElementNS(NS, "path");
      p.setAttribute("d", d); p.setAttribute("class", "w"); p.dataset.id = String(w.id);
      p.style.animationDelay = Math.min(i * 14, 700) + "ms";
      wg.append(p); MAP.paths.set(String(w.id), p);
      let big = polys[0], bigN = 0;
      polys.forEach(poly => { if (poly[0].length > bigN) { bigN = poly[0].length; big = poly; } });
      let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
      big[0].forEach(pt => { const q = MAP.proj(pt[0], pt[1]); x0 = Math.min(x0, q[0]); x1 = Math.max(x1, q[0]); y0 = Math.min(y0, q[1]); y1 = Math.max(y1, q[1]); });
      MAP.bbox.set(String(w.id), [x0, y0, x1, y1]);
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", ((x0 + x1) / 2).toFixed(1)); t.setAttribute("y", ((y0 + y1) / 2).toFixed(1)); t.dataset.id = String(w.id);
      lg.append(t);
    });
    const dot = document.createElementNS(NS, "circle"); dot.setAttribute("class", "mePulse"); dot.setAttribute("r", "6");
    const dot2 = document.createElementNS(NS, "circle"); dot2.setAttribute("class", "meDot"); dot2.setAttribute("r", "5");
    mg.append(dot, dot2); mg.style.display = "none";
    applyView(false);
  }

  function applyView(animate) {
    const vp = $("vp"); if (!vp) return;
    vp.style.transition = animate && !reduce ? "transform .7s cubic-bezier(.22,1,.36,1)" : "none";
    vp.style.transform = "translate(" + MAP.tx + "px," + MAP.ty + "px) scale(" + MAP.k + ")";
    $("labelsG").style.opacity = MAP.k >= 2.2 ? 1 : 0;
    const sx = MAP.W / Math.max($("mapSvg").getBoundingClientRect().width, 200), fs = 11.5 * sx / MAP.k;
    document.querySelectorAll("#labelsG text").forEach(t => { t.setAttribute("font-size", fs.toFixed(2)); t.setAttribute("stroke-width", (3 * sx / MAP.k).toFixed(2)); });
    document.querySelectorAll("#repG circle").forEach(c => c.setAttribute("r", String(Number(c.dataset.r) / MAP.k)));
    document.querySelectorAll("#meG circle").forEach(c => c.setAttribute("r", String((c.classList.contains("mePulse") ? 6 : 5) / MAP.k)));
    const touch = MAP.k > 1.02 ? "none" : "pan-y";
    $("mapSvg").style.touchAction = touch; $("mapBox").style.touchAction = touch;
  }

  function clampPan() { MAP.tx = clamp(MAP.tx, MAP.W - MAP.W * MAP.k, 0); MAP.ty = clamp(MAP.ty, MAP.H - MAP.H * MAP.k, 0); }
  function zoomAt(cx, cy, f, animate) {
    const nk = clamp(MAP.k * f, 1, 8), r = nk / MAP.k;
    MAP.tx = cx - (cx - MAP.tx) * r; MAP.ty = cy - (cy - MAP.ty) * r; MAP.k = nk; clampPan(); applyView(animate);
  }
  function fitView() { MAP.k = 1; MAP.tx = 0; MAP.ty = 0; applyView(true); }
  function toSvg(e) { const r = $("mapSvg").getBoundingClientRect(); return [(e.clientX - r.left) * MAP.W / r.width, (e.clientY - r.top) * MAP.H / r.height]; }

  function zoomToWard(id) {
    const b = MAP.bbox.get(String(id)); if (!b) return;
    const bw = Math.max(b[2] - b[0], 20), bh = Math.max(b[3] - b[1], 20);
    const k = clamp(0.5 * Math.min(MAP.W / bw, MAP.H / bh), 1.8, 6);
    MAP.k = k; MAP.tx = MAP.W / 2 - ((b[0] + b[2]) / 2) * k; MAP.ty = MAP.H / 2 - ((b[1] + b[3]) / 2) * k; clampPan(); applyView(true);
  }

  function recolor() {
    if (!MAP.shape) return;
    const base = MAP.mode === "base", learned = MAP.mode === "learned", sc = MAP.scores || {};
    let mn = 1e9, mx = -1e9;
    if (base) Object.values(sc).forEach(r => { if (r.v !== null && r.v !== undefined) { mn = Math.min(mn, r.v); mx = Math.max(mx, r.v); } });
    MAP.paths.forEach((p, id) => {
      const r = sc[id]; const raw = r ? (learned ? r.li : base ? r.v : r.s) : null;
      if (raw === null || raw === undefined) { p.style.fill = "#22313b"; return; }
      p.style.fill = ramp(base ? (raw - mn) / ((mx - mn) || 1) : raw);
    });
    $("legCap").textContent = learned ? "Report-proneness, learned (relative)" : base ? "Built-in vulnerability (relative)" : "Risk right now";
    document.querySelectorAll("#labelsG text").forEach(t => { const r = sc[t.dataset.id]; const n = r ? r.n : ""; t.textContent = n.length > 18 ? n.slice(0, 17) + "…" : n; });
  }

  function barRow(label, value, valueText, shade) {
    const wrap = document.createDocumentFragment();
    const a = document.createElement("span"); a.textContent = label;
    const b = document.createElement("strong"); b.textContent = valueText;
    const bar = document.createElement("div"); bar.className = "bar";
    const i = document.createElement("i"); bar.append(i);
    wrap.append(a, b, bar);
    requestAnimationFrame(() => requestAnimationFrame(() => { i.style.width = (value === null || value === undefined ? 0 : clamp(value, 0, 1) * 100) + "%"; i.style.background = ramp(shade === undefined ? value : shade); }));
    return wrap;
  }

  function renderPanel() {
    const panel = $("wardPanel"), id = MAP.sel, r = id && MAP.scores ? MAP.scores[id] : null;
    panel.replaceChildren();
    if (!r) {
      const p = document.createElement("p"); p.className = "hint"; p.textContent = MAP.shape ? "Hover or tap a ward to see its risk." : "Ward details will appear here.";
      panel.append(p); return;
    }
    const box = document.createElement("div"); box.className = "swapIn";
    const head = document.createElement("div"); head.className = "ph";
    const h = document.createElement("h3"); h.textContent = r.n;
    const chip = document.createElement("span"); chip.className = "chip"; chip.textContent = (MAP.replay ? "Then: " : "Now: ") + levelWord(r.l, r.s);
    head.append(h, chip); box.append(head);
    const word = levelWord(r.l, r.s), vw = vulnWord(r.v);
    const sum = document.createElement("p"); sum.className = "sum";
    sum.textContent = (r.s !== null && r.s < 0.2)
      ? (MAP.replay ? "Risk was low on this day. " : "Risk is low right now. ") + "This ward's built-in vulnerability is " + vw.toLowerCase() + " (terrain, drainage and river distance), so it matters more when heavy rain is forecast."
      : "Risk is " + word.toLowerCase() + " because forecast rain meets terrain and river conditions that make this ward more vulnerable (built-in vulnerability: " + vw.toLowerCase() + ").";
    box.append(sum);
    const rows = document.createElement("div"); rows.className = "prow";
    rows.append(barRow("Risk index", r.s, r.s === null ? "—" : r.s.toFixed(2), r.s));
    rows.append(barRow("Waterlogging-prone terrain", r.u, r.u === null ? "—" : r.u.toFixed(2), r.u));
    rows.append(barRow("Near a river corridor", r.r, r.r === null ? "—" : r.r.toFixed(2), r.r));
    if (r.p !== null && r.p !== undefined) rows.append(barRow("Chance of a reported local flood today", clamp(r.p / 0.5, 0, 1), r.p < 0.01 ? "under 1%" : Math.round(r.p * 100) + "%", clamp(r.p / 0.5, 0, 1)));
    if (r.li !== null && r.li !== undefined) rows.append(barRow("Report-proneness since 2022 (learned)", r.li, topWord(r.li), r.li));
    box.append(rows);
    const rpt = REP.wards[id];
    const f = document.createElement("div"); f.className = "prow";
    const l1 = document.createElement("span"); l1.textContent = "Rain forecast, next 24 h"; const v1 = document.createElement("strong"); v1.textContent = r.f24 === null ? "—" : r.f24.toFixed(1) + " mm";
    const l2 = document.createElement("span"); l2.textContent = "Dam-release corridor"; const v2 = document.createElement("strong"); v2.textContent = r.c ? "Flagged by release scenario" : "Not active (no live release data)";
    f.append(l1, v1, l2, v2);
    if (rpt && !state.replay) {
      const l3 = document.createElement("span"); l3.textContent = "Citizen reports, last 6 h";
      const v3 = document.createElement("strong"); const ago = rpt.minutes_ago < 1 ? "just now" : rpt.minutes_ago < 60 ? rpt.minutes_ago + " min ago" : Math.floor(rpt.minutes_ago / 60) + " h ago";
      v3.textContent = rpt.count + (rpt.count === 1 ? " report" : " reports") + " · " + rpt.people + (rpt.people === 1 ? " person" : " people") + " · latest " + ago;
      f.append(l3, v3);
    }
    const rn = REP.rain[id];
    if (rn && !state.replay) {
      const l4 = document.createElement("span"); l4.textContent = "Residents report rain, last 3 h";
      const v4 = document.createElement("strong");
      v4.textContent = rn.count + (rn.count === 1 ? " report" : " reports") + " · up to " + RAIN_WORDS[rn.max_intensity] + " · latest " + agoText(rn.minutes_ago);
      f.append(l4, v4);
    }
    box.append(f);
    panel.append(box);
  }

  function updateSel() {
    const hs = $("hlSel"); if (!hs) return;
    hs.setAttribute("d", MAP.sel && MAP.d.has(MAP.sel) ? MAP.d.get(MAP.sel) : "");
  }

  function selectWard(id, opts) {
    opts = opts || {};
    MAP.sel = id === null || id === undefined || id === "" ? null : String(id);
    updateSel(); renderPanel();
    const sel = $("ward");
    if (MAP.sel !== null && sel.value !== MAP.sel && [...sel.options].some(o => o.value === MAP.sel)) { sel.value = MAP.sel; renderWard(); }
    if (opts.zoom && MAP.sel !== null) zoomToWard(MAP.sel); else if (opts.zoom) fitView();
  }
  function mapSelectFromSelect() { selectWard($("ward").value || null, {zoom: true}); }

  function showTip(e, id) {
    const tip = $("mapTip"), r = MAP.scores && MAP.scores[id];
    if (!r) { tip.hidden = true; return; }
    tip.replaceChildren();
    const b = document.createElement("b"); b.textContent = r.n;
    const t = document.createElement("span");
    t.textContent = MAP.mode === "learned" ? "Report-proneness: " + topWord(r.li) : MAP.mode === "base" ? "Built-in vulnerability: " + vulnWord(r.v) : (MAP.replay ? "Risk then: " : "Risk now: ") + levelWord(r.l, r.s) + (r.f24 !== null ? " · 24 h rain " + r.f24.toFixed(1) + " mm" : "");
    tip.append(b, t); tip.hidden = false;
    const box = $("mapBox").getBoundingClientRect();
    let x = e.clientX - box.left + 14, y = e.clientY - box.top + 14;
    if (x + 200 > box.width) x = e.clientX - box.left - 210;
    if (y + 70 > box.height) y = e.clientY - box.top - 70;
    tip.style.left = Math.max(6, x) + "px"; tip.style.top = Math.max(6, y) + "px";
  }

  function pointInRing(x, y, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
      if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }
  function wardAt(lon, lat) {
    for (const w of MAP.shape.wards) for (const poly of polysOf(w.g)) { if (pointInRing(lon, lat, poly[0]) && !poly.slice(1).some(h => pointInRing(lon, lat, h))) return String(w.id); }
    return null;
  }

  function locateMe() {
    const panel = $("wardPanel");
    const say = text => { panel.replaceChildren(); const p = document.createElement("p"); p.className = "hint"; p.textContent = text; panel.append(p); };
    if (!MAP.shape) return say("The map is still loading.");
    if (!navigator.geolocation) return say("Your browser can't share location. Choose your ward from the list instead.");
    say("Finding your location…");
    navigator.geolocation.getCurrentPosition(pos => {
      const lon = pos.coords.longitude, lat = pos.coords.latitude, id = wardAt(lon, lat);
      const q = MAP.proj(lon, lat), mg = $("meG");
      mg.style.display = ""; mg.querySelectorAll("circle").forEach(c => { c.setAttribute("cx", q[0].toFixed(1)); c.setAttribute("cy", q[1].toFixed(1)); });
      if (id) selectWard(id, {zoom: true}); else { selectWard(null); say("You appear to be outside Pune's 58 wards. You can still tap any ward on the map."); }
    }, () => say("Location permission was not granted. Tap your ward on the map or choose it from the list."), {enableHighAccuracy: false, timeout: 10000, maximumAge: 300000});
  }

  function ensureShape() {
    if (MAP.shape) return Promise.resolve(true);
    if (!MAP.promise) {
      MAP.promise = getJson("/ml/map/shape").then(d => { MAP.shape = d; buildMap(); recolor(); updateSel(); $("mapMsg").hidden = true; return true; })
        .catch(e => { MAP.promise = null; $("mapMsg").hidden = false; $("mapMsg").textContent = "Map unavailable: " + (e.message || "could not load ward shapes"); return false; });
    }
    return MAP.promise;
  }

  async function loadMapScores(date) {
    const ok = await ensureShape(); if (!ok) return;
    try {
      const d = await getJson("/ml/map/scores" + (date ? "?date=" + encodeURIComponent(date) : ""));
      MAP.scores = d.scores; MAP.replay = !!d.replay; MAP.note = d.note || "";
      const tag = $("mapTag"); tag.hidden = !MAP.replay; if (MAP.replay && state.replay) tag.textContent = "Replay · " + (state.replay.date ? new Date(state.replay.date + "T00:00:00").toLocaleDateString("en-IN", {day: "numeric", month: "short", year: "numeric"}) : "past day");
      setMapNote(); recolor(); renderPanel();
    } catch (e) {
      if (!MAP.scores) { $("mapMsg").hidden = false; $("mapMsg").textContent = "Risk colours unavailable: " + (e.message || "error"); }
    }
  }

  function drawReports() {
    const g = $("repG"); if (!g) return;
    g.replaceChildren();
    if (state.replay) return;
    Object.keys(REP.wards).forEach(id => {
      const b = MAP.bbox.get(id); if (!b) return;
      const w = REP.wards[id], cx = ((b[0] + b[2]) / 2).toFixed(1), cy = ((b[1] + b[3]) / 2).toFixed(1), col = REP_COLORS[w.max_severity] || REP_COLORS[1], r = 15 + Math.min(w.count, 5) * 2;
      if (w.minutes_ago <= 30) { const ring = document.createElementNS(NS, "circle"); ring.setAttribute("class", "ring"); ring.setAttribute("cx", cx); ring.setAttribute("cy", cy); ring.setAttribute("stroke", col); ring.dataset.r = String(r); g.append(ring); }
      const pin = document.createElementNS(NS, "circle"); pin.setAttribute("class", "pin"); pin.setAttribute("cx", cx); pin.setAttribute("cy", cy); pin.setAttribute("fill", col); pin.setAttribute("fill-opacity", w.people > 1 ? "1" : "0.78"); pin.dataset.r = String(r); g.append(pin);
    });
    Object.keys(REP.rain).forEach(id => {
      const b = MAP.bbox.get(id); if (!b) return;
      const w = REP.rain[id], cx = (b[0] + (b[2] - b[0]) * 0.3).toFixed(1), cy = (b[1] + (b[3] - b[1]) * 0.3).toFixed(1), r = 9 + w.max_intensity * 3;
      if (w.minutes_ago <= 20) { const ring = document.createElementNS(NS, "circle"); ring.setAttribute("class", "ring"); ring.setAttribute("cx", cx); ring.setAttribute("cy", cy); ring.setAttribute("stroke", "#4aa3ff"); ring.dataset.r = String(r); g.append(ring); }
      const pin = document.createElementNS(NS, "circle"); pin.setAttribute("class", "pin"); pin.setAttribute("cx", cx); pin.setAttribute("cy", cy); pin.setAttribute("fill", "#4aa3ff"); pin.setAttribute("fill-opacity", w.people > 1 ? "1" : "0.78"); pin.dataset.r = String(r); g.append(pin);
    });
    applyView(false);
  }

  function renderReportBar() {
    const n = Object.keys(REP.wards).length, m = Object.keys(REP.rain).length;
    const parts = [];
    if (REP.total) parts.push(REP.total + (REP.total === 1 ? " flood report" : " flood reports") + " from " + n + (n === 1 ? " ward" : " wards") + " (6 h)");
    if (REP.rainTotal) parts.push(REP.rainTotal + (REP.rainTotal === 1 ? " rain report" : " rain reports") + " from " + m + (m === 1 ? " ward" : " wards") + " (3 h)");
    $("repText").textContent = state.replay ? "Replay mode: live citizen reports are hidden."
      : parts.length === 0 ? "No citizen reports right now."
      : parts.join(" · ") + " · unverified. Red and amber dots are floods, blue dots are rain.";
    const a = $("repLink");
    if (REP.bot && REP.bot.link) { a.href = REP.bot.link; a.hidden = false; } else a.hidden = true;
  }

  async function loadReports() {
    try {
      const d = await getJson("/ml/reports");
      REP.wards = d.wards || {}; REP.total = d.total || 0; REP.bot = d.bot || null; REP.rain = d.rain || {}; REP.rainTotal = d.rain_total || 0; REP.rainRecent = d.rain_recent || {people: 0, wards: 0};
    } catch (e) { REP.wards = {}; REP.total = 0; REP.rain = {}; REP.rainTotal = 0; REP.rainRecent = {people: 0, wards: 0}; }
    renderReportBar(); drawReports(); if (MAP.sel) renderPanel(); if (!state.replay) render();
  }

  const agoText = m => m < 1 ? "just now" : m < 60 ? m + " min ago" : Math.floor(m / 60) + " h ago";

  function renderObs() {
    const card = $("obsCard"), list = OBS.stations;
    if (!list.length || state.replay) { card.hidden = true; return; }
    card.hidden = false;
    const rows = document.createElement("div"); rows.className = "prow";
    list.forEach(s => rows.append(barRow(s.station, clamp(s.mm / 65, 0, 1), s.mm.toFixed(1) + " mm", clamp(s.mm / 65, 0, 1))));
    $("obsRows").replaceChildren(rows);
    const newest = list.reduce((m, s) => Math.min(m, s.minutes_ago), 1e9);
    $("obsNote").textContent = (OBS.source || "IMD station data") + " · updated " + agoText(newest) + ". Bars fill up to 65 mm. Daily totals are to 8:30 am.";
  }

  async function loadObservations() {
    try { const d = await getJson("/ml/observations"); OBS.stations = d.stations || []; OBS.source = d.source || ""; } catch (e) { OBS.stations = []; OBS.source = ""; }
    renderObs(); if (!state.replay) render();
  }

  function setMapNote() {
    $("mapNote").textContent = MAP.mode === "learned"
      ? "Learned from news-reported local floods since 2022 (Google Groundsource). It reflects reporting as well as flooding, and is not an official map. Ctrl + scroll or the + / − buttons zoom the map."
      : (MAP.note ? MAP.note + " " : "") + "Ctrl + scroll or the + / − buttons zoom the map.";
  }

  function setupMap() {
    const svg = $("mapSvg"), box = $("mapBox");
    document.querySelectorAll(".seg button").forEach(b => b.addEventListener("click", () => {
      MAP.mode = b.dataset.mode; document.querySelectorAll(".seg button").forEach(x => x.classList.toggle("on", x === b)); setMapNote(); recolor();
    }));
    $("zIn").addEventListener("click", () => zoomAt(MAP.W / 2, MAP.H / 2, 1.6, true));
    $("zOut").addEventListener("click", () => zoomAt(MAP.W / 2, MAP.H / 2, 1 / 1.6, true));
    $("zFit").addEventListener("click", fitView);
    $("locate").addEventListener("click", locateMe);
    svg.addEventListener("wheel", e => { if (!(e.ctrlKey || e.metaKey)) return; e.preventDefault(); const c = toSvg(e); zoomAt(c[0], c[1], e.deltaY < 0 ? 1.18 : 1 / 1.18, false); }, {passive: false});
    svg.addEventListener("dblclick", e => { const c = toSvg(e); zoomAt(c[0], c[1], 1.8, true); });
    const ptrs = new Map(); let last = null, pinch = null;
    svg.addEventListener("pointerdown", e => {
      ptrs.set(e.pointerId, [e.clientX, e.clientY]); MAP.moved = false; last = [e.clientX, e.clientY];
      if (ptrs.size === 2) { const a = [...ptrs.values()]; pinch = Math.hypot(a[0][0] - a[1][0], a[0][1] - a[1][1]); }
    });
    window.addEventListener("pointermove", e => {
      if (!ptrs.has(e.pointerId)) return;
      ptrs.set(e.pointerId, [e.clientX, e.clientY]);
      if (ptrs.size === 2 && pinch) {
        const a = [...ptrs.values()], d = Math.hypot(a[0][0] - a[1][0], a[0][1] - a[1][1]);
        const r = svg.getBoundingClientRect(), cx = ((a[0][0] + a[1][0]) / 2 - r.left) * MAP.W / r.width, cy = ((a[0][1] + a[1][1]) / 2 - r.top) * MAP.H / r.height;
        zoomAt(cx, cy, d / pinch, false); pinch = d; MAP.moved = true; return;
      }
      if (ptrs.size === 1 && last) {
        const dx = e.clientX - last[0], dy = e.clientY - last[1];
        if (Math.abs(dx) + Math.abs(dy) > 3) MAP.moved = true;
        if (MAP.moved && MAP.k > 1.02) {
          const r = svg.getBoundingClientRect();
          MAP.tx += dx * MAP.W / r.width; MAP.ty += dy * MAP.H / r.height; clampPan(); applyView(false); $("mapTip").hidden = true;
        }
        last = [e.clientX, e.clientY];
      }
    });
    const up = e => { ptrs.delete(e.pointerId); if (ptrs.size < 2) pinch = null; if (ptrs.size === 0) last = null; };
    window.addEventListener("pointerup", up); window.addEventListener("pointercancel", up);
    svg.addEventListener("pointermove", e => {
      if (e.pointerType !== "mouse" || ptrs.size) return;
      const p = e.target.closest ? e.target.closest("path.w") : null;
      const hh = $("hlHover"); if (!hh) return;
      if (p) { hh.setAttribute("d", MAP.d.get(p.dataset.id) || ""); showTip(e, p.dataset.id); } else { hh.setAttribute("d", ""); $("mapTip").hidden = true; }
    });
    svg.addEventListener("pointerleave", () => { const hh = $("hlHover"); if (hh) hh.setAttribute("d", ""); $("mapTip").hidden = true; });
    svg.addEventListener("click", e => {
      if (MAP.moved) { MAP.moved = false; return; }
      const p = e.target.closest ? e.target.closest("path.w") : null;
      if (p) selectWard(p.dataset.id, {zoom: MAP.k < 1.6}); else if (MAP.sel) selectWard(null);
    });
    ensureShape();
  }

  $("ward").addEventListener("change", () => { renderWard(); mapSelectFromSelect(); });
  $("refreshBtn").addEventListener("click", () => refresh().catch(() => {}));
  $("replayGo").addEventListener("click", startReplay);
  $("backLive").addEventListener("click", backToLive);
  setInterval(() => { if (!state.replay && !document.hidden) refresh().catch(() => {}); }, 600000);
  setInterval(() => { if (!state.replay && !document.hidden) loadReports(); }, 60000);
  setupReveal();
  setupMap();
  loadEvents();
  render();
  refresh().catch(() => { render(); });
})();
</script>
</body>
</html>
"""
