"""Bounded deterministic tool programs and evidence-gated reuse.

The interpreter grants no permissions: every primitive goes through the caller's
normal admission and operation journal. Programs are data, never Python/eval.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import json
import re
from string import Formatter
from urllib.parse import urljoin

from .models import canonical_digest


class RecipeError(ValueError):
    pass


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())


def evaluate(value, context, *, depth=0):
    if depth > 48:
        raise RecipeError('Expression depth exceeds 48')
    recur = lambda child: evaluate(child, context, depth=depth + 1)
    if isinstance(value, list):
        return [recur(child) for child in value]
    if not isinstance(value, dict):
        return value
    if len(value) != 1 or not next(iter(value)).startswith('$'):
        return {key: recur(child) for key, child in value.items()}
    name, args = next(iter(value.items()))
    if name == '$ref':
        if not isinstance(args, str) or len(args) > 512:
            raise RecipeError('Invalid reference')
        result = context
        try:
            for key in args.split('.'):
                result = result[int(key)] if isinstance(result, list) else result[key]
            return deepcopy(result)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RecipeError(f'Unresolved reference: {args}') from exc
    if name == '$literal':
        return deepcopy(args)
    evaluated = recur(args)
    if name == '$concat':
        if not isinstance(evaluated, list): raise RecipeError('concat expects a list')
        if all(isinstance(item, list) for item in evaluated):
            return [child for item in evaluated for child in item]
        if not all(isinstance(item, (str, int, float)) and not isinstance(item, bool) for item in evaluated):
            raise RecipeError('concat expects strings or lists')
        return ''.join(map(str, evaluated))
    if name in ('$eq', '$ne', '$lt', '$le', '$gt', '$ge', '$urljoin'):
        if not isinstance(evaluated, list) or len(evaluated) != 2: raise RecipeError('Binary expression expects two values')
        left, right = evaluated
        return {'$eq': lambda: left == right, '$ne': lambda: left != right,
                '$lt': lambda: left < right, '$le': lambda: left <= right,
                '$gt': lambda: left > right, '$ge': lambda: left >= right,
                '$urljoin': lambda: urljoin(left, right)}[name]()
    if name == '$add':
        if not isinstance(evaluated, list) or not all(type(item) in (int, float) for item in evaluated):
            raise RecipeError('add expects numbers')
        return sum(evaluated)
    if name == '$len': return len(evaluated)
    if name == '$json': return json.loads(evaluated)
    if name == '$not': return not evaluated
    if name in ('$all', '$any'):
        if not isinstance(evaluated, list): raise RecipeError('Boolean expression expects a list')
        return all(evaluated) if name == '$all' else any(evaluated)
    if name == '$format':
        if not isinstance(evaluated, dict) or set(evaluated) != {'template', 'values'}:
            raise RecipeError('format expects template and values')
        template, values = evaluated['template'], evaluated['values']
        if not isinstance(template, str) or len(template) > 8192 or not isinstance(values, dict):
            raise RecipeError('Invalid format template')
        # No attribute/index traversal, conversions, nested fields or huge widths.
        for _, field, spec, conversion in Formatter().parse(template):
            if field is not None and (not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', field) or conversion or
                                      not re.fullmatch(r'(?:0?[1-9][0-9]{0,2})?[ds]?', spec)):
                raise RecipeError('Unsupported format field')
        return template.format_map(values)
    raise RecipeError(f'Unknown expression: {name}')


def validate_program(program):
    if not isinstance(program, dict) or program.get('version') != 1 or set(program) - {'version', 'steps', 'output'}:
        raise RecipeError('Expected recipe version 1')
    if _size(program) > 262144:
        raise RecipeError('Recipe exceeds 256 KiB')
    tools = set()
    def visit(steps, depth=0):
        if depth > 16 or not isinstance(steps, list) or len(steps) > 1000:
            raise RecipeError('Invalid or overly nested steps')
        ids = set()
        for step in steps:
            if not isinstance(step, dict) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}', str(step.get('id', ''))):
                raise RecipeError('Step requires a stable identifier')
            if step['id'] in ids: raise RecipeError('Duplicate step identifier')
            ids.add(step['id'])
            if set(step) - {'id', 'tool', 'inputs', 'items', 'steps', 'output', 'repeat', 'until', 'when', 'checks', 'value'}:
                raise RecipeError('Unknown step property')
            kinds = sum(key in step for key in ('tool', 'items', 'repeat', 'value'))
            if kinds != 1: raise RecipeError('Step requires exactly one tool, items, repeat or value')
            if 'tool' in step:
                if not isinstance(step['tool'], str): raise RecipeError('Tool name must be a literal')
                tools.add(step['tool'])
            if 'repeat' in step and (type(step['repeat']) is not int or not 1 <= step['repeat'] <= 100):
                raise RecipeError('Repeat must have a bound between 1 and 100')
            if 'items' in step or 'repeat' in step: visit(step.get('steps'), depth + 1)
            if not isinstance(step.get('checks', []), list): raise RecipeError('Checks must be a list')
    visit(program.get('steps'))
    return {'digest': canonical_digest(program), 'tools': sorted(tools)}


async def execute_recipe(program, inputs, call, *, checkpoint=None, on_checkpoint=None,
                         max_steps=20000, max_concurrency=5, max_output_bytes=16_000_000):
    """Execute with stable operation IDs; return output and reusable checkpoint.

    `call(tool, args, operation_id)` must enforce the approved scope and journal.
    Failed/in-flight effects are deliberately not inferred as successful here.
    The caller reconciles their durable receipts before any replay.
    """
    validated = validate_program(program)
    identity = canonical_digest({'program': validated['digest'], 'inputs': inputs})
    if max_steps < 1 or not 1 <= max_concurrency <= 64: raise RecipeError('Invalid execution limits')
    state = deepcopy(checkpoint) if checkpoint is not None else {'identity': identity, 'completed': {}, 'steps': 0}
    if state.get('identity') != identity: raise RecipeError('Checkpoint belongs to a different program or input')
    semaphore, lock = asyncio.Semaphore(max_concurrency), asyncio.Lock()
    completed = state['completed']
    async def persist():
        if on_checkpoint:
            result = on_checkpoint(deepcopy(state))
            if inspect.isawaitable(result): await result
    async def block(steps, context, prefix, *, parallel=True):
        scope = {**context, 'steps': deepcopy(context.get('steps', {}))}
        results = {}
        for step in steps:
            path = f'{prefix}/{step["id"]}'
            if path in completed:
                output = deepcopy(completed[path])
            elif not evaluate(step.get('when', True), scope):
                output = {'skipped': True}
            else:
                async with lock:
                    if state['steps'] >= max_steps: raise RecipeError('Recipe step budget exhausted')
                    state['steps'] += 1
                if 'tool' in step:
                    args = evaluate(step.get('inputs', {}), scope)
                    if not isinstance(args, dict): raise RecipeError('Tool input must be an object')
                    async with semaphore:
                        output = await call(step['tool'], args, f'{identity}:{path}')
                elif 'value' in step:
                    output = evaluate(step['value'], scope)
                elif 'items' in step:
                    items = evaluate(step['items'], scope)
                    if not isinstance(items, list) or len(items) > max_steps:
                        raise RecipeError('Loop input must be a bounded list')
                    async def iteration(index):
                        child = {**scope, 'item': items[index], 'index': index}
                        result = await block(step['steps'], child, f'{path}/{index}', parallel=False)
                        return evaluate(step['output'], {**child, 'steps': {**scope['steps'], **result}}) if 'output' in step else result
                    output = [None] * len(items)
                    # Only one loop level fans out; nested loops stay sequential.
                    async def worker(offset, width):
                        for index in range(offset, len(items), width): output[index] = await iteration(index)
                    width = min(max_concurrency, len(items)) if parallel else min(1, len(items))
                    workers = [asyncio.create_task(worker(offset, width)) for offset in range(width)]
                    try:
                        if workers: await asyncio.gather(*workers)
                    except BaseException:
                        for task in workers: task.cancel()
                        await asyncio.gather(*workers, return_exceptions=True)
                        raise
                else:
                    output = None
                    matched = False
                    for index in range(step['repeat']):
                        child = {**scope, 'index': index, 'previous': output}
                        output = await block(step['steps'], child, f'{path}/{index}', parallel=False)
                        if evaluate(step.get('until', True), {**child, 'steps': {**scope['steps'], **output}, 'output': output}):
                            matched = True
                            break
                    if not matched: raise RecipeError('Bounded repeat did not satisfy until')
                if not all(evaluate(check, {**scope, 'output': output}) is True for check in step.get('checks', [])):
                    raise RecipeError(f'Step check failed: {step["id"]}')
                async with lock:
                    if _size(output) > max_output_bytes: raise RecipeError('Step output exceeds byte budget')
                    completed[path] = deepcopy(output)
                    if _size(state) > max_output_bytes:
                        del completed[path]
                        raise RecipeError('Checkpoint exceeds byte budget')
                    await persist()
            results[step['id']] = output
            scope['steps'][step['id']] = output
        return results
    results = await block(program['steps'], {'inputs': inputs, 'steps': {}}, 'root')
    output = evaluate(program.get('output', {'$ref': 'steps'}), {'inputs': inputs, 'steps': results})
    if _size(output) > max_output_bytes: raise RecipeError('Recipe output exceeds byte budget')
    return {'output': output, 'checkpoint': deepcopy(state), 'program_digest': validated['digest'],
            'model_calls': 0, 'validation_strength': 'program_checks'}


class RecipeCatalog:
    """Promotion is a host-verifier decision, not a model-writable success flag."""
    collection = 'workflow.recipe'
    required_cases = 5

    def __init__(self, store):
        self.store = store

    @staticmethod
    def contract(*, tools, source, validator, access):
        if not all(isinstance(value, (dict, str)) and value for value in (tools, source, validator, access)):
            raise RecipeError('Reuse requires explicit tool, source, validator and access contracts')
        return canonical_digest({'tools': tools, 'source': source, 'validator': validator, 'access': access})

    def propose(self, program, *, contract, description='', owner=''):
        checked = validate_program(program)
        if not isinstance(contract, str) or not re.fullmatch('[a-f0-9]{64}', contract):
            raise RecipeError('Expected a canonical contract digest')
        ident = canonical_digest({'program': checked['digest'], 'contract': contract, 'owner': owner})
        with self.store._tx() as conn:
            row = self.store._doc(conn, self.collection, ident)
            if row: return row['data']
            value = {'id': ident, 'program': deepcopy(program), 'program_digest': checked['digest'],
                     'contract': contract, 'owner': owner, 'description': str(description)[:1000],
                     'tools': checked['tools'], 'status': 'candidate', 'cases': {}, 'reuses': 0}
            self.store._put(conn, self.collection, ident, value)
            return value

    async def verify(self, ident, *, inputs, evidence, verifier):
        """Host code passes the registered independent verifier; never expose this as a tool."""
        row = self.store.get_document(self.collection, ident)
        if not row: raise KeyError(ident)
        decision = verifier(deepcopy(inputs), deepcopy(evidence))
        if inspect.isawaitable(decision): decision = await decision
        if not isinstance(decision, dict) or type(decision.get('passed')) is not bool or not decision.get('validator_digest'):
            raise RecipeError('Independent verifier must report a verdict and version digest')
        case = decision.get('case_digest')
        if not isinstance(case, str) or not re.fullmatch('[a-f0-9]{64}', case):
            raise RecipeError('Independent verifier must identify the actual observed input case')
        with self.store._tx() as conn:
            state = self.store._doc(conn, self.collection, ident)['data']
            state['cases'][case] = {'passed': decision['passed'], 'evidence_digest': canonical_digest(evidence),
                                    'validator_digest': decision['validator_digest']}
            if not decision['passed']:
                state['status'] = 'invalidated'
                state['invalidation_reason'] = 'independent_verification_failed'
            elif state['status'] != 'invalidated':
                passed = sum(value['passed'] for value in state['cases'].values())
                state['status'] = 'promoted' if passed >= self.required_cases else 'candidate'
            self.store._put(conn, self.collection, ident, state)
            return state

    def find(self, *, contract, owner='', tools=None):
        return [row for row in self.store.list_documents(self.collection)
                if row.get('contract') == contract and row.get('owner') == owner and row.get('status') == 'promoted'
                and (tools is None or set(row['tools']) <= set(tools))]

    def can_replay(self, ident, *, contract, owner=''):
        row = self.store.get_document(self.collection, ident)
        return bool(row and row.get('status') == 'promoted' and row.get('contract') == contract and row.get('owner') == owner)

    def reused(self, ident, *, contract, owner=''):
        with self.store._tx() as conn:
            row = self.store._doc(conn, self.collection, ident)
            if not row or row['data'].get('status') != 'promoted' or row['data'].get('contract') != contract or row['data'].get('owner') != owner:
                raise RecipeError('Recipe is not promoted under the current contract')
            state = row['data']; state['reuses'] += 1
            self.store._put(conn, self.collection, ident, state)
            return state

    def invalidate(self, ident, reason):
        with self.store._tx() as conn:
            row = self.store._doc(conn, self.collection, ident)
            if not row: raise KeyError(ident)
            state = row['data']; state.update(status='invalidated', invalidation_reason=str(reason)[:200])
            self.store._put(conn, self.collection, ident, state)
            return state
