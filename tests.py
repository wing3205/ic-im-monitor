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
    def test_pair_samples_include_quotes_before_pair_was_selected(self):
        base=dict(product='IC',near='IC2612',far='IC2703',annualized_pct=-10)
        current=dict(base,date='2026-10-21')
        snapshots={(dt.date(2026,7,20)+dt.timedelta(days=i)).isoformat():[
            dict(symbol='IC2612',close=6000,volume=10),
            dict(symbol='IC2703',close=5925,volume=10)] for i in range(25)}
        # Current and future quotes must never enter the historical denominator.
        snapshots['2026-10-21']=[dict(symbol='IC2612',close=6000,volume=10),
                                 dict(symbol='IC2703',close=5000,volume=10)]
        snapshots['2026-10-22']=snapshots['2026-10-21']
        selected=[dict(base,date='2026-07-20',annualized_pct=-30)]
        s=monitor.statistics(current,selected+[current],snapshots)
        self.assertEqual(len(s['same']),25)
        self.assertEqual(s['percentile'],100)
        self.assertEqual(s['same'][0]['annualized_pct'],-5)

    def test_rolling_window_is_separate_from_exact_pair(self):
        base=dict(product='IC',near='IC2609',far='IC2612',annualized_pct=-5)
        current=dict(base,date='2026-10-21',near='IC2703',far='IC2706',annualized_pct=-10)
        rows=[dict(base,date=(dt.date(2025,10,21)+dt.timedelta(days=i)).isoformat(),
                   annualized_pct=-5 if i<10 else -15) for i in range(20)]
        rows += [dict(base,date='2025-10-20'),current,dict(current,date='2026-10-22'),
                 dict(base,date='2026-10-20',product='IM')]
        s=monitor.statistics(current,rows,{})
        self.assertEqual(len(s['same']),0)
        self.assertIsNone(s['percentile'])
        self.assertEqual(s['rolling']['count'],20)
        self.assertEqual(s['rolling']['percentile'],50)
        self.assertEqual(s['rolling']['start'],'2025-10-21')

    def test_invalid_or_ambiguous_pair_quotes_do_not_add_samples(self):
        r=dict(product='IC',near='IC2612',far='IC2703',annualized_pct=-10,date='2026-10-21')
        good=[dict(symbol='IC2612',close=6000,volume=10),dict(symbol='IC2703',close=5925,volume=10)]
        snapshots={'2026-10-16':good,'2026-10-19':good+[good[0]],
                   '2026-10-20':[good[0],dict(good[1],volume=0)],
                   '2026-10-15':[good[0],dict(good[1],close=float('nan'))]}
        s=monitor.statistics(r,[],snapshots)
        self.assertEqual([x['date'] for x in s['same']],['2026-10-16'])
        self.assertIsNone(s['percentile'])

    def test_beginner_explanation_uses_rules_and_handles_premium(self):
        q=[dict(symbol=k,close=v,volume=1000,oi=2000) for k,v in
           [('IC2611',6000),('IC2612',5950),('IC2703',6000)]]
        r=compute(dt.date(2026,10,21),q,'IC')
        text='\n'.join(x['text'] for x in monitor.learning_notes(r,[r]))
        self.assertIn('远季价格高于近季',text)
        self.assertIn('模拟',text)
        self.assertIn('20,000',text)
        self.assertIn('不能',text)

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

class TestReportDateAndEmail(unittest.TestCase):
    def test_report_date_tracks_latest_evening_session(self):
        cases=[((2026,10,26,18,37),'2026-10-26'),((2026,10,27,0,56),'2026-10-26'),
               ((2026,10,27,17,59),'2026-10-26'),((2026,10,27,18,0),'2026-10-27'),
               ((2026,10,27,10,30),'2026-10-26'),((2026,10,24,1,0),'2026-10-23'),
               ((2026,10,26,10,0),'2026-10-23'),((2026,10,5,19,0),'2026-09-30')]
        for parts,want in cases:
            with self.subTest(parts=parts):
                self.assertEqual(monitor.report_date(dt.datetime(*parts,tzinfo=monitor.TZ)).isoformat(),want)

    def test_slow_generation_rechecks_date_before_sending(self):
        quotes=[dict(symbol=p+s,close=v,volume=1000,oi=2000) for p in ('IC','IM')
                for s,v in [('2611',6000),('2612',5950),('2703',5800)]]
        args=types.SimpleNamespace(date=None,backfill_days=0,no_push=False,backfill_only=False,
                                   demo=False,refresh=False,channel='email')
        with patch.object(monitor,'report_date',side_effect=[dt.date(2026,10,26),dt.date(2026,10,26),dt.date(2026,10,27)]), \
             patch.object(monitor,'read_history',return_value=[]),patch.object(monitor,'load_quotes',return_value=quotes), \
             patch.object(monitor,'write_history',side_effect=lambda old,new:old+new),patch.object(monitor,'atomic_json'), \
             patch.object(monitor,'render_reports',return_value=('2026-10-26','report')), \
             patch.dict(monitor.os.environ,{'SMTP_PASSWORD':'synthetic-password'}), \
             patch.object(monitor,'notify') as notify,contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'交易日'):
                monitor.run(args)
            notify.assert_not_called()

    def test_midnight_delayed_generation_does_not_request_future_close(self):
        now=dt.datetime(2026,10,27,0,56,tzinfo=monitor.TZ)
        quotes=[dict(symbol=p+s,close=v,volume=1000,oi=2000) for p in ('IC','IM')
                for s,v in [('2611',6000),('2612',5950),('2703',5800)]]
        args=types.SimpleNamespace(date=None,backfill_days=0,no_push=True,backfill_only=False,
                                   demo=False,refresh=False)
        def fetch(date):
            if date>dt.date(2026,10,26):raise RuntimeError('close not available yet')
            return quotes
        with patch.object(monitor.dt,'datetime') as clock,patch.object(monitor,'read_history',return_value=[]), \
             patch.object(monitor,'load_quotes',return_value=None),patch.object(monitor,'get_daily',side_effect=fetch) as source, \
             patch.object(monitor,'cache_quotes'),patch.object(monitor,'write_history',side_effect=lambda old,new:old+new), \
             patch.object(monitor,'atomic_json'),patch.object(monitor.time,'sleep'), \
             patch.object(monitor,'render_reports',return_value=('2026-10-26','report')), \
             contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            clock.now.return_value=now
            self.assertEqual(monitor.run(args),0)
            self.assertEqual(max(call.args[0] for call in source.call_args_list),dt.date(2026,10,26))

    def test_delayed_notification_accepts_target_and_rejects_stale_override(self):
        history=[dict(date='2026-10-26',product=p) for p in ('IC','IM')]
        env={'REPORT_DATE':'2026-10-26','SERVERCHAN_SENDKEY':'SCT000SyntheticCredential'}
        with patch.object(monitor,'report_date',return_value=dt.date(2026,10,26)), \
             patch.object(monitor,'read_history',return_value=history), \
             patch.object(monitor.Path,'read_text',return_value='交易日：2026-10-26；test'), \
             patch.dict(monitor.os.environ,env),patch.object(monitor,'notify') as notify:
            monitor.send_existing_report()
            self.assertEqual(notify.call_args.args[0],'2026-10-26')
            for invalid in ('2026-10-23','2026-10-28'):
                with patch.dict(monitor.os.environ,{'REPORT_DATE':invalid}):
                    with self.assertRaisesRegex(ValueError,'交易日'):
                        monitor.send_existing_report()
            self.assertEqual(notify.call_count,1)

    def test_notification_rechecks_after_loading_report(self):
        env={'REPORT_DATE':'2026-10-26','SERVERCHAN_SENDKEY':'SCT000SyntheticCredential'}
        with patch.object(monitor,'report_date',side_effect=[dt.date(2026,10,26),dt.date(2026,10,27)]), \
             patch.object(monitor,'read_history',return_value=[dict(date='2026-10-26',product=p) for p in ('IC','IM')]), \
             patch.object(monitor.Path,'read_text',return_value='交易日：2026-10-26；test'), \
             patch.dict(monitor.os.environ,env),patch.object(monitor,'notify') as notify:
            with self.assertRaisesRegex((ValueError,RuntimeError),'交易日'):
                monitor.send_existing_report()
            notify.assert_not_called()

    def test_wechat_and_email_dedup_are_independent(self):
        state={'2026-10-26':{'status':'submitted'}}
        def save(path,new):
            state.clear();state.update(__import__('copy').deepcopy(new))
        with patch.object(monitor.Path,'exists',return_value=True), \
             patch.object(monitor.Path,'read_text',side_effect=lambda **kwargs:monitor.json.dumps(state)), \
             patch.object(monitor,'atomic_json',side_effect=save),patch.object(monitor,'push_email') as email, \
             patch.dict(monitor.os.environ,{'SMTP_USERNAME':'sender@qq.com','MAIL_TO':'reader@qq.com'}), \
             contextlib.redirect_stdout(io.StringIO()):
            monitor.notify('2026-10-26','body','synthetic-password',channel='email')
            monitor.notify('2026-10-26','body','synthetic-password',channel='email')
            email.assert_called_once()
            self.assertEqual(state['email:2026-10-26']['status'],'submitted')
            self.assertEqual(state['2026-10-26']['status'],'submitted')

    def test_email_contains_plaintext_safe_html_and_inline_chart(self):
        message=monitor.build_email('IC/IM 测试','交易日：2026-10-26；\n### IC\n<script>bad</script>',
                                    'sender@qq.com','reader@qq.com',chart=b'fake png bytes')
        self.assertIn('2026-10-26',message.get_body(preferencelist=('plain',)).get_content())
        html=message.get_body(preferencelist=('html',)).get_content()
        self.assertIn('&lt;script&gt;',html)
        self.assertNotIn('<script>',html)
        self.assertIn('cid:quarter-spread',html)
        self.assertTrue(any(part.get_content_type()=='image/png' for part in message.walk()))

    def test_smtp_errors_hide_credentials_and_recipient(self):
        password='synthetic-secret-value'
        with patch.dict(monitor.os.environ,{'SMTP_USERNAME':'sender@qq.com','MAIL_TO':'reader@qq.com'}), \
             patch.object(monitor.smtplib,'SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.login.side_effect=monitor.smtplib.SMTPAuthenticationError(
                535,(password+' sender@qq.com reader@qq.com').encode())
            with self.assertRaises(RuntimeError) as raised:
                monitor.push_email('test','body',password)
            self.assertIn('SMTPAuthenticationError',str(raised.exception))
            for secret in [password,'sender@qq.com','reader@qq.com']:
                self.assertNotIn(secret,str(raised.exception))

if __name__ == '__main__':
    unittest.main()
