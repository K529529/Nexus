"""Offline RCA and deterministic before/after evidence; no network/model calls."""
from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
import httpx
from nexus.app.config import ModelConfig
from nexus.core.model import ChatModel
from tests.test_model import Recorder, chunk
from tests.test_model_transport import Client, Stream, success

OUT = Path(__file__).parent
BASE = '412e51175c9a116bdb327a4ac2e9a7782d4d7e7f'
old = ModuleType('transport_baseline')
sys.modules[old.__name__] = old
exec(compile(subprocess.check_output(['git','show',BASE+':src/nexus/core/model.py'],cwd=ROOT),'<baseline model.py>','exec'),old.__dict__)


def scenario(name):
    error = httpx.RemoteProtocolError('secret-not-for-events')
    if name == 'create': return Client([error,error])
    if name == 'before_delta': return Client([Stream([error]),success()])
    if name == 'partial': return Client([Stream([chunk({'content':'partial'}),error])])
    if name == 'close': return Client([success('transport')])
    if name == 'read_and_close': return Client([Stream([chunk({'content':'partial'}),httpx.ReadError('private')],'transport')])
    if name == 'cancel': return Client([Stream([asyncio.CancelledError()],'transport')])
    return Client([success()])


async def capture(cls,name):
    emit=Recorder()
    client=scenario(name)
    model=cls(ModelConfig('test',32768),client=client)
    outcome={}
    try:
        reply=await model.complete([],[],emit)
        outcome={'returned':reply.message.content}
    except (Exception,asyncio.CancelledError) as exc:
        outcome={'exception_type':type(exc).__name__,'code':getattr(exc,'code',None)}
    for kind,data in emit.events:
        if kind=='model_finished':data['duration_ms']=0
    return {**outcome,'events':emit.events,'starts':sum(k=='model_started' for k,_ in emit.events),'finishes':sum(k=='model_finished' for k,_ in emit.events),'requests':len(client.requests),'client_closes':client.closes}


async def benchmark(cls):
    durations=[]
    for _ in range(5):
        start=time.perf_counter()
        for _ in range(200):
            model=cls(ModelConfig('test',32768),client=Client([success()]))
            await model.complete([],[],Recorder())
        durations.append((time.perf_counter()-start)*1e6/200)
    return {'unit':'microseconds per fake complete, includes construction','samples':durations,'median':statistics.median(durations)}


def historical():
    root=Path.home()/'.nexus/evaluation/results'
    output=[]
    for rid in ('20261023T212800Z-def5aaf5','20261024T055859Z-d3c0919a'):
        summary=json.loads((root/rid/'summary.json').read_text(encoding='utf-8'))
        for case in summary['cases']:
            p=root/rid/case['case_id']/case['artifacts']['session']
            raw=p.read_bytes()
            rows=[json.loads(line) for line in raw.splitlines()]
            pending=None; faults=[]; pairs=[]
            for row in rows:
                if row['kind']=='model_started':pending=row
                elif row['kind']=='model_finished':
                    if row['data'].get('error'):
                        faults.append({'start_seq':pending['seq'] if pending else None,'finish_seq':row['seq'],'timestamp':row['timestamp'],'data':row['data']})
                    pairs.append((pending['seq'] if pending else None,row['seq']))
                    pending=None
                elif row['kind']=='run_finished' and pending:
                    faults.append({'start_seq':pending['seq'],'start_time':pending['timestamp'],'start_data':pending['data'],'finish_seq':None,'run_end_seq':row['seq'],'run_end_time':row['timestamp'],'reason':row['data']['reason']})
            output.append({'run':rid,'case':case['case_id'],'path':str(p),'sha256':hashlib.sha256(raw).hexdigest(),'paired_attempts':len(pairs),'faults':faults})
    return output


async def main():
    result={'baseline':BASE,'method':'Historical JSONL read only; fault injection and microbenchmark use fake clients. No provider requests or old-session resume.'}
    result['history']=historical()
    result['injection']={name:{'before':await capture(old.ChatModel,name),'after':await capture(ChatModel,name)} for name in ('create','before_delta','partial','close','read_and_close','cancel','success')}
    result['benchmark']={'before':await benchmark(old.ChatModel),'after':await benchmark(ChatModel)}
    sizes={}
    for name,versions in result['injection'].items():
        sizes[name]={v:sum(len(json.dumps(d,ensure_ascii=False,separators=(',',':')).encode()) for k,d in item['events'] if k=='model_finished') for v,item in versions.items()}
    result['finished_payload_bytes']=sizes
    (OUT/'rca.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'injection':{n:{v:{k:item[k] for k in ('starts','finishes','requests')} for v,item in versions.items()} for n,versions in result['injection'].items()},'bytes':sizes,'benchmark':result['benchmark']},indent=2))

if __name__=='__main__':asyncio.run(main())
