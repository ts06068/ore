"""Bounded planner context with explicit, conversation-scoped read handles."""
from __future__ import annotations
import json
from .models import canonical_digest


class PlannerContext:
    def __init__(self, store, conversation_id, *, limit=65536):
        self.store, self.owner, self.limit = store, conversation_id, limit

    def pack(self, value):
        # Retain small structure verbatim; large values remain retrievable, never
        # silently summarized away. References are pure data, not authorization.
        def handle(item):
            raw=json.dumps(item,ensure_ascii=False,default=str)
            ident=canonical_digest({'owner':self.owner,'text':raw})
            self.store.put_document('conversation.context',ident,{'id':ident,'owner':self.owner,'text':raw})
            return {'context_ref':ident,'chars':len(raw),'bytes':len(raw.encode()),
                    'read_with':'inspect_context {context_ref, offset, limit}'}
        def walk(item,limit):
            raw=json.dumps(item,ensure_ascii=False,default=str)
            if len(raw.encode())<=limit:return item
            if isinstance(item,dict) and len(item)<30:
                candidate={key:walk(child,max(1200,limit//max(1,len(item)))) for key,child in item.items()}
                if len(json.dumps(candidate,ensure_ascii=False).encode())<=limit:return candidate
            return handle(item)
        return walk(value,self.limit)

    def read(self, ref, offset=0, limit=12000):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=12000:
            raise ValueError('Context read requires nonnegative offset and limit 1..12000')
        row=self.store.get_document('conversation.context',ref)
        if not row or row.get('owner')!=self.owner:raise KeyError('Unknown context reference')
        text=row['text'];chunk=text[offset:offset+limit]
        return {'context_ref':ref,'text':chunk,'offset':offset,'next_offset':offset+len(chunk),
                'total_chars':len(text),'has_more':offset+len(chunk)<len(text)}
