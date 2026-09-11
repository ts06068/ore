"""Workflow binding for deterministic programs and host-verified recipe reuse."""
from __future__ import annotations

import asyncio
import inspect

from .models import canonical_digest
from .policy import AccessDenied
from .recipes import RecipeCatalog, RecipeError, execute_recipe, validate_program
from .tools import ToolRuntime

RECIPE_SPECS = [
    {'name': 'recipe.propose', 'description': 'Store a bounded deterministic candidate program. Proposal is not validation or promotion. Program version1 has steps with stable id, tool+inputs, items+steps, repeat+until+steps, or value; expressions use $ref,$urljoin,$concat,$format,$len,$json and comparisons.',
     'input_schema': {'type': 'object', 'properties': {'program': {'type': 'object'}, 'description': {'type': 'string'}}, 'required': ['program'], 'additionalProperties': False}},
    {'name': 'recipe.execute', 'description': 'Execute a bounded deterministic program through the same authorized durable tools, without additional model calls. Supply program for an explicit candidate, or recipe_id for a recipe independently promoted under this source/tool/access/validator contract. Inputs are data. Step references use steps.ID (not steps.ID.output); loop inputs use item/index.',
     'input_schema': {'type': 'object', 'properties': {'program': {'type': 'object'}, 'recipe_id': {'type': 'string'}, 'inputs': {'type': 'object'}}, 'required': ['inputs'], 'oneOf': [{'required': ['program']}, {'required': ['recipe_id']}], 'additionalProperties': False}},
    {'name': 'recipe.inspect', 'description': 'Read matching promoted recipes or one candidate status. Model assertions cannot add verification cases or promote a recipe.',
     'input_schema': {'type': 'object', 'properties': {'recipe_id': {'type': 'string'}}, 'additionalProperties': False}},
]


class WorkflowRecipes:
    def __init__(self, native):
        self.native, self.manager = native, native.manager
        self.catalog = RecipeCatalog(self.manager.store)

    def _contract(self, run, node, job, program):
        checked = validate_program(program)
        specs = {row['name']: row for row in self.manager._catalog()}
        for tool in checked['tools']:
            self.manager._allowed(run, node, tool)
            if tool not in specs: raise AccessDenied('Recipe tool is not registered')
        mission = job['mission']; completion = node['spec'].get('completion', {})
        verifier_id = completion.get('validator')
        registry = getattr(self.manager.engine, 'recipe_verifiers', {})
        verifier = registry.get(verifier_id)
        if verifier_id and (not isinstance(verifier, dict) or not callable(verifier.get('verify')) or not verifier.get('digest')):
            raise AccessDenied('Recipe validator is not registered by the host')
        validator = {'id': verifier_id, 'digest': verifier['digest']} if verifier else {'unavailable': True}
        source = getattr(self.manager.engine, 'recipe_source_contract', None) or {
            'allowed_origins': mission.get('allowed_origins', []), 'sources': mission.get('sources', []),
            'protocol_digest': job.get('protocol_digest'), 'source_policy': mission.get('source_policy')}
        access = {key: mission.get(key) for key in ('access_profile', 'access_profile_ref', 'source_policy', 'retrieval_policy')}
        profile = self.manager.engine.profile(mission) if hasattr(self.manager.engine, 'profile') else {}
        access['profile_digest'] = canonical_digest(profile)
        contract = self.catalog.contract(tools={tool: specs[tool].get('digest', specs[tool].get('version')) for tool in checked['tools']} or {'none': True},
                                         source=source, validator=validator, access=access)
        owner = str(mission.get('access_profile_ref') or mission.get('access_profile', 'public'))
        return contract, owner, verifier

    def propose(self, run, node, job, program, description=''):
        contract, owner, _ = self._contract(run, node, job, program)
        row = self.catalog.propose(program, contract=contract, owner=owner, description=description)
        return {key: row[key] for key in ('id', 'status', 'program_digest', 'contract', 'description', 'tools')}

    def inspect(self, run, node, job, ident=None):
        rows = self.manager.store.list_documents(self.catalog.collection)
        found = []
        for row in rows:
            if ident is not None and row['id'] != ident: continue
            try:
                contract, owner, _ = self._contract(run, node, job, row['program'])
            except (ValueError, AccessDenied): continue
            if row['owner'] != owner or row['contract'] != contract: continue
            if ident is None and row['status'] != 'promoted': continue
            found.append({key: row[key] for key in ('id', 'status', 'program', 'description', 'tools', 'program_digest', 'reuses')})
            if len(found) >= 20: break
        if ident is not None and not found: raise AccessDenied('Recipe is outside this execution contract')
        return {'recipes': found}

    async def try_replay(self, task, run, node, job):
        """An approved hint selects only independently promoted compatible code."""
        from .workflow import WorkflowError, resolve
        hint = node['spec'].get('replay')
        if not isinstance(hint, dict): return False
        reason = 'recipe_not_promoted'
        recipe_id = hint.get('recipe_id')
        try:
            program = hint.get('program')
            if recipe_id:
                row = self.manager.store.get_document(self.catalog.collection, recipe_id)
                if not row: raise RecipeError('Unknown replay recipe')
                program = row['program']
            if not isinstance(program, dict): raise RecipeError('Replay requires a program or recipe identity')
            contract, owner, verifier = self._contract(run, node, job, program)
            if not recipe_id:
                recipe_id = canonical_digest({'program': validate_program(program)['digest'], 'contract': contract, 'owner': owner})
            if verifier and self.catalog.can_replay(recipe_id, contract=contract, owner=owner):
                nodes = {value['id']: value for value in self.manager.nodes(run['id'])}
                inputs = resolve(hint.get('inputs', node.get('resolved_inputs', {})), self.manager._context(run, node, nodes))
                result = await self.execute(task, run, node, job, inputs=inputs,
                                            operation_id='promoted-replay', recipe_id=recipe_id)
                with self.manager.store._tx() as conn:
                    current_run, current_node, current_job = self.manager._owned(conn, task)
                    current_node['deterministic_replay'] = True
                    self.manager._save(conn, 'workflow.node', [run['id'], node['id']], current_node, current_job)
                self.manager._finish(task, result['output'])
                self.manager.store.append_event(job['id'], 'recipe.replayed',
                    {'run_id': run['id'], 'node_id': node['id'], 'recipe_id': recipe_id, 'model_calls': 0})
                return True
        except (RecipeError, WorkflowError, AccessDenied, ValueError, TypeError, KeyError) as exc:
            # Unknown side effects are not an ordinary fallback: preserve their
            # reconciliation gate instead of issuing a second operation.
            from .workflow import NeedsReconciliation, NodeOutcome
            if isinstance(exc, NeedsReconciliation) or isinstance(exc, NodeOutcome) and exc.status in ('awaiting_user', 'awaiting_auth', 'paused_budget', 'needs_reconciliation'):
                raise
            reason = type(exc).__name__
            if recipe_id and self.manager.store.get_document(self.catalog.collection, recipe_id):
                self.catalog.invalidate(recipe_id, 'replay_output_or_contract_failed')
        with self.manager.store._tx() as conn:
            active_run, active_node, active_job = self.manager._owned(conn, task)
            active_node.pop('deterministic_replay', None)
            active_node['replay_fallback'] = {'reason': reason, 'recipe_id': recipe_id,
                'inspect': {'kind': 'receipts', 'node_id': node['id']}}
            self.manager._save(conn, 'workflow.node', [run['id'], node['id']], active_node, active_job)
        self.manager.store.append_event(job['id'], 'recipe.fallback',
            {'run_id': run['id'], 'node_id': node['id'], 'reason': reason})
        return False

    async def execute(self, task, run, node, job, *, inputs, operation_id, program=None, recipe_id=None):
        if (program is None) == (recipe_id is None): raise RecipeError('Supply exactly one program or recipe_id')
        replay_requested = recipe_id is not None
        if recipe_id is not None:
            row = self.manager.store.get_document(self.catalog.collection, recipe_id)
            if not row: raise AccessDenied('Unknown recipe')
            program = row['program']
        contract, owner, verifier = self._contract(run, node, job, program)
        if recipe_id is not None:
            if not self.catalog.can_replay(recipe_id, contract=contract, owner=owner):
                raise AccessDenied('Recipe has not been independently promoted under this execution contract')
        else:
            row = self.catalog.propose(program, contract=contract, owner=owner)
            recipe_id = row['id']
        key = [run['id'], node['id'], node.get('execution_digest', node['spec_digest']), operation_id]
        old = self.manager.store.get_document('workflow.recipe_execution', key)
        expected = canonical_digest({'recipe': recipe_id, 'inputs': inputs})
        if old and old['input_digest'] != expected: raise RecipeError('Recipe call identity was reused with different inputs')
        lease = {field: task[field] for field in ('worker_id', 'fence', 'revision')} | {'task_id': task['id']}
        state = old or {'run_id': run['id'], 'node_id': node['id'], 'recipe_id': recipe_id, 'input_digest': expected, 'status': 'started', 'checkpoint': None}
        lock = asyncio.Lock()
        async def save(checkpoint):
            nonlocal state
            async with lock:
                state = self.manager.store.put_document('workflow.recipe_execution', key,
                    {**state, 'checkpoint': checkpoint}, job_id=job['id'], lease=lease,
                    expected_version=state.get('state_version', 0))
        cap = max(1, min(int(job['mission'].get('budget', {}).get('max_agent_workers', 5)), int(getattr(self.manager.engine.settings, 'max_workers', 5))))
        semaphore = self.native.semaphores.setdefault(run['id'], asyncio.Semaphore(cap))
        async def call(tool, arguments, step_id):
            async with semaphore:
                self.native.budget(job).authorize('execution')
                runtime = ToolRuntime(self.manager.engine, job['id'], task)
                return await self.manager._tool(task, runtime, tool, arguments, operation_id + ':' + step_id)
        result = await execute_recipe(program, inputs, call, checkpoint=state.get('checkpoint'), on_checkpoint=save,
            max_steps=min(20000, int(job['mission'].get('budget', {}).get('max_tasks', 10000))), max_concurrency=cap,
            max_output_bytes=1_500_000)
        state = self.manager.store.put_document('workflow.recipe_execution', key,
            {**state, 'status': 'validation_pending' if verifier else 'completed', 'output': result['output'],
             'reused': replay_requested, 'independent_verification': 'pending' if verifier else 'unavailable'},
            job_id=job['id'], lease=lease, expected_version=state.get('state_version', 0))
        if verifier:
            receipts = [receipt for receipt in self.manager.store.list_documents('workflow.operation', run['job_id'])
                        if receipt.get('run_id') == run['id'] and receipt.get('node_id') == node['id'] and
                        str(receipt.get('operation_id', '')).startswith(operation_id + ':')]
            evidence = {'result': result['output'], 'receipts': receipts, 'artifacts': self.manager.store.artifacts(run['job_id'])}
            async def verify(values, proof):
                decision = verifier['verify'](values, proof)
                if inspect.isawaitable(decision): decision = await decision
                if not isinstance(decision, dict) or decision.get('validator_digest') != verifier['digest']:
                    raise RecipeError('Host verifier version does not match the bound contract')
                return decision
            verified = await self.catalog.verify(recipe_id, inputs=inputs, evidence=evidence, verifier=verify)
            passed = verified['status'] != 'invalidated'
            state = self.manager.store.put_document('workflow.recipe_execution', key,
                {**state, 'status': 'completed' if passed else 'verification_failed',
                 'independent_verification': 'passed' if passed else 'failed'}, job_id=job['id'], lease=lease,
                expected_version=state.get('state_version', 0))
            if not passed:
                raise RecipeError('Independent recipe verification failed; the cached program was invalidated')
        if replay_requested and state.get('independent_verification') == 'passed' and not (old and old.get('status') == 'completed' and old.get('independent_verification') == 'passed'):
            self.catalog.reused(recipe_id, contract=contract, owner=owner)
        return {'output': result['output'], 'recipe_id': recipe_id, 'program_digest': result['program_digest'],
                'model_calls': 0, 'reuse_eligible': bool(verifier and self.catalog.can_replay(recipe_id, contract=contract, owner=owner))}
