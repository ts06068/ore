#!/usr/bin/env python3
"""Actual isolated-program workflow acceptance. No model or publisher requests.

Use --state-dir on a shared host-visible workspace. For remote Docker set
ORE_CODE_HOST_ROOT to the daemon's corresponding <state-dir>/code-staging path.
The default cgroup mode is strict; use explicit ORE_CODE_RESOURCE_MODE=rlimit only
on hosts requiring the documented per-process resource mode.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time

from ore.config import Settings
from ore.engine import Engine
from ore.models import canonical_digest


def ref(value): return {'$ref':value}

def equal(left,right): return {'op':'eq','left':left,'right':right}

async def main(args):
    engine=Engine(Settings(state_dir=args.state_dir,max_workers=3))
    results={}
    try:
        program={'language':'python','source':'import json,sys,time\nvalue=json.load(sys.stdin)\ntime.sleep(1)\nprint(json.dumps({"sum":sum(value)}))',
            'input_schema':{'type':'array','items':{'type':'integer'}},
            'output_schema':{'type':'object','properties':{'sum':{'type':'integer'}},'required':['sum'],'additionalProperties':False}}
        plan={'goal':'Validate parallel isolated generated-code execution and exact saved outputs',
            'scope':{'fixture':'integer arrays'},'outputs':['verified JSON results'],'constraints':{},
            'budget':{'max_turns':1,'max_seconds':120,'max_agent_workers':3},
            'mission':{'artifact_roles':[]},
            'workflow':{'nodes':[
                {'id':'program','kind':'tool','tool':'code.register','inputs':program,'depends_on':[],
                 'checks':[{'op':'exists','value':ref('output.program_id')}]},
                {'id':'parallel','kind':'foreach','items':[[1,2],[3,4],[5,6]],'depends_on':['program'],
                 'body':[{'id':'sum','kind':'tool','tool':'code.run','depends_on':['program'],
                    'inputs':{'program_id':ref('nodes.program.output.program_id'),'input':ref('item')},
                    'checks':[equal(ref('output.output_verified'),True)]}],
                 'checks':[{'type':'schema','value':ref('output.children'),'schema':{'type':'object','minProperties':3,'maxProperties':3}}]},
                {'id':'file','kind':'tool','tool':'content.write','depends_on':['parallel'],
                 'inputs':{'data':ref('nodes.parallel.output.children'),'format':'json','filename':'parallel-results.json'},
                 'checks':[equal(ref('output.artifact.status'),'verified')]}]},
            'acceptance':[equal(ref('nodes.file.output.artifact.status'),'verified')]}
        run=engine.workflows.create_run(plan)
        start=time.monotonic();await engine.workflows.start_run(run['id'])
        deadline=start+120
        peak_containers=0
        while time.monotonic()<deadline:
            run=engine.workflows.get_run(run['id'])
            if run['status'] not in ('running','queued','resuming'):break
            process=await asyncio.create_subprocess_exec('docker','ps','--filter','label=ore.job='+run['job_id'],'--format','{{.Names}}',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            stdout,_=await process.communicate()
            peak_containers=max(peak_containers,len(stdout.decode().splitlines()))
            await asyncio.sleep(.1)
        nodes=engine.workflows.nodes(run['id'])
        outputs=[node['output'] for node in nodes if node['kind']=='tool' and node['spec'].get('tool')=='code.run' and node['status']=='succeeded']
        model_events=[event for event in engine.store.events(run['job_id']) if event['type'] in ('model_selected','agent_decision')]
        artifact=next(iter(engine.store.artifacts(run['job_id'])),None)
        checks={'completed':run['status']=='completed','three_containers':len({value['container_name'] for value in outputs})==3,
                'exact_results':sorted(value['output']['sum'] for value in outputs)==[3,7,11],
                'parallel_containers_observed':peak_containers>=2,'zero_model_calls':not model_events,'artifact_verified':bool(artifact and artifact['status']=='verified'),
                'audit_pass':engine.audit(run['job_id'])['status']=='complete_within_scope'}
        results={'schema_version':'ore.workflow-runtime-acceptance/v1','kind':'actual Docker execution; fixed arithmetic fixture',
            'state_dir':str(args.state_dir),'run_id':run['id'],'job_id':run['job_id'],'status':'passed' if all(checks.values()) else 'failed',
            'checks':checks,'run_status':run['status'],'elapsed_seconds':time.monotonic()-start,'containers':outputs,
            'model_calls':len(model_events),'peak_parallel_containers':peak_containers,'nodes':[{'id':node['id'],'status':node['status'],'error':node.get('error')} for node in nodes]}
        # Stop a genuinely running generated program, then independently query
        # Docker rather than equating local coroutine cancellation with success.
        stop_program={**program,'source':'import json,sys,time\njson.load(sys.stdin)\ntime.sleep(60)\nprint(json.dumps({"sum":0}))'}
        stop_plan={**plan,'goal':'Verify physical interruption of a running generated-code container',
            'workflow':{'nodes':[
                {'id':'program','kind':'tool','tool':'code.register','inputs':stop_program,'depends_on':[],
                 'checks':[{'op':'exists','value':ref('output.program_id')}]},
                {'id':'wait','kind':'tool','tool':'code.run','depends_on':['program'],
                 'inputs':{'program_id':ref('nodes.program.output.program_id'),'input':[1], 'timeout_seconds':90},
                 'checks':[equal(ref('output.output_verified'),True)]}]},'acceptance':[]}
        stopped=engine.workflows.create_run(stop_plan)
        await engine.workflows.start_run(stopped['id'])
        deadline=time.monotonic()+25;running=[]
        while time.monotonic()<deadline:
            process=await asyncio.create_subprocess_exec('docker','ps','--filter','label=ore.job='+stopped['job_id'],'--format','{{.Names}}',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            out,_=await process.communicate();running=out.decode().splitlines()
            if running:break
            await asyncio.sleep(.1)
        await engine.workflows.interrupt(stopped['id'])
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            stopped=engine.workflows.get_run(stopped['id'])
            if stopped['status']=='paused':break
            await asyncio.sleep(.1)
        process=await asyncio.create_subprocess_exec('docker','ps','--filter','label=ore.job='+stopped['job_id'],'--format','{{.Names}}',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        out,_=await process.communicate()
        checks.update({'stop_actual_container_started':bool(running), 'stop_confirmed':stopped['status']=='paused' and stopped.get('interrupt_confirmed') is True,
            'stop_container_absent':not out.strip(),'prior_artifact_preserved':engine.store.artifacts(run['job_id'])[0]['sha256']==artifact['sha256']})
        results['stop']={'run_id':stopped['id'],'job_id':stopped['job_id'],'status':stopped['status'],'started_containers':running,
                        'remaining_containers':out.decode().splitlines(),'interrupt_confirmed':stopped.get('interrupt_confirmed')}
        results['status']='passed' if all(checks.values()) else 'failed'
        args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(results,indent=2)+'\n')
        print(json.dumps({'status':results['status'],'report':str(args.report),'checks':checks}))
        return 0 if all(checks.values()) else 1
    finally:await engine.stop()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--report',type=Path,default=Path('.ore/reports/workflow-runtime-live.json'))
    raise SystemExit(asyncio.run(main(parser.parse_args())))
