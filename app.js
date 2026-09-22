'use strict';
const $ = id => document.getElementById(id);
const NS = 'http://www.w3.org/2000/svg';
const SURFACE = '#111d2e';
const COLORS = {close: '#22a47f', fast: '#bf862c', slow: '#6a80ec', equity: '#6a80ec', grid: '#25324a', base: '#3d4d6b'};
const INITIAL_EQUITY = 10000;
const fmt = n => n == null ? '—' : Number(n).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const pct = n => n == null ? '—' : fmt(n) + '%';
const date = n => n == null ? '—' : new Date(n * 1000).toLocaleString('th-TH');
const day = n => n == null ? '—' : new Date(n * 1000).toLocaleDateString('th-TH');
let lastData = null, busy = false, initialized = false;
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
function svgEl(tag, attrs) { const e = document.createElementNS(NS, tag); Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v)); return e; }
function metric(name, value, color = '') { const e = el('div', 'metric'); e.append(el('span', '', name), el('b', color, value)); return e; }
function key(color) { const k = el('span', 'key'); k.style.background = color; return k; }
function tipRow(color, value, label) { const row = el('div', 'tiprow'); if (color) row.append(key(color)); row.append(el('b', '', value), el('span', '', label)); return row; }

// Hover layer shared by every line chart: a crosshair snaps to the nearest x, one tooltip lists every series,
// and the same readout is reachable from the keyboard (Tab to focus, arrow keys to move, Home/End to jump).
function hoverLayer(svg, count, xAt, describe) {
  const wrap = el('div', 'chartwrap'), tip = el('div', 'tip');
  const height = svg.viewBox.baseVal.height, cross = svgEl('line', {y1: 0, y2: height, stroke: '#98a9c2', 'stroke-width': 1, visibility: 'hidden'});
  tip.hidden = true; svg.append(cross); svg.setAttribute('tabindex', '0'); wrap.append(svg, tip);
  let index = -1;
  const show = i => {
    index = Math.max(0, Math.min(count - 1, i));
    const x = xAt(index), ctm = svg.getScreenCTM();
    cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.setAttribute('visibility', 'visible');
    tip.replaceChildren(...describe(index)); tip.hidden = false;
    if (!ctm) return;
    const left = new DOMPoint(x, 0).matrixTransform(ctm).x - wrap.getBoundingClientRect().left, width = wrap.clientWidth;
    tip.style.left = left > width / 2 ? '' : (left + 10) + 'px';
    tip.style.right = left > width / 2 ? (width - left + 10) + 'px' : '';
  };
  const hide = () => { index = -1; cross.setAttribute('visibility', 'hidden'); tip.hidden = true; };
  svg.addEventListener('pointermove', e => {
    const ctm = svg.getScreenCTM(); if (!ctm) return;
    const px = new DOMPoint(e.clientX, e.clientY).matrixTransform(ctm.inverse()).x;
    let best = 0; for (let i = 1; i < count; i++) if (Math.abs(xAt(i) - px) < Math.abs(xAt(best) - px)) best = i;
    show(best);
  });
  svg.addEventListener('pointerleave', hide);
  svg.addEventListener('focus', () => show(count - 1));
  svg.addEventListener('blur', hide);
  svg.addEventListener('keydown', e => {
    const step = {ArrowLeft: -1, ArrowRight: 1, Home: -count, End: count}[e.key];
    if (step === undefined) return;
    e.preventDefault(); show((index < 0 ? count - 1 : index) + step);
  });
  return wrap;
}
function gridlines(svg, width, top, bottom, rows = 4) {
  for (let i = 0; i < rows; i++) { const y = top + i * (bottom - top) / (rows - 1); svg.append(svgEl('line', {x1: 0, x2: width, y1: y, y2: y, stroke: COLORS.grid})); }
}
function priceChart(a) {
  const W = 500, H = 160, bars = a.bars, seconds = a.bar_close - bars[bars.length - 1].time;
  const series = [['close', 'ราคาปิด', bars.map(b => b.close), 2], ['fast', 'EMA20', a.fast, 1.5], ['slow', 'EMA50', a.slow, 1.5]];
  const all = series.flatMap(s => s[2]), min = Math.min(...all), range = (Math.max(...all) - min) || 1;
  const x = i => i / (bars.length - 1) * W, y = v => H - 10 - (v - min) / range * (H - 20);
  const svg = svgEl('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': `ราคาปิด EMA20 และ EMA50 ${bars.length} แท่งล่าสุด ปิดล่าสุด ${fmt(a.price)}`});
  gridlines(svg, W, 10, H - 15);
  series.forEach(([name, , values, width]) => svg.append(svgEl('polyline', {points: values.map((v, i) => `${x(i)},${y(v)}`).join(' '), fill: 'none', stroke: COLORS[name], 'stroke-width': width, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'})));
  return hoverLayer(svg, bars.length, x, i => [el('div', 'tipdate', 'ปิดแท่ง ' + date(bars[i].time + seconds)), ...series.map(([name, label, values]) => tipRow(COLORS[name], fmt(values[i]), label))]);
}
// Simulated portfolio value (liquidation equity at each bar close) for one test period. Single series: no legend needed.
function equityChart(report, quote, title) {
  const curve = report.equity_curve || [], box = el('div', 'equity');
  box.append(el('h3', '', title));
  if (curve.length < 2) { box.append(el('p', 'footnote', 'ยังไม่มีข้อมูลพอร์ตจำลองในช่วงนี้')); return box; }
  const W = 500, H = 150, R = 62, T = 10, B = 12, values = curve.map(p => p.equity), last = values[values.length - 1];
  const lo = Math.min(INITIAL_EQUITY, ...values), hi = Math.max(INITIAL_EQUITY, ...values), pad = ((hi - lo) || INITIAL_EQUITY * .01) * .1;
  const x = i => i / (curve.length - 1) * (W - R), y = v => T + (hi + pad - v) / (hi - lo + 2 * pad) * (H - T - B);
  const svg = svgEl('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': `${title}: เริ่ม ${fmt(INITIAL_EQUITY)} สิ้นสุด ${fmt(last)} ${quote}`});
  gridlines(svg, W - R, T, H - B);
  const base = y(INITIAL_EQUITY), points = values.map((v, i) => `${x(i)},${y(v)}`);
  svg.append(svgEl('line', {x1: 0, x2: W - R, y1: base, y2: base, stroke: COLORS.base}));
  svg.append(svgEl('polygon', {points: [...points, `${W - R},${H - B}`, `0,${H - B}`].join(' '), fill: COLORS.equity, 'fill-opacity': .1}));
  svg.append(svgEl('polyline', {points: points.join(' '), fill: 'none', stroke: COLORS.equity, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'}));
  svg.append(svgEl('circle', {cx: x(curve.length - 1), cy: y(last), r: 4, fill: COLORS.equity, stroke: SURFACE, 'stroke-width': 2}));
  const endLabel = svgEl('text', {x: W - R + 8, y: y(last) + 4, fill: '#e4ecf8'}); endLabel.textContent = fmt(last); svg.append(endLabel);
  if (Math.abs(y(last) - base) >= 13) { const baseLabel = svgEl('text', {x: W - R + 8, y: base + 4, fill: '#8d9db6'}); baseLabel.textContent = fmt(INITIAL_EQUITY); svg.append(baseLabel); }
  box.append(hoverLayer(svg, curve.length, x, i => [tipRow(null, fmt(curve[i].equity) + ' ' + quote, 'มูลค่าพอร์ต'), el('div', 'tipdate', date(curve[i].time))]));
  box.append(el('p', 'footnote', `เริ่ม ${fmt(INITIAL_EQUITY)} · ต่ำสุด ${fmt(Math.min(...values))} · สูงสุด ${fmt(Math.max(...values))} · สิ้นสุด ${fmt(last)} ${quote} · วัดที่ราคาปิดหลังต้นทุนสมมติ ไม่ใช่ intrabar`));
  return box;
}
function comparison(report) {
  const table = el('table'), head = el('tr'); ['ผลทดสอบ', 'ช่วงแรก 70%*', 'ช่วงล่าสุด 30%'].forEach(v => head.append(el('th', '', v))); const thead = el('thead'); thead.append(head); table.append(thead);
  const body = el('tbody'), early = report.earlier, recent = report.holdout;
  [['เริ่มทดสอบ', day(early.start_time), day(recent.start_time)], ['สิ้นสุด', day(early.end_time), day(recent.end_time)],
   ['จำนวนเทรด (ชนะ)', `${early.trades} (${early.wins})`, `${recent.trades} (${recent.wins})`], ['อัตราชนะ', pct(early.win_rate_pct), pct(recent.win_rate_pct)],
   ['กำไรสุทธิพอร์ต', pct(early.net_return_pct), pct(recent.net_return_pct)], ['ผลเฉลี่ยต่อเทรด', pct(early.average_trade_pct), pct(recent.average_trade_pct)],
   ['Drawdown ราคาปิด', pct(early.max_drawdown_pct), pct(recent.max_drawdown_pct)], ['Profit factor', factor(early), factor(recent)]
  ].forEach(row => { const tr = el('tr'); row.forEach(v => tr.append(el('td', '', v))); body.append(tr); }); table.append(body); return table;
}
function factor(report) { return report.profit_factor == null ? (report.profit_factor_status === 'no_losses' ? 'ยังไม่มีเทรดขาดทุน' : '—') : fmt(report.profit_factor); }
function backtest(a) {
  const section = el('section', 'backtest'), h = a.backtest.holdout;
  section.append(el('h3', '', 'ผลทดสอบช่วงล่าสุด 30%'), el('p', 'footnote', date(h.start_time) + ' ถึง ' + date(h.end_time)), el('div', 'evidence', a.evidence));
  const grid = el('div', 'btgrid'); grid.append(metric('อัตราชนะย้อนหลัง · ' + h.trades + ' เทรด', pct(h.win_rate_pct)), metric('กำไรสุทธิพอร์ตจำลอง', pct(h.net_return_pct), h.net_return_pct >= 0 ? 'positive' : 'negative'), metric('Drawdown ที่ราคาปิด', pct(h.max_drawdown_pct)), metric('Profit factor', factor(h))); section.append(grid);
  section.append(el('p', 'footnote', h.win_rate_interval ? `ช่วงประมาณอัตราชนะ 95%: ${pct(h.win_rate_interval[0])}–${pct(h.win_rate_interval[1])} (Wilson; สมมติผลเทรดเป็นอิสระ ซึ่งอาจไม่จริง)` : 'ยังไม่มีเทรดปิด จึงคำนวณอัตราชนะไม่ได้'));
  section.append(equityChart(h, a.quote, 'มูลค่าพอร์ตจำลองช่วงล่าสุด'));
  const details = el('details'); details.append(el('summary', '', 'เปรียบเทียบช่วงเวลาและดูเทรดล่าสุด'), comparison(a.backtest), el('p', 'footnote', `*ช่วงแรกหัก 200 แท่งสำหรับตั้งต้นอินดิเคเตอร์ · ข้อมูลทั้งหมด ${a.backtest.bars} แท่ง · เริ่มพอร์ตจำลองใหม่แต่ละช่วงที่ ${fmt(INITIAL_EQUITY)} ${a.quote}`));
  details.append(equityChart(a.backtest.earlier, a.quote, 'มูลค่าพอร์ตจำลองช่วงแรก 70%'));
  const wrap = el('div', 'tablewrap'), table = el('table'), head = el('tr'); ['เวลาเข้า', 'ทิศทาง', 'ผลต่อพอร์ต', 'เหตุผลปิด'].forEach(t => head.append(el('th', '', t))); const thead = el('thead'); thead.append(head); table.append(thead); const tbody = el('tbody');
  h.recent_trades.slice(-8).forEach(t => { const row = el('tr'); [date(t.entry_time), t.side, pct(t.return_pct), t.reason].forEach(v => row.append(el('td', '', v))); tbody.append(row); }); table.append(tbody); wrap.append(table);
  details.append(el('h3', '', 'เทรดล่าสุดในช่วงทดสอบ'), h.recent_trades.length ? wrap : el('p', 'footnote', 'ยังไม่มีเทรดปิดในช่วงนี้')); section.append(details); return section;
}
function bigPicture(a) {
  const box = el('div', 'context'), ctx = a.context, tf = a.higher_timeframe || '';
  box.append(el('div', 'eyebrow', 'ภาพใหญ่ ' + tf + ' · ปริมาณซื้อขาย'));
  const m = el('div', 'metrics');
  if (ctx) {
    const bias = ctx.bias === 1 ? 'ขาขึ้น' : ctx.bias === -1 ? 'ขาลง' : 'ไม่ชัด';
    m.append(metric('แนวโน้ม ' + tf, bias), metric('RSI ' + tf, fmt(ctx.rsi)), metric('EMA50 / EMA200 ' + tf, fmt(ctx.ema50) + ' / ' + fmt(ctx.ema200)));
  } else {
    m.append(metric('แนวโน้ม ' + tf, 'ไม่มีข้อมูล'));
  }
  const v = a.volume_pressure_pct;
  m.append(metric('ปริมาณฝั่งซื้อ 20 แท่ง', v == null ? 'ไม่มีข้อมูล' : pct(v)));
  box.append(m);
  const notes = [];
  if (ctx) notes.push(`แท่ง ${tf} ปิดล่าสุด ${date(ctx.bar_close)} ราคาปิด ${fmt(ctx.close)}`);
  if (a.context_error) notes.push(a.context_error);
  notes.push(v == null ? 'แหล่งข้อมูลนี้ไม่ส่งปริมาณซื้อขาย เงื่อนไขปริมาณจึงไม่ผ่านโดยอัตโนมัติ' : 'สัดส่วนปริมาณของแท่งที่ปิดบวกต่อปริมาณทั้งหมดใน 20 แท่งล่าสุด (ไม่นับแท่งปิดเท่าเดิม)');
  box.append(el('p', 'footnote', notes.join(' · ')));
  return box;
}
function card(a) {
  const c = el('article', 'card'), top = el('div', 'cardtop');
  top.append(el('h2', '', a.asset === 'BTC' ? '₿ Bitcoin' : '◈ Gold'), el('span', 'pill ' + (a.action || ''), a.error ? 'ยังไม่เชื่อมต่อ' : ({BUY: 'BUY · พิจารณาซื้อ', SELL: 'SELL · พิจารณาขาย', REDUCE: 'REDUCE · ลดที่ถือ', WAIT: 'WAIT · รอ'}[a.action]))); c.append(top, el('p', 'muted', `${a.symbol} · ${a.feed}`));
  if (a.error) { c.append(el('p', 'error', a.error)); if (a.asset === 'GOLD') { const p = el('p', 'empty', 'การอ่านทองผ่าน Twelve Data ต้องตั้ง API key ในเครื่องและมีสิทธิ์ข้อมูล XAU/USD ราคาของโบรกเกอร์อาจต่างจากผู้ให้บริการข้อมูล '); const link = el('a', '', 'ตรวจแพ็กเกจและสร้าง key'); link.href = 'https://twelvedata.com/'; link.target = '_blank'; link.rel = 'noopener noreferrer'; p.append(link); c.append(p); } return c; }
  const price = el('div', 'price', fmt(a.price)); price.append(el('span', 'unit', a.quote)); c.append(price, el('div', 'muted', 'ราคาปิดแท่ง · ' + date(a.bar_close)), priceChart(a));
  const legend = el('div', 'legend'); [['ราคาปิด', 'close'], ['EMA20', 'fast'], ['EMA50', 'slow']].forEach(([text, name]) => { const item = el('span'); item.append(key(COLORS[name]), text); legend.append(item); }); c.append(legend);
  const m = el('div', 'metrics'); m.append(metric('RSI (14)', fmt(a.rsi)), metric('ATR (14)', fmt(a.atr)), metric('EMA (200)', fmt(a.ema200))); c.append(m);
  c.append(bigPicture(a));
  const score = el('div', 'scorebox'), row = el('div', 'scorerow'); row.append(el('span', '', 'คะแนนเข้าเงื่อนไข · ' + a.bias), el('b', '', a.score + '%')); const progress = el('progress'); progress.max = 100; progress.value = a.score; progress.setAttribute('aria-label', 'คะแนนเข้าเงื่อนไข'); score.append(row, progress, el('div', 'footnote', `ไม่ใช่โอกาสกำไร · BUY ${a.buy_score}% / SELL ${a.sell_score}%`)); c.append(score, el('div', 'reason', a.reason));
  const levels = el('div', 'levels'); [['Stop Loss', a.stop, a.stop_move_pct], ['Take Profit', a.target, a.target_move_pct]].forEach(([k, v, p]) => { const item = el('div', 'level'); item.append(el('span', 'muted', k), el('b', '', fmt(v)), el('span', 'footnote', p == null ? 'ไม่มีแผนเปิดสถานะ' : 'ระยะราคา ' + pct(p))); levels.append(item); }); c.append(levels);
  const checks = el('details'); checks.append(el('summary', '', 'เหตุผลคะแนนและสถานะข้อมูล')); const list = el('ul', 'checks'); a.checks.forEach(check => list.append(el('li', check.pass ? 'pass' : 'fail', `${check.pass ? '✓' : '○'} ${check.name} (${check.weight}%)` + (check.note ? ' · ' + check.note : '')))); checks.append(list, el('p', 'footnote', 'ดึงจากแหล่งข้อมูล: ' + date(a.fetched_at)), el('p', 'footnote', a.allow_short ? 'จำลอง Long/Short เชิงทฤษฎี ไม่รวม Swap/Funding' : 'BTC Spot จำลอง Long เท่านั้น SELL ใช้ปิด/ลดสถานะที่มี')); c.append(checks, backtest(a)); return c;
}
async function refresh() {
  if (busy) return; busy = true; ['refresh', 'source', 'timeframe', 'download'].forEach(id => $(id).disabled = true); $('status').textContent = 'กำลังอ่านข้อมูลและทดสอบย้อนหลัง…'; $('assets').classList.add('loading');
  try {
    const query = initialized ? '?' + new URLSearchParams({source: $('source').value, timeframe: $('timeframe').value}) : '';
    const response = await fetch('/api/analysis' + query, {signal: AbortSignal.timeout(30000)}), data = await response.json(); if (!response.ok) throw Error(data.error || 'โหลดข้อมูลไม่ได้');
    initialized = true; lastData = data; $('source').value = data.source; $('timeframe').value = data.timeframe;
    $('banner').textContent = data.source === 'demo' ? 'โหมดสาธิต — ราคาและผลกำไรทั้งหมดมาจากข้อมูลสังเคราะห์ ใช้ตรวจการทำงานเท่านั้น' : 'คะแนนเข้าเงื่อนไขไม่ใช่โอกาสกำไร · อัตราชนะและกำไรด้านล่างเป็นผลย้อนหลังหลังต้นทุนสมมติ ไม่ใช่ผลตอบแทนที่รับรอง';
    const failed = data.assets.filter(a => a.error).length;
    $('status').textContent = `${data.timeframe} · ประมวลผล ${new Date(data.updated).toLocaleTimeString('th-TH')} · พร้อม ${2 - failed}/2 สินทรัพย์ · รีเฟรชหน้าจอทุก 60 วินาที / API เก็บข้อมูลชั่วคราว 5 นาที`;
    $('assets').replaceChildren(...data.assets.map(card)); $('threshold').textContent = data.min_score;
    const s = data.settings; $('assumptions').textContent = `สมมติฐานต่อสินทรัพย์: เสี่ยงถึง Stop ${s.risk_pct}% ของพอร์ตต่อเทรด (รวมต้นทุนโดยประมาณ) · ค่าธรรมเนียม ${s.fee_bps} bps/ข้าง · Slippage + ครึ่งสเปรด ${s.slippage_bps} bps/ข้าง · 1 bp = 0.01% · มูลค่าสถานะไม่เกินทุน 1 เท่า · ถือสูงสุด 48 แท่ง`;
  } catch (e) { lastData = null; $('assets').replaceChildren(); $('status').textContent = 'เชื่อมต่อไม่ได้: ' + e.message; $('banner').textContent = 'ยืนยันข้อมูลล่าสุดไม่ได้ จึงระงับการแสดงคำแนะนำ'; }
  finally { busy = false; $('assets').classList.remove('loading'); ['refresh', 'source', 'timeframe'].forEach(id => $(id).disabled = false); $('download').disabled = !lastData; }
}
$('refresh').onclick = refresh; ['source', 'timeframe'].forEach(id => $(id).onchange = () => { initialized = true; refresh(); });
$('download').onclick = () => { if (!lastData) return; const blob = new Blob([JSON.stringify(lastData, null, 2)], {type: 'application/json'}), url = URL.createObjectURL(blob), link = el('a'); link.href = url; link.download = `easytrade-${lastData.source}-${lastData.timeframe}-${Date.now()}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); };
refresh(); setInterval(refresh, 60000);
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
