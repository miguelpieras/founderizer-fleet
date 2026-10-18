"""Read-only checks of a recorded pilot case (PILOT.md), as `fleet.py verify-case`. Missing evidence is a
failure. The health check of a machine is another thing entirely and lives in verify.py (PRODUCT.md 4.1)."""
import hashlib
import json
import pathlib
import re
import subprocess


def verify(records, kit, case_id=None, run=None, emit=print):
    checks = []
    def check(label, ok, detail):
        checks.append(bool(ok))
        emit(f"{'PASS' if ok else 'FAIL'} {label}: {detail}")
    cases = [c for c in records['cases'] if (not case_id or c['id'] == case_id) and (not run or c['id'].startswith(run+'-'))]
    check('case selection', bool(cases), ', '.join(c['id'] for c in cases) or 'no cases')
    if not run and not case_id:
        groups={}
        for c in cases:
            groups.setdefault(c['id'].rsplit('-',1)[0],set()).add(c['test'])
        full=[name for name, tests in groups.items() if set(range(1,15))<=tests]
        check('two complete runs', len(full)>=2, ', '.join(full) or 'none yet')
        for n in (2,9,10):
            count=sum(c['test']==n for c in cases)
            check(f'test {n}: two runs plus three repeats', count>=5, f'{count}/5 recorded cases')
        recovery=next((json.loads(e['data']) for e in reversed(records['evidence']) if e['kind']=='service_recovery'),{})
        v,s,h=(recovery.get(k,{}) for k in ('vps','studio','hub'))
        check('VPS reboot and automatic helper recovery', bool(v.get('boot_before')) and v.get('boot_before')!=v.get('boot_after') and bool(v.get('boot_after')) and v.get('service')=='active' and v.get('enabled')=='enabled' and v.get('linger')=='yes' and v.get('check')=='READY' and 0<=v.get('observed_at',0)-v.get('helper_seen',-1000)<60, str(v))
        check('Studio helper restarts in desktop session', bool(s.get('pid_before')) and bool(s.get('pid_after')) and s['pid_before']!=s['pid_after'] and s.get('state')=='running' and s.get('domain')=='gui/501' and s.get('helper_seen',0)>=s.get('stopped_at',float('inf')) and 0<=s.get('observed_at',0)-s.get('helper_seen',-1000)<60, str(s))
        check('hub services remain available', all(h.get(k)=='active' for k in ('hub','git','manager')) and h.get('health') is True, str(h))
    if run:
        check(run+' covers tests 1–14', {c['test'] for c in cases} == set(range(1,15)), str(sorted(c['test'] for c in cases)))
    for c in cases:
        cid, test, exp = c['id'], c['test'], json.loads(c['expected'])
        tasks = [t for t in records['tasks'] if t['case_id'] == cid]
        ids = {t['id'] for t in tasks}
        end = max([t['checked_at'] or t['delivered'] or records['time'] for t in tasks] or [records['time']])
        calls = [x for x in records['calls'] if c['created'] <= x['ts'] <= end+5]
        ev = [x for x in records['events'] if c['created'] <= x['created'] <= end+5]
        evidence = {x['kind']: json.loads(x['data']) for x in records['evidence'] if x['case_id'] == cid}
        logs = [x for x in records['log'] if c['created'] <= x['ts'] <= end+5]
        qs = [x for x in records['questions'] if x['task'] in ids]
        ps = [x for x in records['prompts'] if x['task'] in ids]
        hits = [x for x in records['hits'] if c['created'] <= x['ts'] <= end]
        def ck(label, ok, detail=''):
            check(cid+' '+label, ok, detail or str(ok))
        ck('task count', len(tasks) == exp['tasks'], f"{len(tasks)} / {exp['tasks']}; ids {sorted(ids)}")
        for t in tasks:
            tid = 'T-'+str(t['id'])
            ck(tid+' result', t['state'] == 'completed' and t['accepted'] == 1 and all(t[k] for k in ('what','where_','how','checked_at')), f"{t['state']}; accepted={t['accepted']}; output={t['where_']}; checked={t['how']}")
            accepts = [x for x in records['calls'] if x['name']=='check_result' and x['ok'] and x['agent']==t['requester'] and json.loads(x['args']).get('task_id')==tid and json.loads(x['args']).get('accepted') is True]
            ck(tid+' requester checked', bool(accepts), str([x['id'] for x in accepts]))
            capability = records['team']['agents'][t['owner']]['can']
            ck(tid+' route', t['needs']=='none' or t['needs'] in capability, f"{t['owner']}; needs={t['needs']}")
            reads = [x for x in records['events'] if x['kind']=='task' and x['task']==t['id'] and x['read_at']]
            ck(tid+' inbox receipt', bool(reads), str([x['id'] for x in reads]))
        ck('only manager contacts owner', all(x['by']=='manager' for x in records['needs']) and not any(x['ok'] and x['agent']!='manager' and x['name']=='raise_needs_you' for x in records['calls']), 'Needs-you authors and successful tool calls')
        content = {}
        for path in exp.get('files',[]):
            local = pathlib.Path(kit)/path
            tracked = subprocess.run(['git','ls-files','--error-unmatch','--',path],cwd=kit,capture_output=True).returncode == 0
            history = subprocess.run(['git','log','-1','--format=%H','--',path],cwd=kit,capture_output=True,text=True).stdout.strip()
            present = local.is_file() and local.stat().st_size > 0
            ck('artifact '+path, tracked and present and bool(history), f'commit={history or "missing"}')
            content[path] = local.read_bytes() if present else b''
            if path.endswith('.jpg'):
                ck('JPEG '+path, content[path].startswith(b'\xff\xd8') and len(content[path])>1000, f'{len(content[path])} bytes')
        texts = {p:b.decode('utf-8','replace') for p,b in content.items() if not p.endswith(('.jpg','.png'))}
        alltext = '\n'.join(texts.values())
        if test == 1:
            snap = evidence.get('team',{})
            rows = snap.get('agents',[])
            ck('three live helpers and CLI names', len([a for a in rows if a['name'] in ('manager','vps','studio') and a['client'] and 0 <= snap.get('time',0)-(a['last_seen'] or 0)<60]) == 3, str([{k:a.get(k) for k in ('name','last_seen','client')} for a in rows]))
        if test == 2:
            ck('progress recorded', any(x['task'] in ids for x in records.get('notes',[])), 'task progress notes')
            ck('owner request read', any(x['agent']=='manager' and '[owner]' in x['text'] and cid in x['text'] and x['read_at'] for x in ev), 'durable owner-message receipt')
            if exp.get('startup'):
                start=evidence.get('manager_startup',{}).get('restarted_at')
                ck('request delivered across manager startup', bool(start) and any(x['agent']=='manager' and '[owner]' in x['text'] and cid in x['text'] and x['read_at'] and x['created']>=start for x in ev), str(start))
            ck('follow-up word absent from original', bool(alltext.strip()) and 'lighthouse' not in alltext.lower(), alltext[:160])
            ck('haiku has three lines', any(len([v for v in text.splitlines() if v.strip()])==3 for text in texts.values()), alltext[:160])
        if test in (3,8,9,12):
            ck('browser routing', bool(tasks) and all(t['owner']=='studio' and t['needs']=='browser' for t in tasks), str([(t['id'],t['owner'],t['needs']) for t in tasks]))
        if test == 4:
            count = next((t for t in tasks if 'count' in (t['where_'] or '')), None)
            ck('one task waits while count completes', bool(count and count['delivered'] and qs and all(q['answered'] and q['asked']<count['delivered']<=q['answered'] for q in qs)), str([(q['asked'],q['answered']) for q in qs])+f'; count delivered={count and count["delivered"]}')
            ck('colour and count', any(s.strip()=='teal' for s in texts.values()) and any(re.findall(r'\d+',s)==list(map(str,range(1,11))) for s in texts.values()), alltext[:200])
        if test in (5,10):
            sends = [x for x in records['outbox'] if x['key']==exp['key']]
            ck('one outside send', len(sends)==1, exp['key']+f' count={len(sends)}')
        if test == 5:
            ck('real permission channel', any(p['tool']=='Bash' and exp['key'] in p['input'] and p['decision']=='allow' and p['by']=='manager' for p in ps), str([(p['id'],p['decision'],round((p['decided'] or p['created'])-p['created'],1)) for p in ps]))
            ck('permission command delivered in full', all(any(p['input'] in e['text'] for e in ev) for p in ps), 'stored command matches manager inbox')
        if test in (6,7):
            follow = [t for t in tasks if t['follows']]
            ck('new task follows a completed task', len(follow)==1 and any(p['id']==follow[0]['follows'] and p['state']=='completed' and p['case_id']==(cid if test==6 else cid.rsplit('-',1)[0]+'-02') and p['delivered']<=follow[0]['created'] for p in records['tasks']), str([(t['id'],t['follows']) for t in follow]))
        if test == 6:
            ck('both workers and source links', {t['owner'] for t in tasks}=={'vps','studio'} and 'https://' in alltext, 'brief and screenshot')
            versions=subprocess.run(['git','log','--format=%H','--',exp['files'][0]],cwd=kit,capture_output=True,text=True).stdout.splitlines()
            ck('brief preserved by follow-up', len(versions)==1, str(versions))
            ck('follow-up names source file', any(exp['files'][0] in t['details'] for t in tasks if t['follows']), exp['files'][0])
        if test == 7:
            original=pathlib.Path(kit)/f"outputs/{cid.rsplit('-',1)[0]}-02/haiku.txt"
            first=original.read_text() if original.is_file() else ''
            ck('word was absent before the follow-up', bool(first.strip()) and 'lighthouse' not in first.lower(), first[:160])
            ck('revised haiku has three lines', any(len([v for v in s.splitlines() if v.strip()])==3 for s in texts.values()), alltext[:160])
            ck('new requested word', 'lighthouse' in alltext.lower(), alltext[:160])
        if test == 8:
            ck('browser read with no API read', any(h['path'].startswith('/site/price') and h['ua']=='Chrome' for h in hits) and not any(h['path'].startswith('/api/price') for h in hits), str(hits))
            ck('price and stock', '42' in alltext and '17' in alltext and 'browser' in alltext.lower(), alltext[:220])
        if test == 9:
            overlap = len(tasks)==2 and all(t['started'] and t['delivered'] for t in tasks) and max(t['started'] for t in tasks)<min(t['delivered'] for t in tasks)
            in_overlap = [h for h in hits if overlap and max(t['started'] for t in tasks)<=h['ts']<=min(t['delivered'] for t in tasks)]
            ck('two sessions and Chrome reads overlap', overlap and all(any(h['path'].startswith('/site/'+colour) and h['ua']=='Chrome' for h in in_overlap) for colour in ('blue','orange')), str(in_overlap))
            hashes = [hashlib.sha256(b).hexdigest() for b in content.values() if b]
            ck('two distinct screenshots', len(hashes)==2 and len(set(hashes))==2, str(hashes))
        if test == 10:
            crash = evidence.get('crash',{})
            attempts = [a for a in records['attempts'] if a['task'] in ids]
            restarted = [a for a in attempts if a['attempt']==2 and a['started']]
            ck('restart within 120 seconds', bool(restarted and crash.get('killed_at') and 0<restarted[0]['started']-crash['killed_at']<=120), str(attempts))
            ck('old process tree stopped', bool(crash.get('before_pids')) and crash.get('after_pids')==[], str(crash))
            stale = evidence.get('stale_probe',{})
            ck('old attempt refused', stale.get('isError') is True and 'STOP' in json.dumps(stale), str(stale))
            try:
                env = next((json.loads(text) for p,text in texts.items() if p.endswith('env.json')), {})
            except ValueError:
                env = {}
            ck('task environment', bool(tasks) and env.get('FLEET_TASK')=='T-'+str(tasks[0]['id']) and env.get('FLEET_AGENT')=='vps' and env.get('FLEET_HUB') and env.get('FLEET_GENERATION')==records['db'] and env.get('FLEET_ATTEMPT')=='2', str(env))
        if test == 11:
            nudges = [x for x in logs if any(f'silent T-{i}' in x['what'] for i in ids) and x['what'].startswith('nudged')]
            ck('STALLED recorded in hub log', bool(nudges), str(nudges))
            ck('eight-minute task finishes', bool(tasks) and all(t['delivered'] and t['started'] and t['delivered']-t['started']>=480 and t['nudge_level']==0 for t in tasks), str([(t['delivered'],t['started'],t['nudge_level']) for t in tasks]))
        if test == 12:
            offline = [x for x in logs if any(f'T-{i} never started' in x['what'] for i in ids)]
            ck('offline warning and later start', bool(offline) and all(t['started'] and t['started']>offline[0]['ts'] and offline[0]['ts']-t['created']>=180 for t in tasks), str(offline))
            ck('manager decides after warning', any(x['agent']=='manager' and x['ok'] and x['name'] in ('set_reminder','raise_needs_you','cancel_task') and offline and x['ts']>offline[0]['ts'] for x in calls), 'manager decision call')
        if test == 13:
            down = evidence.get('manager_down',{})
            alerts = [e for e in ev if e['id']==down.get('event_id') and e['agent']=='owner' and 'manager session is not running' in e['text']]
            ck('hub alert within one minute', bool(alerts and down.get('killed_at') and 0<=alerts[0]['created']-down['killed_at']<=60), str([(e['id'],e['created']) for e in alerts]))
            visible = evidence.get('notification_visible',{})
            ck('notification seen on Mac screen', visible.get('event_id') == down.get('event_id') and visible.get('event_id') in [e['id'] for e in alerts] and bool(visible.get('observation')), str(visible))
            ck('manager recovers and reads status', any(x['agent']=='manager' and x['name']=='see_team' and x['ok'] and x['ts']>down.get('restarted_at',float('inf')) for x in calls), str(down))
        if test == 14:
            needs = [n for n in records['needs'] if c['created']<=n['raised']<=end and n['answer']==exp['codename']]
            ck('worker asks before manager asks owner', bool(qs and needs) and qs[0]['asked']<=needs[0]['raised']<=(needs[0]['answered'] or 0) and (qs[0]['answered'] or 0)>=(needs[0]['answered'] or float('inf')), str([(q['id'],q['asked'],q['answered']) for q in qs])+str([(n['id'],n['raised'],n['answered']) for n in needs]))
            ck('owner answer becomes file', any(s.strip()==exp['codename'] for s in texts.values()), alltext[:160])
    ok = bool(checks) and all(checks)
    emit(f"{'PASS' if ok else 'FAIL'}: {sum(checks)}/{len(checks)} checks")
    return ok
