"""Self-contained mobile report: embedded data and SVG, no CDN or account data."""
import datetime as dt
import html
import json
import re
from pathlib import Path


def write_report(history, message, missing, failures, requested_date, output_dir, quotes=None, quote_history=None):
    from monitor import series_for, statistics, reference_view, learning_notes, CALENDAR
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    latest = max(r['date'] for r in history)
    display_history=reference_view(history)
    rows = sorted((r for r in display_history if r['date'] == latest), key=lambda r: r['product'])
    series = {p: series_for(display_history, p) for p in ('IC', 'IM')}
    analytics, lessons = {}, {}
    for basis, records in [('reference', display_history), ('nearest', history)]:
        analytics[basis], lessons[basis] = {}, {}
        for r in records:
            if r['date'] != latest:
                continue
            stats = statistics(r, records, quote_history)
            analytics[basis][r['product']] = {k: stats[k] for k in ('pair', 'rolling')}
            lessons[basis][r['product']] = learning_notes(r, records, stats=stats)
    cards = []
    for r in rows:
        stats = analytics['reference'][r['product']]
        percentile = stats['pair']['percentile']
        ptext = f'{percentile:.0f}%' if percentile is not None else '样本不足'
        rolling = stats['rolling']
        rtext = f'{rolling["percentile"]:.0f}%' if rolling['percentile'] is not None else '样本不足'
        window = (f'{rolling["start"] or "无"} 至 {rolling["end"] or "无"} · {rolling["count"]} 次此前观测')
        cards.append(f'''<section class="metric {r['product'].lower()}">
          <div class="metric-top"><strong>{html.escape(r['product'])}</strong><span data-pair="{r['product']}">{html.escape(r['near'])} → {html.escape(r['far'])}</span></div>
          <div class="number"><span data-value="{r['product']}">{float(r['annualized_pct']):+.2f}</span><small>%</small></div>
          <div class="metric-bottom"><span>年化跨期价差</span><span>滚动365日深度分位 <b data-rolling="{r['product']}">{rtext}</b></span></div>
          <div class="subtle">同组深度分位 <b data-percentile="{r['product']}">{ptext}</b> · <span data-count="{r['product']}">{stats['pair']['count']}</span> 次</div>
          <div class="sample-window" data-window="{r['product']}">{window}</div>
          <div class="sample-window" data-pair-window="{r['product']}">同组样本：{stats['pair']['start'] or '无'} 至 {stats['pair']['end'] or '无'}</div>
        </section>''')
    learning_html = ''.join('<details open><summary>'+html.escape(p)+'｜学习解说</summary>'+
        ''.join('<p><strong>'+html.escape(n['title'])+'：</strong>'+html.escape(n['text'])+'</p>'
                for n in lessons['reference'].get(p, []))+'</details>' for p in ('IC', 'IM'))
    days = sorted({r['date'] for r in history})
    requested = requested_date.isoformat()
    stale = requested != latest
    notice = (f'请求日期 {requested} 未取得新的完整行情，以下保留截至 {latest} 的历史报告。'
              if stale else '盘后日频数据，非实时盘口。')
    paragraphs = []
    for line in message.splitlines():
        if not line or line.startswith(('![', '[查看')):
            continue
        tag = 'h3' if line.startswith('### ') else 'p'
        line = line[4:] if tag == 'h3' else line[2:] if line.startswith('- ') else line
        line = re.sub(r'\*\*(.*?)\*\*',r'<strong>\1</strong>',html.escape(line))
        paragraphs.append(f'<{tag}>{line}</{tag}>')
    details = ''.join(paragraphs)
    term_rows = []
    for product in ('IC','IM'):
        group = sorted((q for q in (quotes or []) if q['symbol'].startswith(product)),key=lambda q:q['symbol'])
        for q in group:
            term_rows.append('<tr><td>'+html.escape(q['symbol'])+'</td><td>'+f"{q['close']:.1f}"+
                             '</td><td>'+str(int(q['volume']))+'</td><td>'+str(int(q['oi']))+
                             '</td><td>'+f"{group[0]['close']-q['close']:+.1f}"+'</td></tr>')
    term_table = ''.join(term_rows) or '<tr><td colspan="5">此日期没有完整行情快照，请重新补录。</td></tr>'
    # JSON inside an HTML script element must escape '<', even for imported CSV values.
    payload = json.dumps({'series': series,'nearSeries':{p:series_for(history,p) for p in ('IC','IM')},
        'analytics':analytics, 'lessons':lessons,
        'rawHistory':history,'latest': latest}, ensure_ascii=False).replace('<', '\\u003c')
    missing_text = '、'.join(missing) if missing else '当前覆盖区间内未发现缺失交易日。'
    failure_text = '；'.join(f"{x['date']}：{x['error']}" for x in failures) or '本次无请求失败。'
    page = TEMPLATE.replace('__LATEST__', html.escape(latest)).replace('__NOTICE__', html.escape(notice))
    page = page.replace('__CARDS__', ''.join(cards)).replace('__PAYLOAD__', payload)
    page = page.replace('__LESSONS__', learning_html)
    page = page.replace('__SUMMARY__', html.escape(f'{days[0]} 至 {latest} · {len(days)} 个交易日 · 缺失 {len(missing)} 日'))
    page = page.replace('__DETAILS__', details).replace('__MISSING__', html.escape(missing_text))
    page = page.replace('__FAILURES__', html.escape(failure_text))
    unknown = sorted({dt.date.fromisoformat(r[k]).year for r in rows for k in ('near_expiry','far_expiry')}
                     -set(CALENDAR['covered_years']))
    calendar_note = (f'休市表覆盖 {CALENDAR["covered_years"]}；{unknown} 年到期日为估算，日历日年化仅作近似。'
                     if unknown else f'休市表覆盖 {CALENDAR["covered_years"]}，异常休市和特殊交割调整须及时更新。')
    page = page.replace('__CALENDAR__', html.escape(calendar_note)).replace('__TERM__',term_table)
    (output_dir / 'index.html').write_text(page, encoding='utf-8')
    (output_dir / 'history.json').write_text(json.dumps(history, ensure_ascii=False), encoding='utf-8')
    return output_dir / 'index.html'


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>IC / IM 贴水观察 · __LATEST__</title>
<style>
:root{--paper:#f6f5f0;--ink:#242b2b;--muted:#626e6b;--line:#d8ded7;--ic:#317466;--im:#5d66b8}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:1040px;padding:40px 36px 70px;margin:auto}header{border-top:5px solid var(--ink);padding-top:22px;margin-bottom:26px}
.eyebrow{font-size:12px;letter-spacing:.15em;color:var(--muted)}h1{font-size:32px;line-height:1.25;margin:8px 0 14px;letter-spacing:-.03em}h2{font-size:19px;margin:0}
.meta,.subtle{font-size:13px;color:var(--muted)}.notice{margin-top:12px}.metrics{display:grid;grid-template-columns:1fr 1fr;border-block:1px solid var(--line);margin:24px 0 32px}
.metric{padding:20px 22px 18px 0}.metric+.metric{border-left:1px solid var(--line);padding-left:26px}.metric-top{display:flex;align-items:baseline;justify-content:space-between;gap:8px;font-size:12px;color:var(--muted)}.metric-top strong{font-size:22px;color:var(--ink)}
.number{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:52px;line-height:1.5;letter-spacing:-.06em;font-variant-numeric:tabular-nums}.number small{font-size:24px;margin-left:4px}.ic .number{color:var(--ic)}.im .number{color:var(--im)}
.metric-bottom{display:flex;flex-wrap:wrap;justify-content:space-between;gap:4px 12px;font-size:12px;color:var(--muted)}
.sample-window{font-size:11px;color:var(--muted);margin-top:8px;overflow-wrap:anywhere}
.section-top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.ranges{display:flex;gap:4px}button{font:inherit;font-size:12px;padding:5px 10px;background:transparent;border:1px solid var(--line);border-radius:4px;color:var(--ink);cursor:pointer;min-height:36px}button[aria-pressed=true]{background:var(--ink);color:white;border-color:var(--ink)}button:active{transform:translateY(1px)}button:focus-visible,a:focus-visible,select:focus-visible{outline:2px solid var(--im);outline-offset:3px}
select{font:inherit;font-size:13px;max-width:100%;padding:6px;border:1px solid var(--line);background:white;color:var(--ink)}.legend{display:flex;gap:18px;align-items:center;font-size:12px;margin:10px 0;color:var(--muted)}.legend i{display:inline-block;width:18px;height:3px;margin-right:5px;vertical-align:middle}
.chart{background:#fff;border:1px solid var(--line);padding:10px 8px 4px;overflow:hidden}.chart svg{display:block;width:100%;height:auto;touch-action:pan-y}.chart-note{font-size:12px;color:var(--muted);margin:8px 0 26px}.readout{font:12px/1.7 ui-monospace,SFMono-Regular,monospace;min-height:44px;padding:7px 12px;background:#eeefe9;overflow-wrap:anywhere}
.section{margin-top:28px;padding-top:22px;border-top:1px solid var(--line)}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;margin:14px 0}th,td{padding:10px 9px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}th{font-weight:500;color:var(--muted)}th:first-child,td:first-child{text-align:left}
details{border-bottom:1px solid var(--line);padding:14px 0}summary{cursor:pointer;font-weight:600}details p{margin:8px 0;font-size:14px;overflow-wrap:anywhere}a{color:var(--ic)}footer{font-size:12px;color:var(--muted);margin-top:32px;line-height:1.9}.empty{padding:30px}
@media(max-width:600px){main{padding:22px 18px 44px}h1{font-size:28px}.metrics{margin-top:20px}.metric{padding:14px 12px 14px 0}.metric+.metric{padding-left:14px}.metric-top{display:block}.metric-top span{display:block;font-size:11px}.number{font-size:38px}.number small{font-size:19px}.metric-bottom{display:block}.metric-bottom span{display:block}h2{font-size:17px}.ranges button{padding:4px 9px}.readout{font-size:11px}}
@media(prefers-reduced-motion:reduce){button:active{transform:none}}
</style></head><body><main>
<header><div class="eyebrow">INDEX FUTURES / DAILY OBSERVATORY</div><h1>IC / IM 贴水观察</h1>
<div class="meta">交易日 __LATEST__ · 中金所盘后收盘价</div><div class="notice subtle">__NOTICE__</div></header>
<div class="metrics">__CARDS__</div>
<p class="subtle">深度分位越高，折价相对所选历史越深。滚动365日比较同一选约规则下的不同合约组；同组只比较相同两张合约。均排除当天，至少20次才显示；未按期限与季节校准，分位不是胜率。</p>
<section><div class="section-top"><h2>季度价差的历史位置</h2><div class="ranges" aria-label="选择图表区间">
<button type="button" data-days="30" aria-pressed="false">1个月</button><button type="button" data-days="180" aria-pressed="false">半年</button><button type="button" data-days="365" aria-pressed="true">1年</button><button type="button" data-days="0" aria-pressed="false">全部</button></div></div>
<p class="subtle"><label for="basis">指标口径：</label><select id="basis"><option value="reference">挂牌远端季月（文章对照）</option><option value="nearest">最近两个季月（辅助）</option></select></p><div class="legend"><span><i style="background:var(--ic)"></i>IC 中证500</span><span><i style="background:var(--im)"></i>IM 中证1000</span><span>单位：%</span></div>
<div class="chart"><svg id="chart" viewBox="0 0 900 360" role="img" aria-label="IC和IM年化跨期价差历史走势图"></svg><div id="readout" class="readout" aria-live="polite">点击图线查看日期与合约。</div></div>
<div class="chart-note">负值更低表示远季折价更深。断线表示合约组切换或缺失交易日；不跨断点推断涨跌。<br>__SUMMARY__</div></section>
<section class="section"><h2>新手学习与模拟</h2><p class="subtle">规则解说，未调用 AI API。围绕数据解释和纸面练习，不读取个人账户。</p><div id="lessons">__LESSONS__</div></section>
<section class="section"><div class="section-top"><h2>最近的每日记录</h2><button type="button" id="download">下载完整 CSV</button></div>
<div class="scroll"><table><thead><tr><th>交易日</th><th>IC 年化</th><th>IM 年化</th><th>IC 合约组</th><th>IM 合约组</th></tr></thead><tbody id="records"></tbody></table></div></section>
<section class="section"><h2>各到期月份的价格与流动性</h2><div class="scroll"><table><thead><tr><th>合约</th><th>收盘价</th><th>成交量（手）</th><th>持仓量（手）</th><th>最近合约减本合约（点）</th></tr></thead><tbody>__TERM__</tbody></table></div><p class="subtle">价差按盘后价格计算；实际换仓还需买卖盘、手续费和持仓方向。每点对应200元/手，表内点差未扣成本。</p></section>
<section class="section"><h2>解释与数据核查</h2>
<details><summary>今日完整记录与计算说明（文章对照口径）</summary>__DETAILS__</details>
<details><summary>缺失交易日</summary><p>__MISSING__</p></details>
<details><summary>本次请求失败记录</summary><p>__FAILURES__</p></details>
<details><summary>口径与边界</summary><p>默认文章对照口径选择挂牌最远两个连续季月；辅助口径选择最近两个有效季月。合约月份重合时二者相同，临近月度到期后可能不同。<br>年化跨期价差 =（远季收盘价 / 近季收盘价 − 1）× 4 × 100%。这是两张期货合约之间的季度斜率，不是现货基差，也不是账户年收益。</p><p>同组分位使用此前最多90次相同两张合约观测，从已选指标及完整报价快照还原，每日最多一次；滚动365日分位使用此前365个自然日内同一选约规则的每日指标，包含不同合约组。两者均不含当前值，至少20次才显示，不插值。深度分位 = 此前价差年化值大于等于当前值的次数 / 此前有效次数。样本有时间相关性，未按剩余期限、季节和市场状态校准；观察阈值尚未回测，滚动分位不触发买卖建议。</p><p>__CALENDAR__</p><p>未读取个人持仓、实时买卖盘和实际费用。当前为学习与模拟模式；多头、空头套保及跨期交易的判断不同。</p><p>交易制度依据：<a href="http://www.cffex.com.cn/cn/zz500.html">IC合约表</a>、<a href="http://www.cffex.com.cn/cn/ssxz/20220718/43093.html">IM交易细则</a>；概念参考：<a href="http://www.cffex.com.cn/cn/jysdt/20131126/17440.html">中金所升贴水教育文章</a>（历史教育材料，非当前行情依据）。</p></details></section>
<footer>数据来源：<a href="https://www.cffex.com.cn/">中国金融期货交易所</a>。历史为具体月份合约，不使用主力连续价格拼接。<br>本页为生成时的静态快照，请先检查顶部交易日期。数据和图表内嵌，可离线查看。</footer>
</main><script type="application/json" id="data">__PAYLOAD__</script><script>
'use strict';
const data=JSON.parse(document.getElementById('data').textContent),svg=document.getElementById('chart'),ns='http://www.w3.org/2000/svg';
const colors={IC:'#317466',IM:'#5d66b8'},parseDate=s=>Date.parse(s+'T00:00:00Z'),format=v=>Number(v).toFixed(2)+'%';
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let drawn=[],basis="reference",activeSeries=data.series,range=365;
function add(tag,attrs,text){const el=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))el.setAttribute(k,String(v));if(text!==undefined)el.textContent=text;svg.appendChild(el);return el;}
function draw(days){range=days;svg.replaceChildren();const cutoff=days?parseDate(data.latest)-(days-1)*86400000:-Infinity;drawn=Object.values(activeSeries).flat().filter(r=>parseDate(r.date)>=cutoff);if(!drawn.length)return;
const times=drawn.map(r=>parseDate(r.date)),vals=drawn.map(r=>r.annualized_pct);let xmin=Math.min(...times),xmax=Math.max(...times);if(xmin===xmax)xmax+=86400000;
let ymin=Math.floor(Math.min(...vals)-.6),ymax=Math.ceil(Math.max(...vals)+.6);if(ymin===ymax)ymax=ymin+1;
const narrow=window.innerWidth<600,W=narrow?560:900,R=W-32,span=R-58,ticks=narrow?2:4,font=narrow?17:13;svg.setAttribute('viewBox','0 0 '+W+' 360');const X=t=>58+(t-xmin)/(xmax-xmin)*span,Y=v=>22+(ymax-v)/(ymax-ymin)*284;
for(let i=0;i<=4;i++){const value=ymin+(ymax-ymin)*i/4,y=Y(value);add('line',{x1:58,x2:R,y1:y,y2:y,stroke:'#e4e8e2'});add('text',{x:48,y:y+4,'text-anchor':'end',fill:'#64706a','font-size':font},value.toFixed(1));}
for(let i=0;i<=ticks;i++){const t=xmin+(xmax-xmin)*i/ticks;add('text',{x:X(t),y:333,'text-anchor':i===0?'start':i===ticks?'end':'middle',fill:'#64706a','font-size':font},new Date(t).toISOString().slice(0,10));}
for(const p of ['IC','IM']){const points=activeSeries[p].filter(r=>parseDate(r.date)>=cutoff);let path='';for(let i=0;i<points.length;i++){const r=points[i],x=X(parseDate(r.date)),y=Y(r.annualized_pct);path+=(i===0||r.break_before?'M':'L')+x.toFixed(2)+','+y.toFixed(2)+' ';if(r.roll)add('line',{x1:x,x2:x,y1:22,y2:306,stroke:colors[p],'stroke-opacity':.16,'stroke-dasharray':'3 5'});if(points.length<45||i===points.length-1){const dot=add('circle',{cx:x,cy:y,r:points.length<45?2.5:4,fill:colors[p]});const title=document.createElementNS(ns,'title');title.textContent=r.date+' '+p+' '+format(r.annualized_pct)+' '+r.near+'/'+r.far;dot.appendChild(title);}}add('path',{d:path,fill:'none',stroke:colors[p],'stroke-width':2,'stroke-linejoin':'round'});}
const cover=add('rect',{x:58,y:15,width:span,height:300,fill:'transparent'});cover.addEventListener('pointerdown',e=>{const box=svg.getBoundingClientRect(),v=(e.clientX-box.left)/box.width*W,t=xmin+(Math.max(58,Math.min(R,v))-58)/span*(xmax-xmin);const nearest=drawn.reduce((a,b)=>Math.abs(parseDate(b.date)-t)<Math.abs(parseDate(a.date)-t)?b:a);const found=drawn.filter(r=>r.date===nearest.date);document.getElementById('readout').textContent=nearest.date+' ｜ '+found.map(r=>r.product+' '+format(r.annualized_pct)+' '+r.near+'/'+r.far).join(' ｜ ');});
document.getElementById('readout').textContent='截至 '+data.latest+' ｜ '+['IC','IM'].map(p=>{const r=activeSeries[p].at(-1);return r?p+' '+format(r.annualized_pct):'';}).join(' ｜ ');
}
document.querySelectorAll('[data-days]').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('[data-days]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));draw(Number(b.dataset.days));}));draw(365);
function table(){document.getElementById("records").replaceChildren();const byDate=new Map;for(const[p,rows]of Object.entries(activeSeries))for(const r of rows){if(!byDate.has(r.date))byDate.set(r.date,{});byDate.get(r.date)[p]=r;}
for(const date of [...byDate.keys()].sort().reverse().slice(0,15)){const row=byDate.get(date),tr=document.createElement('tr');tr.innerHTML='<td>'+esc(date)+'</td>'+['IC','IM'].map(p=>'<td>'+esc(row[p]?format(row[p].annualized_pct):'缺失')+'</td>').join('')+['IC','IM'].map(p=>'<td>'+esc(row[p]?row[p].near+'/'+row[p].far:'缺失')+'</td>').join('');document.getElementById('records').appendChild(tr);}}table();
document.getElementById('basis').addEventListener('change',e=>{basis=e.target.value;activeSeries=basis==='reference'?data.series:data.nearSeries;for(const p of ['IC','IM']){const r=activeSeries[p].at(-1);if(!r)continue;document.querySelector('[data-pair="'+p+'"]').textContent=r.near+' → '+r.far;document.querySelector('[data-value="'+p+'"]').textContent=Number(r.annualized_pct).toFixed(2);const stats=data.analytics[basis][p],pair=stats.pair,roll=stats.rolling;document.querySelector('[data-percentile="'+p+'"]').textContent=pair.percentile===null?'样本不足':pair.percentile.toFixed(0)+'%';document.querySelector('[data-count="'+p+'"]').textContent=pair.count;document.querySelector('[data-rolling="'+p+'"]').textContent=roll.percentile===null?'样本不足':roll.percentile.toFixed(0)+'%';document.querySelector('[data-window="'+p+'"]').textContent=(roll.start||'无')+' 至 '+(roll.end||'无')+' · '+roll.count+' 次此前观测';}const lessons=document.getElementById('lessons');lessons.replaceChildren();for(const p of ['IC','IM']){const detail=document.createElement('details'),summary=document.createElement('summary');detail.open=true;summary.textContent=p+'｜学习解说';detail.appendChild(summary);for(const note of data.lessons[basis][p]||[]){const paragraph=document.createElement('p'),title=document.createElement('strong');title.textContent=note.title+'：';paragraph.appendChild(title);paragraph.appendChild(document.createTextNode(note.text));detail.appendChild(paragraph);}lessons.appendChild(detail);}draw(range);table();});
document.getElementById('download').addEventListener('click',()=>{const rows=data.rawHistory.slice().sort((a,b)=>(a.date+a.product).localeCompare(b.date+b.product)),fields=['date','product','near','far','near_close','far_close','annualized_pct','reference_near','reference_far','reference_annualized_pct','near_volume','far_volume'];const cell=v=>'"'+String(v??'').replace(/"/g,'""')+'"';const csv='\ufeff'+[fields,...rows.map(r=>fields.map(k=>r[k]))].map(r=>r.map(cell).join(',')).join('\r\n');const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='ic-im-history-'+data.latest+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
document.getElementById('basis').addEventListener('change',()=>{for(const p of ['IC','IM']){const pair=data.analytics[basis][p].pair;document.querySelector('[data-pair-window="'+p+'"]').textContent='同组样本：'+(pair.start||'无')+' 至 '+(pair.end||'无');}});
</script></body></html>'''
