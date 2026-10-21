import datetime as dt
import unittest
import types
import io
import contextlib
from unittest.mock import patch
import monitor
from monitor import compute, month_expiry, describe, parse_daily_xml

class TestMath(unittest.TestCase):
    def test_quarter_price(self):
        rows = [
            dict(symbol='IC2610', close=6010, volume=1000, oi=2000),
            dict(symbol='IC2611', close=5990, volume=1000, oi=2000),
            dict(symbol='IC2612', close=6000, volume=10000, oi=20000),
            dict(symbol='IC2703', close=5850, volume=10000, oi=20000),
        ]
        r=compute(dt.date(2026,10,21), rows, 'IC')
        self.assertEqual(r['near'], 'IC2612')
        self.assertEqual(r['far'], 'IC2703')
        self.assertEqual(r['front'], 'IC2611')
        self.assertEqual(r['next'], 'IC2612')
        self.assertAlmostEqual(r['annualized_pct'], -10)
        self.assertAlmostEqual(r['spread_points'], 150)
        self.assertEqual(month_expiry('IC2612'), dt.date(2026,12,18))
    def test_parse_official_shape(self):
        body = '<root>' + ''.join(
            '<dailydata><instrumentid>{}</instrumentid><tradingday>20261021</tradingday><closeprice>{}</closeprice><volume>10</volume><openinterest>15</openinterest></dailydata>'.format(k,v)
            for k,v in [('IC2612','6000'),('IC2703','5850'),('IM2612','7000'),('IM2703','6800')]
        ) + '</root>'
        q = parse_daily_xml(body.encode('utf8'), dt.date(2026,10,21))
        self.assertEqual(len(q),4)
        self.assertEqual(compute(dt.date(2026,10,21),q,'IC')['annualized_pct'],-10)
    def test_reject_stale(self):
        body = b'<root><dailydata><instrumentid>IC2612</instrumentid><tradingday>20261020</tradingday></dailydata></root>'
        with self.assertRaisesRegex(ValueError,'交易日不一致'):
            parse_daily_xml(body,dt.date(2026,10,21))
    def test_late_quarter(self):
        rows = [dict(symbol=k,close=p,volume=200,oi=300) for k,p in
                [('IM2701', 6000),('IM2703',5900),('IM2706',5800)]]
        r=compute(dt.date(2027,1,5),rows,'IM')
        self.assertEqual((r['near'],r['far']), ('IM2703','IM2706'))

class TestRegressions(unittest.TestCase):
    def test_delivery_day_keeps_expiring_contract(self):
        q = [dict(symbol=k, close=v, volume=1000, oi=2000) for k,v in
             [('IC2612',6000),('IC2701',5950),('IC2703',5850),('IC2706',5700)]]
        r=compute(dt.date(2026,12,18),q,'IC')
        self.assertEqual(r['near'], 'IC2612')
        self.assertEqual(r['front'], 'IC2612')

    def test_gap_has_actual_comparison_date(self):
        q = [dict(symbol=k, close=v, volume=1000, oi=2000) for k,v in
             [('IC2611',6000),('IC2612',5950),('IC2703',5800)]]
        old=compute(dt.date(2026,10,12),q,'IC')
        current=compute(dt.date(2026,10,20),q,'IC')
        text=describe(current,[old,current])
        self.assertIn('2026-10-12',text)
        self.assertNotIn('较上一交易日',text)

    def test_rollover_does_not_claim_same_pair_delta(self):
        q = [dict(symbol=k, close=v, volume=1000, oi=2000) for k,v in
             [('IC2609',6000),('IC2610',5990),('IC2612',5850),('IC2703',5810)]]
        old=compute(dt.date(2026,9,17),q,'IC')
        q=q[1:]+[dict(symbol='IC2706',close=5750,volume=1000,oi=2000)]
        current=compute(dt.date(2026,9,21),q,'IC')
        text=describe(current,[old,current])
        self.assertIn('切换',text)
        self.assertNotIn('较上一交易日',text)

class TestPersistenceAndNotifications(unittest.TestCase):
    def setUp(self):
        self.quotes = [dict(symbol=p+s,close=v,volume=1000,oi=2000)
                       for p in ('IC','IM') for s,v in [('2611',6000),('2612',5950),('2703',5800)]]

    def args(self, **changes):
        values=dict(date='2026-10-18',demo=False,backfill_days=7,no_push=True,
                    backfill_only=True,refresh=False,retry_push=False)
        values.update(changes)
        return types.SimpleNamespace(**values)

    def test_weekend_backfill_persists_successful_days(self):
        saved=[]
        def save(old,new):
            saved.extend(new)
            return old+new
        with patch.object(monitor,'read_history',return_value=[]), \
             patch.object(monitor,'load_quotes',return_value=None), \
             patch.object(monitor,'get_daily',return_value=self.quotes), \
             patch.object(monitor,'cache_quotes'), patch.object(monitor,'write_history',side_effect=save), \
             patch.object(monitor,'atomic_json'), patch.object(monitor.time,'sleep'), \
             patch.object(monitor,'render_reports',return_value=('2026-10-16','report')), \
             patch.object(monitor,'notify') as notify, contextlib.redirect_stdout(io.StringIO()):
            result=monitor.run(self.args())
        self.assertEqual(result,0)
        self.assertEqual(len(saved),10)
        self.assertEqual({r['product'] for r in saved},{'IC','IM'})
        notify.assert_not_called()

    def test_partial_product_failure_does_not_write_day(self):
        only_ic=[q for q in self.quotes if q['symbol'].startswith('IC')]
        with patch.object(monitor,'read_history',return_value=[]), \
             patch.object(monitor,'load_quotes',return_value=None), \
             patch.object(monitor,'get_daily',return_value=only_ic), \
             patch.object(monitor,'cache_quotes') as cache, \
             patch.object(monitor,'write_history') as write, patch.object(monitor,'atomic_json'), \
             contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'没有有效历史'):
                monitor.run(self.args(date='2026-10-20',backfill_days=0))
        cache.assert_not_called()
        write.assert_not_called()

    def test_missing_sendkey_is_explicit_error(self):
        with self.assertRaisesRegex(ValueError,'缺少'):
            monitor.push('title','body','')

    def test_notification_error_never_contains_key(self):
        key='SCT000SyntheticCredential'
        error=monitor.URLError('https://sctapi.ftqq.com/'+key+'.send')
        with patch.object(monitor,'urlopen',side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                monitor.push('test','body',key)
        self.assertNotIn(key,str(raised.exception))
        self.assertIn('URLError',str(raised.exception))

    def test_duplicate_push_is_suppressed(self):
        with patch.object(monitor.PUSH_STATE.__class__,'exists',return_value=True), \
             patch.object(monitor.PUSH_STATE.__class__,'read_text',return_value='{"2026-10-21":{"status":"submitted"}}'), \
             patch.object(monitor,'push') as push,contextlib.redirect_stdout(io.StringIO()):
            monitor.notify('2026-10-21','body','SCT000SyntheticCredential')
        push.assert_not_called()

    def test_uncertain_push_requires_explicit_retry(self):
        with patch.object(monitor.PUSH_STATE.__class__,'exists',return_value=True), \
             patch.object(monitor.PUSH_STATE.__class__,'read_text',return_value='{"2026-10-21":{"status":"uncertain"}}'), \
             patch.object(monitor,'push') as push:
            with self.assertRaisesRegex(RuntimeError,'未自动重发'):
                monitor.notify('2026-10-21','body','SCT000SyntheticCredential')
        push.assert_not_called()

    def test_failed_push_retains_uncertain_state(self):
        captured=[]
        with patch.object(monitor.PUSH_STATE.__class__,'exists',return_value=False), \
             patch.object(monitor,'atomic_json',side_effect=lambda path,state:captured.append(__import__('copy').deepcopy(state))), \
             patch.object(monitor,'push',side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                monitor.notify('2026-10-21','body','SCT000SyntheticCredential')
        self.assertEqual(captured[-1]['2026-10-21']['status'],'uncertain')


class TestCalendarAndStatistics(unittest.TestCase):
    def test_holiday_delivery_postponed(self):
        self.assertEqual(month_expiry('IC2606'),dt.date(2026,6,22))
        self.assertFalse(monitor.is_trading_day(dt.date(2026,6,19)))
        self.assertFalse(monitor.is_trading_day(dt.date(2026,10,10)))
        self.assertTrue(monitor.is_trading_day(dt.date(2026,10,8)))

    def test_gap_and_roll_break_the_chart(self):
        base=dict(product='IC',near='IC2612',far='IC2703',annualized_pct=-10)
        rows=[dict(base,date='2026-10-12'),dict(base,date='2026-10-14'),
              dict(base,date='2026-10-15',near='IC2703',far='IC2706')]
        result=monitor.series_for(rows,'IC')
        self.assertFalse(result[0]['break_before'])
        self.assertTrue(result[1]['break_before'])
        self.assertTrue(result[2]['roll'])

    def test_percentile_excludes_current_and_future(self):
        base=dict(product='IC',near='IC2612',far='IC2703',annualized_pct=-5)
        rows=[dict(base,date=(dt.date(2026,8,1)+dt.timedelta(days=i)).isoformat()) for i in range(20)]
        current=dict(base,date='2026-10-20',annualized_pct=-10)
        stats=monitor.statistics(current,rows+[current,dict(current,date='2026-10-21')])
        self.assertEqual(len(stats['same']),20)
        self.assertEqual(stats['percentile'],100)

    def test_bad_report_url_is_rejected(self):
        for url in ('http://example.com','https://user:pass@example.com','https://example.com/?token=secret'):
            with self.assertRaises(ValueError):monitor.publish_url(url)
        self.assertEqual(monitor.publish_url('https://example.com/report'),'https://example.com/report/')

class TestReferenceBasisAndReservations(unittest.TestCase):
    def test_two_bases_use_different_explicit_pairs(self):
        quotes=[dict(symbol=k,close=v,volume=1000,oi=2000) for k,v in
                [('IC2609',6000),('IC2610',5990),('IC2612',5850),('IC2703',5700)]]
        row=compute(dt.date(2026,9,11),quotes,'IC')
        self.assertEqual((row['near'],row['far']),('IC2609','IC2612'))
        self.assertEqual((row['reference_near'],row['reference_far']),('IC2612','IC2703'))
        view=monitor.reference_view([row])[0]
        self.assertEqual(view['near'],'IC2612')
        self.assertAlmostEqual(view['annualized_pct'],round((5700/5850-1)*400,4))
        self.assertNotEqual(view['annualized_pct'],row['annualized_pct'])

    def test_old_rows_are_not_relabelled_as_reference(self):
        self.assertEqual(monitor.reference_view([dict(date='2026-10-20',product='IC',near='IC2612')]),[])

    def test_older_missing_day_does_not_block_complete_today(self):
        saved=[]
        quotes=[dict(symbol=p+s,close=v,volume=1000,oi=2000) for p in ('IC','IM')
                for s,v in [('2611',6000),('2612',5950),('2703',5800)]]
        args=types.SimpleNamespace(date='2026-10-20',backfill_days=1,no_push=True,
                                   backfill_only=False,demo=False,refresh=False)
        def fetch(date):
            if date==dt.date(2026,10,19):raise RuntimeError('synthetic missing history')
            return quotes
        with patch.object(monitor,'read_history',return_value=[]),patch.object(monitor,'load_quotes',return_value=None), \
             patch.object(monitor,'get_daily',side_effect=fetch),patch.object(monitor,'cache_quotes'), \
             patch.object(monitor,'write_history',side_effect=lambda old,new:saved.extend(new) or old+new), \
             patch.object(monitor,'atomic_json'),patch.object(monitor.time,'sleep'), \
             patch.object(monitor,'render_reports',return_value=('2026-10-20','report')), \
             contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            result=monitor.run(args)
        self.assertEqual(result,0)
        self.assertEqual(len(saved),2)

    def test_reservation_survives_a_new_run_and_blocks_duplicates(self):
        state={}
        def write(path,new):
            state.clear();state.update(__import__('copy').deepcopy(new))
        with patch.object(monitor.PUSH_STATE.__class__,'exists',return_value=True), \
             patch.object(monitor.PUSH_STATE.__class__,'read_text',side_effect=lambda **kwargs:monitor.json.dumps(state)), \
             patch.object(monitor,'atomic_json',side_effect=write), \
             patch.object(monitor,'push') as push,patch.dict(monitor.os.environ,{'GITHUB_RUN_ID':'10','GITHUB_RUN_ATTEMPT':'1'}), \
             contextlib.redirect_stdout(io.StringIO()):
            monitor.notify('2026-10-21','body','SCT000SyntheticCredential',reserve_only=True)
            self.assertEqual(state['2026-10-21']['status'],'reserved')
            push.assert_not_called()
            with patch.dict(monitor.os.environ,{'GITHUB_RUN_ATTEMPT':'2'}):
                with self.assertRaisesRegex(RuntimeError,'属于本次运行'):
                    monitor.notify('2026-10-21','body','SCT000SyntheticCredential',use_reservation=True)
            push.assert_not_called()
            monitor.notify('2026-10-21','body','SCT000SyntheticCredential',use_reservation=True)
            push.assert_called_once()
            self.assertEqual(state['2026-10-21']['status'],'submitted')
            monitor.notify('2026-10-21','body','SCT000SyntheticCredential')
            push.assert_called_once()

if __name__ == '__main__':
    unittest.main()
