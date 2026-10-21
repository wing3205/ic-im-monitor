#!/usr/bin/env python3
"""IC/IM 每日跨期贴水监控：中金所日行情 -> CSV -> 图表 -> Server酱。
无实盘交易功能。Python 3.12+。全部行情均以同一个交易日的官方盘后数据为准。
"""
import argparse
import hashlib
import json
import contextlib
import csv
import datetime as dt
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

BASE = Path(__file__).resolve().parent
HISTORY = BASE / 'data' / 'history.csv'
CHART = BASE / 'output' / 'quarter_spread.png'
QUOTES = BASE / 'data' / 'quotes'
STATUS = BASE / 'data' / 'ingest_status.json'
PUSH_STATE = BASE / 'data' / 'push_state.json'
CALENDAR = json.loads((BASE / 'trading_calendar.json').read_text(encoding='utf-8'))
COVERED_YEARS = set(CALENDAR['covered_years'])
TZ = dt.timezone(dt.timedelta(hours=8))
HTTPS_FAILED = False
FIELDS = [
    'date', 'product', 'near', 'far', 'near_close', 'far_close',
    'near_expiry', 'far_expiry', 'spread_points', 'annualized_pct',
    'daycount_annualized_pct', 'near_volume', 'far_volume',
    'near_open_interest', 'far_open_interest',
    'front', 'next', 'front_close', 'next_close',
    'front_volume', 'next_volume', 'front_open_interest', 'next_open_interest',
] + ['reference_' + k for k in ('near','far','near_close','far_close','near_expiry','far_expiry',
    'spread_points','annualized_pct','daycount_annualized_pct','near_volume','far_volume',
    'near_open_interest','far_open_interest')]
SYMBOL_RE = re.compile(r'^(IC|IM)(\d{2})(\d{2})$')


def as_float(text):
    value = float(str(text).strip().replace(',', ''))
    if not math.isfinite(value):
        raise ValueError('non-finite number')
    return value


def month_expiry(symbol):
    match = SYMBOL_RE.fullmatch(symbol)
    if not match:
        raise ValueError(f'不支持的合约: {symbol}')
    y = 2000 + int(match.group(2))
    m = int(match.group(3))
    if m not in range(1, 13):
        raise ValueError(f'不合法月份: {symbol}')
    first = dt.date(y, m, 1)
    first_friday = 1 + (4 - first.weekday()) % 7
    if symbol in CALENDAR.get('expiry_overrides', {}):
        return dt.date.fromisoformat(CALENDAR['expiry_overrides'][symbol])
    expiry = dt.date(y, m, first_friday + 14)
    while not is_trading_day(expiry):
        expiry += dt.timedelta(days=1)
    return expiry


def is_trading_day(date):
    return date.weekday() < 5 and not any(
        start <= date.isoformat() <= end for start, end in CALENDAR['closed_ranges'])


def expected_previous(date):
    date -= dt.timedelta(days=1)
    while not is_trading_day(date):
        date -= dt.timedelta(days=1)
    return date


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def get_daily(date: dt.date, timeout=12):
    """官方同日日行情；记录实际 URL/哈希，HTTP 回退不伪称加密传输。"""
    global HTTPS_FAILED
    path = f'/sj/hqsj/rtj/{date:%Y%m}/{date:%d}/index.xml'
    protocols = ('http',) if HTTPS_FAILED else ('https', 'http')
    errors = []
    for protocol in protocols:
        url = f'{protocol}://www.cffex.com.cn{path}'
        for attempt in range(2):
            try:
                req = Request(url, headers={'User-Agent': 'Mozilla/5.0 IC-IM-observer/2.0'})
                with urlopen(req, timeout=timeout) as response:
                    content = response.read(3_000_001)
                if len(content) > 3_000_000:
                    raise ValueError('行情响应过大')
                rows = parse_daily_xml(content, date)
                for row in rows:
                    row.update(source_url=url, source_sha256=hashlib.sha256(content).hexdigest(),
                               fetched_at=dt.datetime.now(TZ).isoformat(timespec='seconds'))
                return rows
            except HTTPError as exc:
                errors.append(f'{protocol}: HTTP {exc.code}')
                if exc.code < 500:
                    break
            except (URLError, TimeoutError, OSError) as exc:
                errors.append(f'{protocol}: {type(exc).__name__}')
                if protocol == 'https':
                    HTTPS_FAILED = True
                    break
            if attempt == 0:
                time.sleep(1)
    raise RuntimeError('官方行情暂不可用：' + '; '.join(errors))


def parse_daily_xml(content, date):
    if b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():
        raise ValueError('拒绝解析包含外部实体声明的 XML')
    root = ET.fromstring(content)
    rows = []
    for node in root.iter():
        if node.tag.split('}')[-1].lower() != 'dailydata':
            continue
        f = {child.tag.split('}')[-1].lower(): (child.text or '').strip() for child in node}
        symbol = f.get('instrumentid', '').upper()
        if not SYMBOL_RE.fullmatch(symbol):
            continue
        if f.get('tradingday') != date.strftime('%Y%m%d'):
            raise ValueError(f'行情交易日不一致: {symbol}, {f.get("tradingday")}')
        try:
            volume=as_float(f['volume'])
            if volume <= 0:
                continue # 没有成交时不使用虚构的收盘价
            row = dict(symbol=symbol, close=as_float(f['closeprice']),
                       volume=volume, oi=as_float(f['openinterest']),
                       settlement=as_float(f.get('settlementprice') or f['closeprice']))
        except (KeyError, ValueError) as e:
            raise ValueError(f'{symbol} 行情字段缺失/非法: {e}') from e
        if row['close'] <= 0 or row['oi'] < 0:
            continue
        if row['symbol'] in {x['symbol'] for x in rows}:
            raise ValueError(f'重复合约: {symbol}')
        rows.append(row)
    if not rows:
        raise ValueError('数据中没有有效的IC/IM合约；行情结构可能变更，拒绝给出贴水数字')
    return rows


def compute(date, quotes, product):
    a = [q for q in quotes if q['symbol'].startswith(product) and month_expiry(q['symbol']) >= date]
    a.sort(key=lambda q: month_expiry(q['symbol']))
    quarters = [q for q in a if int(q['symbol'][-2:]) in (3, 6, 9, 12)]
    if len(quarters) < 2 or len(a) < 2:
        raise ValueError(f'{product} 有效当季/下季合约不足，拒绝凑数')
    near, far = quarters[:2]
    front, next_ = a[:2]
    # 两个季度合约必须相差3个月；到期周附近如出现缺数据，不误选6个月跨度
    dm = (int(far['symbol'][-4:-2]) - int(near['symbol'][-4:-2])) * 12 + \
         int(far['symbol'][-2:]) - int(near['symbol'][-2:])
    if dm != 3:
        raise ValueError(f'{product} 季月不是连续两个季度: {near["symbol"]}, {far["symbol"]}')
    days = (month_expiry(far['symbol']) - month_expiry(near['symbol'])).days
    spread = near['close'] - far['close']
    result = dict(
        date=date.isoformat(), product=product,
        near=near['symbol'], far=far['symbol'],
        near_close=near['close'], far_close=far['close'],
        near_expiry=month_expiry(near['symbol']).isoformat(),
        far_expiry=month_expiry(far['symbol']).isoformat(),
        spread_points=round(spread, 3),
        annualized_pct=round((far['close'] / near['close'] - 1) * 4 * 100, 4),
        daycount_annualized_pct=round((far['close'] / near['close'] - 1) * 365 / days * 100, 4),
        near_volume=int(near['volume']), far_volume=int(far['volume']),
        near_open_interest=int(near['oi']), far_open_interest=int(far['oi']),
        front=front['symbol'], next=next_['symbol'],
        front_close=front['close'], next_close=next_['close'],
        front_volume=int(front['volume']), next_volume=int(next_['volume']),
        front_open_interest=int(front['oi']), next_open_interest=int(next_['oi']),
    )
    ref_near, ref_far = quarters[-2:]
    ref_months = ((int(ref_far['symbol'][2:4])-int(ref_near['symbol'][2:4]))*12+
                  int(ref_far['symbol'][-2:])-int(ref_near['symbol'][-2:]))
    if ref_months != 3:
        raise ValueError('挂牌远端季月不连续，拒绝计算文章对照口径')
    for prefix, quote in [('near',ref_near),('far',ref_far)]:
        result['reference_'+prefix] = quote['symbol']
        result['reference_'+prefix+'_close'] = quote['close']
        result['reference_'+prefix+'_expiry'] = month_expiry(quote['symbol']).isoformat()
        result['reference_'+prefix+'_volume'] = int(quote['volume'])
        result['reference_'+prefix+'_open_interest'] = int(quote['oi'])
    ref_days=(month_expiry(ref_far['symbol'])-month_expiry(ref_near['symbol'])).days
    ratio=ref_far['close']/ref_near['close']-1
    result['reference_spread_points']=round(ref_near['close']-ref_far['close'],3)
    result['reference_annualized_pct']=round(ratio*400,4)
    result['reference_daycount_annualized_pct']=round(ratio*365/ref_days*100,4)
    return result


def reference_view(history):
    result=[]
    for row in history:
        # Old V1 rows without reference fields are excluded rather than relabelled.
        if not row.get('reference_near'):
            continue
        view=dict(row)
        for key,value in row.items():
            if key.startswith('reference_'):
                view[key[len('reference_'):]]=value
        result.append(view)
    return result



def read_history():
    if not HISTORY.exists():
        return []
    with HISTORY.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    keys = set()
    for r in rows:
        dt.date.fromisoformat(r['date'])
        if r['product'] not in ('IC','IM'):
            raise ValueError('历史品种非法')
        for field in ('near','far','front','next'):
            if not SYMBOL_RE.fullmatch(r[field]) or not r[field].startswith(r['product']):
                raise ValueError('历史合约代码非法')
        for field in ('near_close','far_close','front_close','next_close'):
            if as_float(r[field]) <= 0:
                raise ValueError('历史价格必须大于零')
        for field in ('annualized_pct','daycount_annualized_pct','spread_points'):
            as_float(r[field])
        key = (r['date'],r['product'])
        if key in keys:
            raise ValueError('历史存在重复日期/品种')
        keys.add(key)
    return rows


def write_history(old, new):
    # 只以交易日 + 品种去重；后到的数据修正同日条目
    d = {(r['date'], r['product']): r for r in old}
    for r in new:
        d[(r['date'], r['product'])] = r
    ordered = [d[k] for k in sorted(d)]
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    temp = HISTORY.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction='ignore')
        w.writeheader()
        w.writerows(ordered)
    temp.replace(HISTORY)
    return ordered


def sample_summary(rows, value):
    return dict(count=len(rows), start=rows[0]['date'] if rows else None,
                end=rows[-1]['date'] if rows else None,
                percentile=(100 * sum(as_float(x['annualized_pct']) >= value for x in rows) / len(rows)
                            if len(rows) >= 20 else None))


def statistics(r, history, quote_history=None):
    # The selected daily series supplies the previous value and the rolling context.
    by_date = {x['date']: x for x in history
               if x['product'] == r['product'] and x['date'] < r['date']}
    rows = [by_date[d] for d in sorted(by_date)]
    previous = rows[-1] if rows else None
    same_by_date = {x['date']: x for x in rows if (x['near'], x['far']) == (r['near'], r['far'])}
    # A pair may have traded before the selection rule started displaying it.
    # Canonical snapshots override derived indicator rows, one observation per date.
    for day, quotes in (quote_history or {}).items():
        dt.date.fromisoformat(day)
        if day >= r['date']:
            continue
        pair = [q for q in quotes if q.get('symbol') in (r['near'], r['far'])]
        if not pair:
            continue
        same_by_date.pop(day, None)
        if len(pair) != 2 or {q['symbol'] for q in pair} != {r['near'], r['far']}:
            continue
        try:
            if any(as_float(q.get('close')) <= 0 or as_float(q.get('volume', 0)) <= 0 for q in pair):
                continue
            prices = {q['symbol']: as_float(q['close']) for q in pair}
        except (TypeError, ValueError):
            continue
        same_by_date[day] = dict(date=day, product=r['product'], near=r['near'], far=r['far'],
                                annualized_pct=round((prices[r['far']] / prices[r['near']] - 1) * 400, 4))
    same = [same_by_date[d] for d in sorted(same_by_date)][-90:]
    a = as_float(r['annualized_pct'])
    pair_summary = sample_summary(same, a)
    cutoff = (dt.date.fromisoformat(r['date']) - dt.timedelta(days=365)).isoformat()
    rolling = sample_summary([x for x in rows if x['date'] >= cutoff], a)
    rolling['window_start'] = cutoff
    return dict(previous=previous, same=same, percentile=pair_summary['percentile'],
                pair=pair_summary, rolling=rolling)


def learning_notes(r, history, quote_history=None, stats=None):
    stats = stats if stats is not None else statistics(r, history, quote_history)
    a = as_float(r['annualized_pct'])
    previous = stats['previous']
    if previous and (previous['near'], previous['far']) == (r['near'], r['far']):
        delta = a - as_float(previous['annualized_pct'])
        old_value = as_float(previous['annualized_pct'])
        if delta > 0:
            direction = '远季相对近季变贵，' + ('折价变浅' if a < 0 else
                         '由折价转为平价或升水' if old_value < 0 else '升水扩大')
        elif delta < 0:
            direction = '远季相对近季变便宜，' + ('升水收窄' if a > 0 else
                         '由升水转为平价或折价' if old_value > 0 else '折价加深')
        else:
            direction = '跨期比值基本不变'
        change = f'较 {previous["date"]} 变化 {delta:+.2f} 个百分点：{direction}。'
    elif previous:
        change = '本次合约组已切换，换组前后的变化不能直接解释为市场涨跌。'
    else:
        change = '暂无同口径的此前记录可比较。'
    relation = ('远季价格低于近季' if a < 0 else '远季价格高于近季' if a > 0 else '远季与近季价格相同')
    rolling = stats['rolling']
    if rolling['percentile'] is None:
        position = f'滚动365日只有 {rolling["count"]} 个此前样本，至少20个才显示排名。'
    else:
        position = (f'滚动365日深度分位 {rolling["percentile"]:.0f}%（{rolling["count"]}次）；'
                    '数字越高，表示折价相对该品种自身历史越深，不代表盈利概率。')
    position += (f' 同组合约有 {stats["pair"]["count"]} 个此前样本；'
                 + ('暂不足20个，不能给出同组分位。' if stats['percentile'] is None else
                    f'同组深度分位 {stats["percentile"]:.0f}%。'))
    position += ' 两类排名的比较基准不同，均未按剩余期限、季度和市场状态校准。'
    nominal = as_float(r['near_close']) * 200
    return [
        dict(title='今天的数据怎么读', text=f'{relation}。{change}跨期价差描述相对价格，不能单独判断指数涨跌。'),
        dict(title='历史位置怎么读', text=position),
        dict(title='今天的模拟练习', text=(f'暂未持仓，先做模拟记录：{r["near"]}/{r["far"]} 的价差 '
             f'{as_float(r["spread_points"]):+.1f} 点；下一交易日仅在同组内比较变化，换组另记。'
             '纸面换仓还应单列买卖价差、手续费和资金占用；当前分位本身不足以形成开仓结论。')),
        dict(title='一手意味着什么', text=(f'{r["near"]} 按收盘价计算，一手名义价值约 {nominal:,.0f} 元；'
             '一手发生100点不利变动，价格损失为20,000元，未计费用。保证金不是最大可能损失。'
             f'年化跨期价差 {a:+.2f}% 是近远季比价的年化表达，不能当作账户年收益。')),
    ]


def describe(r, history, quote_history=None):
    stats = statistics(r, history, quote_history)
    previous = stats['previous']
    a = float(r['annualized_pct'])
    if previous is None:
        comparison = '暂无此前记录'
    elif (previous['near'], previous['far']) != (r['near'], r['far']):
        comparison = (f'合约组已切换：{previous["near"]}/{previous["far"]} → '
                      f'{r["near"]}/{r["far"]}，不跨组合计算日变化')
    else:
        comparison = (f'较上次记录 {previous["date"]} '
                      f'{a-float(previous["annualized_pct"]):+.2f}个百分点')
    percentile = stats['percentile']
    ptxt = (f'此前同组合约 {len(stats["same"])} 次观测，折价深度分位 {percentile:.0f}%'
            if percentile is not None else f'此前同组合约只有 {len(stats["same"])} 个样本，暂不生成分位')
    ptxt += f'；样本区间 {stats["pair"]["start"] or "无"} 至 {stats["pair"]["end"] or "无"}'
    days = (dt.date.fromisoformat(r['near_expiry'])-dt.date.fromisoformat(r['date'])).days
    rolling = stats['rolling']
    rolling_text = (f'过去365日滚动口径深度分位 {rolling["percentile"]:.0f}%，'
                    if rolling['percentile'] is not None else '过去365日滚动口径样本不足，')
    rolling_text += (f'{rolling["count"]} 次此前观测（{rolling["start"] or "无"} 至 '
                     f'{rolling["end"] or "无"}）；不同具体合约，未按期限校准')
    observation = '学习模式：先做模拟记录，分位不足时保留未知，不据此开仓。'
    if percentile is not None:
        if a >= 0:
            observation = '当前远季未折价，不能把历史分位解释为贴水收益。'
        elif percentile >= 80:
            observation = '远季折价处于同组历史较深区域，适合记录后续相对价差变化，不能直接推导买入结论。'
        elif percentile <= 20:
            observation = '远季折价处于同组历史较浅区域，可在纸面上比较近月与远月换仓成本。'
        else:
            observation = '远季折价处于同组历史中间区域，暂未触发深浅分位观察条件。'
    if days <= 7:
        observation += f' 近季距到期 {days} 个自然日，优先核查到期安排。'
    return '\n'.join([
        f'### {r["product"]}｜{r["near"]} → {r["far"]}',
        f'- 近季收盘 **{float(r["near_close"]):.1f}**，远季 **{float(r["far_close"]):.1f}**',
        f'- 年化跨期价差：**{a:+.2f}%**；{comparison}',
        f'- 近季减远季：{float(r["spread_points"]):+.1f}点；日历日年化近似 {float(r["daycount_annualized_pct"]):+.2f}%',
        f'- {ptxt}（描述性统计，未按剩余期限校准）',
        f'- {rolling_text}',
        f'- 近季/远季成交量 {r["near_volume"]}/{r["far_volume"]}手；持仓量 {r["near_open_interest"]}/{r["far_open_interest"]}手',
        f'- 最近两个有效合约：{r["front"]} {r["front_close"]} / {r["next"]} {r["next_close"]}',
        f'- 观察：{observation}',
    ] + [f'- {note["title"]}：{note["text"]}' for note in learning_notes(r, history, stats=stats)])


def series_for(history, product):
    result = []
    previous = None
    for r in sorted((x for x in history if x['product'] == product), key=lambda x: x['date']):
        point = dict(r)
        point['annualized_pct'] = float(r['annualized_pct'])
        point['break_before'] = bool(previous and (
            (previous['near'], previous['far']) != (r['near'], r['far']) or
            previous['date'] != expected_previous(dt.date.fromisoformat(r['date'])).isoformat()))
        point['roll'] = bool(previous and (previous['near'], previous['far']) != (r['near'], r['far']))
        result.append(point)
        previous = r
    return result


def make_chart(history):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(11, 5.5))
    for product, color in [('IC', '#317466'), ('IM', '#5d66b8')]:
        points = series_for(history, product)
        xs, ys = [], []
        for r in points:
            date = dt.date.fromisoformat(r['date'])
            if r['break_before']:
                xs.append(date)
                ys.append(float('nan'))
            xs.append(date)
            ys.append(r['annualized_pct'])
        ax.plot(xs, ys, color=color, lw=1.5, marker='.', ms=2.5, label=product)
    ax.set_title('IC / IM | Quarterly spread, annualized x4 | Close prices')
    ax.set_ylabel('Annualized spread (%)')
    ax.grid(alpha=.18)
    ax.legend(frameon=False)
    ax.text(0, -.25, 'Gaps mark contract rollovers or missing trading days. Discount is not account return.',
            transform=ax.transAxes, fontsize=8, color='#555555')
    fig.autofmt_xdate()
    fig.tight_layout()
    CHART.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(CHART, dpi=165)
    plt.close(fig)


def push(title, message, key):
    if not key:
        raise ValueError('缺少 SERVERCHAN_SENDKEY；只生成报告请显式使用 --no-push')
    if re.fullmatch(r'SCT[0-9A-Za-z]+', key):
        url = f'https://sctapi.ftqq.com/{key}.send'
    else:
        match = re.fullmatch(r'sctp(\d+)t[0-9A-Za-z]+', key)
        if not match:
            raise ValueError('SendKey 格式无法识别')
        url = f'https://{match.group(1)}.push.ft07.com/send/{key}.send'
    request = Request(url, data=urlencode({'title': title, 'desp': message}).encode('utf-8'),
                      headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read(100_000))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        # urllib exceptions may contain the whole URL, including SendKey. Never log them.
        raise RuntimeError(f'Server酱请求失败（{type(exc).__name__}），到控制台核查是否已接收后再补发') from None
    if result.get('code') != 0:
        raise RuntimeError(f'Server酱拒绝请求，code={result.get("code")}，请在控制台核查')
    return result


def publish_url(base_url):
    from urllib.parse import urlsplit
    if not base_url:
        return ''
    parsed = urlsplit(base_url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('REPORT_BASE_URL 必须是不含账号、查询参数或锚点的 HTTPS 目录地址')
    return base_url.rstrip('/') + '/'


def notification_owner():
    run_id = os.getenv('GITHUB_RUN_ID','')
    attempt = os.getenv('GITHUB_RUN_ATTEMPT','')
    return f'{run_id}:{attempt}' if run_id and attempt else ''


def notify(date, message, key, retry=False, title=None, reserve_only=False, use_reservation=False):
    if not key:
        raise ValueError('缺少 SERVERCHAN_SENDKEY')
    state = json.loads(PUSH_STATE.read_text(encoding='utf-8')) if PUSH_STATE.exists() else {}
    old = state.get(date)
    if old and old['status'] == 'submitted' and not retry:
        print(f'{date} 日报已提交，跳过重复推送')
        return
    owner = notification_owner()
    if reserve_only:
        if not owner:
            raise ValueError('发送预留仅用于有运行标识的GitHub任务')
        if old and old['status'] in ('uncertain','reserved') and not retry:
            raise RuntimeError('上次发送预留或结果待核实，未自动重发；核对后显式重试')
        state[date] = dict(status='reserved',owner=owner,at=dt.datetime.now(TZ).isoformat(timespec='seconds'))
        atomic_json(PUSH_STATE,state)
        print('发送预留已写入；云端必须成功写回仓库后才能调用推送')
        return
    if use_reservation:
        if not owner or not old or old['status'] != 'reserved' or old.get('owner') != owner:
            raise RuntimeError('缺少属于本次运行的发送预留，拒绝推送')
    elif old and old['status'] in ('uncertain','reserved') and not retry:
        raise RuntimeError('上次推送结果待核实，未自动重发；核对微信和控制台后可使用 --retry-push')
    state[date] = dict(status='uncertain',owner=owner,at=dt.datetime.now(TZ).isoformat(timespec='seconds'))
    atomic_json(PUSH_STATE,state)
    result = push(title or f'IC/IM 贴水日报 {date}', message, key)
    state[date] = dict(status='submitted',at=dt.datetime.now(TZ).isoformat(timespec='seconds'),
                       message_sha256=hashlib.sha256(message.encode()).hexdigest())
    atomic_json(PUSH_STATE,state)
    print('日报已提交 Server酱；手机接收状态请在微信确认')
    return result


def cache_quotes(date, quotes):
    atomic_json(QUOTES / f'{date.isoformat()}.json', {'date': date.isoformat(), 'quotes': quotes, 'schema': 1})


def load_quotes(date):
    path = QUOTES / f'{date.isoformat()}.json'
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding='utf-8'))
    if payload.get('date') != date.isoformat():
        raise ValueError('缓存日期不匹配')
    return payload['quotes']


def render_reports(history, requested_date, failures):
    from report import write_report
    if not history:
        raise RuntimeError('尚无有效历史数据，无法生成报告')
    dates = sorted({r['date'] for r in history})
    latest = dates[-1]
    display_history=reference_view(history)
    rows = [r for r in display_history if r['date'] == latest]
    if {r['product'] for r in rows} != {'IC','IM'}:
        raise ValueError('缺少文章对照口径，请用行情缓存补录重算')
    quote_history = {}
    for day in dates:
        quotes = load_quotes(dt.date.fromisoformat(day))
        if quotes is not None:
            quote_history[day] = quotes
    text = (f'交易日：{latest}；期货盘后收盘价，非实时行情。\n\n'
            '新手学习与模拟模式｜规则解说，未调用 AI API。\n\n'
            '口径：挂牌最远两个连续季月，作为文章对照；以下始终注明合约。\n\n' +
            '\n\n'.join(describe(r, display_history, quote_history) for r in rows))
    text += '\n\n近端季月辅助：'+'；'.join(
        f'{r["product"]} {r["near"]}/{r["far"]} {float(r["annualized_pct"]):+.2f}%'
        for r in history if r['date']==latest)
    all_days = [dt.date.fromisoformat(dates[0]) + dt.timedelta(days=n)
                for n in range((dt.date.fromisoformat(latest)-dt.date.fromisoformat(dates[0])).days+1)]
    missing = [d.isoformat() for d in all_days if is_trading_day(d) and d.isoformat() not in dates]
    text += (f'\n\n历史覆盖：{dates[0]} 至 {latest}，{len(dates)} 个交易日；区间内缺失 {len(missing)} 日。'
             '\n\n公式：（远季/近季−1）×4。折价更深不等于账户获利，跨季指标不等于实际换月价差。'
             '同组与滚动分位分别标注，至少20个此前样本才显示，滚动分位不作为买卖触发器。'
             '80%/20%为同组未回测观察阈值。本报告用于学习模拟，未配置个人持仓。')
    if failures:
        text += '\n\n数据提示：部分请求失败，详细日期见报告。'
    if any('http://' in q.get('source_url', '') for r in rows
           for q in (load_quotes(dt.date.fromisoformat(r['date'])) or [])):
        text += '\n\n来源：中金所官方域名，本次使用 HTTP 回退。'
    url = publish_url(os.getenv('REPORT_BASE_URL', '').strip())
    if url:
        text += f'\n\n[查看一年走势图与完整报告]({url}?date={latest})\n\n![IC/IM走势图]({url}quarter_spread.png?date={latest})'
    if latest != requested_date.isoformat():
        text = f'截至 {latest} 的历史报告；请求日期 {requested_date} 没有新的完整行情。\n\n' + text
    make_chart(display_history)
    write_report(history, text, missing, failures, requested_date, BASE / 'output',
                 load_quotes(dt.date.fromisoformat(latest)), quote_history=quote_history)
    (BASE / 'output' / 'notification.md').write_text(text, encoding='utf-8')
    return latest, text


def run(args):
    today = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(TZ).date()
    if getattr(args, 'demo', False):
        quotes = [dict(symbol=p+s, close=v, volume=1000, oi=2000) for p in ('IC','IM')
                  for s,v in [('2611',6000),('2612',5950),('2703',5800)]]
        rows = [compute(dt.date(2026,10,21),quotes,p) for p in ('IC','IM')]
        print('【虚构演示，不写数据、不推送】\n'+'\n\n'.join(describe(r,rows) for r in rows))
        return 0
    backfill_only = getattr(args, 'backfill_only', False)
    no_push = args.no_push or backfill_only
    key = os.getenv('SERVERCHAN_SENDKEY', '').strip()
    if not no_push and today != dt.datetime.now(TZ).date():
        raise ValueError('历史日期只能使用 --no-push 或 --backfill-only，防止误发为今日行情')
    existing = read_history()
    days = args.backfill_days if args.backfill_days else (0 if args.date else 14)
    if days < 0 or days > 1500:
        raise ValueError('--backfill-days 必须介于 0 与 1500 之间')
    first = today - dt.timedelta(days=days)
    failures = []
    successful = []
    unknown_years = sorted({d.year for d in (first,today)}-COVERED_YEARS)
    if unknown_years:
        print(f'注意：{unknown_years} 年日历未核验，未知日期不伪称正式交易日')
    for offset in range((today-first).days+1):
        date = first+dt.timedelta(days=offset)
        if not is_trading_day(date):
            continue
        cached = None
        try:
            cached = None if getattr(args,'refresh',False) else load_quotes(date)
            quotes = cached if cached is not None else get_daily(date)
            rows = [compute(date,quotes,p) for p in ('IC','IM')]
            # 每一天独立保存：今天无数据、后续网络失败或推送异常均不丢已获得历史。
            if cached is None:
                cache_quotes(date,quotes)
            existing = write_history(existing,rows)
            successful.append(date.isoformat())
            print(f'{date} IC/IM 已保存'+('（缓存）' if cached is not None else ''),flush=True)
        except (RuntimeError, ValueError, ET.ParseError, KeyError, OSError) as exc:
            failures.append({'date':date.isoformat(),'error':str(exc)[:180]})
            print(f'{date} 未取得完整行情：{str(exc)[:180]}',file=sys.stderr,flush=True)
        if days and cached is None:
            time.sleep(.35)
    atomic_json(STATUS, dict(requested_date=today.isoformat(), start=first.isoformat(),
                            successful=successful, failures=failures, calendar_unknown_years=unknown_years))
    if not existing:
        raise RuntimeError('没有有效历史数据；已保存诊断，请检查官方行情访问情况')
    latest, message = render_reports(existing,today,failures)
    print(message)
    current_ok = today.isoformat() in successful
    if backfill_only:
        return 2 if failures else 0
    if not is_trading_day(today):
        print('请求日休市，历史已保存，不发送旧数据')
        return 2 if failures else 0
    if not current_ok:
        raise RuntimeError('请求日缺少完整IC/IM行情：历史已保存，拒绝推送旧值')
    if not no_push:
        if not key:
            raise ValueError('行情已保存，但缺少 SERVERCHAN_SENDKEY；不发送时请使用 --no-push')
        notify(latest,message,key,retry=getattr(args,'retry_push',False))
    return 0


def send_existing_report(retry=False, reserve_only=False, use_reservation=False):
    date = dt.datetime.now(TZ).date()
    if not is_trading_day(date):
        print('今日休市，不发送历史日报')
        return 0
    history = read_history()
    today_rows = [r for r in history if r['date'] == date.isoformat()]
    if {r['product'] for r in today_rows} != {'IC','IM'}:
        raise RuntimeError('今天没有完整行情，拒绝发送历史报告')
    path = BASE / 'output' / 'notification.md'
    message = path.read_text(encoding='utf-8')
    if not message.startswith(f'交易日：{date.isoformat()}；'):
        raise RuntimeError('消息日期与今日不匹配，拒绝发送')
    key = os.getenv('SERVERCHAN_SENDKEY','').strip()
    if not key:
        raise ValueError('缺少 SERVERCHAN_SENDKEY')
    notify(date.isoformat(),message,key,retry=retry,reserve_only=reserve_only,use_reservation=use_reservation)
    return 0


def send_failure_notice(retry=False, reserve_only=False, use_reservation=False):
    date = dt.datetime.now(TZ).date().isoformat()
    key = os.getenv('SERVERCHAN_SENDKEY','').strip()
    if not key:
        raise ValueError('缺少 SERVERCHAN_SENDKEY，无法发异常提醒')
    run_url = os.getenv('MONITOR_RUN_URL','').strip()
    suffix = f'\n\n[查看本次运行日志]({run_url})' if run_url.startswith('https://github.com/') else ''
    message = '本次 IC/IM 日报流程未完整完成，可能涉及行情、报告发布或推送。请检查运行日志与微信接收状态；不要把旧报告当作今日更新。'+suffix
    notify('error:'+date,message,key,title=f'IC/IM 监控异常 {date}',retry=retry,
           reserve_only=reserve_only,use_reservation=use_reservation)
    return 0


def send_test_notice(retry=False, reserve_only=False, use_reservation=False):
    date=dt.datetime.now(TZ).date().isoformat()
    key=os.getenv('SERVERCHAN_SENDKEY','').strip()
    message='IC/IM 监控接入测试成功。此消息用于确认程序已提交微信推送，不含交易指令。每日行情将在工作日18:37后按交易日更新。'
    url=publish_url(os.getenv('REPORT_BASE_URL','').strip())
    if url:
        message+=f'\n\n[查看行情报告]({url})'
    notify('test:'+date,message,key,retry=retry,title='IC/IM 监控接入测试',
           reserve_only=reserve_only,use_reservation=use_reservation)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', help='指定日期 YYYY-MM-DD，历史日期禁止推送')
    parser.add_argument('--backfill-days', type=int, default=0, help='补录自然日数；日常运行自动核查近14天')
    parser.add_argument('--backfill-only', action='store_true', help='只补历史与出报告，结束日无需有行情，绝不推送')
    parser.add_argument('--no-push', action='store_true', help='下载、保存并生成报告，不发送')
    parser.add_argument('--refresh', action='store_true', help='重新请求所选区间，不使用已有缓存')
    parser.add_argument('--retry-push', action='store_true', help='核实上次发送状态后，显式允许重发当日日报')
    parser.add_argument('--demo', action='store_true', help='离线虚构演示，不写数据、不发送')
    parser.add_argument('--reserve-only', action='store_true', help='云端先写发送预留，不调用API')
    parser.add_argument('--use-reservation', action='store_true', help='云端只能使用属于本次运行的预留')
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--test-notice', action='store_true', help='只发送接入测试，不占用当日日报去重记录')
    modes.add_argument('--notify-only', action='store_true', help='只发送已生成且日期为今日的报告，不采集行情')
    modes.add_argument('--failure-notice', action='store_true', help='只发送一次当日运行失败提醒')
    args=parser.parse_args()
    try:
        if args.no_push and (args.notify_only or args.failure_notice or args.test_notice):
            raise ValueError('--no-push 不能与发送专用模式同时使用')
        if (args.reserve_only or args.use_reservation) and not (args.notify_only or args.failure_notice or args.test_notice):
            raise ValueError('发送预留参数仅供通知专用模式使用')
        if args.reserve_only and args.use_reservation:
            raise ValueError('不能同时创建和消费发送预留')
        if args.test_notice:
            return send_test_notice(retry=args.retry_push,reserve_only=args.reserve_only,use_reservation=args.use_reservation)
        if args.notify_only:
            return send_existing_report(retry=args.retry_push,reserve_only=args.reserve_only,use_reservation=args.use_reservation)
        if args.failure_notice:
            return send_failure_notice(retry=args.retry_push,reserve_only=args.reserve_only,use_reservation=args.use_reservation)
        return run(args)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f'失败：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
