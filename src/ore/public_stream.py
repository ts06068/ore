"""Decode only declared public reply fields from incremental structured decisions.

The provider transport and nested JSON are untrusted framing. Reasoning, tool inputs,
provider events and incomplete escapes are never emitted as public text.
"""
from __future__ import annotations
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class StreamDecodeError(ValueError):
    pass


def _string(text, offset):
    if offset >= len(text) or text[offset] != '"':
        raise StreamDecodeError('Expected a JSON string')
    result=[]; index=offset+1
    while index < len(text):
        char=text[index]
        if char=='"': return ''.join(result), index+1, True
        if char=='\\':
            if index+1 >= len(text): break
            escape=text[index+1]
            if escape=='u':
                if index+6 > len(text): break
                try: value=int(text[index+2:index+6],16)
                except ValueError as exc: raise StreamDecodeError('Invalid Unicode escape') from exc
                index+=6
                if 0xD800 <= value <= 0xDBFF:
                    if index+6 > len(text): break
                    if text[index:index+2]!='\\u': raise StreamDecodeError('Missing low surrogate')
                    try: low=int(text[index+2:index+6],16)
                    except ValueError as exc: raise StreamDecodeError('Invalid low surrogate') from exc
                    if not 0xDC00 <= low <= 0xDFFF: raise StreamDecodeError('Invalid low surrogate')
                    value=0x10000+((value-0xD800)<<10)+(low-0xDC00);index+=6
                elif 0xDC00 <= value <= 0xDFFF: raise StreamDecodeError('Unpaired low surrogate')
                result.append(chr(value));continue
            mapping={'"':'"','\\':'\\','/':'/','b':'\b','f':'\f','n':'\n','r':'\r','t':'\t'}
            if escape not in mapping: raise StreamDecodeError('Invalid JSON escape')
            result.append(mapping[escape]);index+=2;continue
        if ord(char)<32 or 0xD800 <= ord(char) <= 0xDFFF: raise StreamDecodeError('Invalid JSON character')
        result.append(char);index+=1
    return ''.join(result),len(text),False


def root_strings(text):
    """Return complete/partial root string fields, never nested unrelated values."""
    index=0;fields={};decoder=json.JSONDecoder()
    def space(pos):
        while pos<len(text) and text[pos].isspace():pos+=1
        return pos
    index=space(index)
    if index==len(text):return fields
    if text[index]!='{':raise StreamDecodeError('Structured decision must be a JSON object')
    index+=1
    while True:
        index=space(index)
        if index>=len(text) or text[index]=='}':return fields
        key,index,complete=_string(text,index)
        if not complete:return fields
        if key in fields:raise StreamDecodeError('Duplicate decision field')
        index=space(index)
        if index>=len(text):return fields
        if text[index]!=':':raise StreamDecodeError('Missing object colon')
        index=space(index+1)
        if index>=len(text):return fields
        if text[index]=='"':
            value,index,complete=_string(text,index);fields[key]=(value,complete)
            if not complete:return fields
        else:
            try:_,index=decoder.raw_decode(text,index)
            except json.JSONDecodeError:return fields
            fields[key]=(None,True)
        index=space(index)
        if index>=len(text) or text[index]=='}':return fields
        if text[index]!=',':raise StreamDecodeError('Missing object separator')
        index+=1


_ASSIGN=re.compile(r'''(?ix)(\b(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|authorization|cookie|set-cookie)\b["']?\s*[:=]\s*)(?:"[^"\n]*(?:"|$)|'[^'\n]*(?:'|$)|[^\s,;}]+)''')
_BEARER=re.compile(r'(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9_+./=-]+')
_KEY=re.compile(r'\b(?:sk-[A-Za-z0-9_-]{8,}|xox[baprs]-[A-Za-z0-9-]+)')
_URL=re.compile(r'https?://[^\s<>"\x27]+')


def public_text(text):
    def clean_url(match):
        try:
            url=urlsplit(match.group());host=url.netloc.rsplit('@',1)[-1]
            query=[(key,'[redacted]' if any(part in key.lower() for part in ('key','token','signature','credential','auth','__cf_chl')) else value) for key,value in parse_qsl(url.query,keep_blank_values=True)]
            return urlunsplit((url.scheme,host,url.path,urlencode(query),''))
        except ValueError:return '[redacted URL]'
    text=_URL.sub(clean_url,text)
    text=_BEARER.sub('[redacted]',text)
    text=_ASSIGN.sub(lambda match:match.group(1)+'[redacted]',text)
    return _KEY.sub('[redacted]',text)


class PublicDecisionStream:
    """Incremental public field decoder with cross-chunk credential masking."""
    def __init__(self, callback, *, max_bytes=2_000_000):
        self.callback=callback;self.raw='';self.emitted='';self.tool=None;self.field=None
        self.max_bytes=max_bytes;self.failed=False

    def feed(self, delta):
        if self.failed:return
        self.raw+=delta
        if len(self.raw.encode('utf-8'))>self.max_bytes:
            self.failed=True;return
        try:
            outer=root_strings(self.raw)
            tool,complete=outer.get('tool',(None,False))
            if not complete or tool not in ('respond','ask','propose_plan'):return
            arguments=outer.get('arguments',(None,False))[0]
            if not isinstance(arguments,str):return
            field='summary' if tool=='propose_plan' else 'content'
            content,closed=root_strings(arguments).get(field,(None,False))
            if content is None:return
            self.tool,self.field=tool,field
            # Retain four trailing lexical units so split credential labels/values
            # and signed URLs cannot cross a publication boundary.
            words=list(re.finditer(r'\S+',content))
            cut=len(content) if closed else words[-4].start() if len(words)>4 else 0
            for pattern in (_ASSIGN,_BEARER,_KEY,_URL):
                for match in pattern.finditer(content):
                    if match.start()<cut<match.end():cut=match.start()
            self._publish(public_text(content[:cut]))
        except (ValueError,TypeError):
            self.failed=True

    def _publish(self, value):
        if not value.startswith(self.emitted):
            self.failed=True;return
        delta=value[len(self.emitted):]
        if delta:
            self.emitted=value
            self.callback(delta, self.tool, self.field)

    def finish(self, decision):
        if self.failed or not self.raw:return
        tool=decision.get('tool');args=decision.get('arguments',{})
        if isinstance(args,str):args=json.loads(args)
        if tool not in ('respond','ask','propose_plan'):return
        self.tool=tool;self.field='summary' if tool=='propose_plan' else 'content'
        value=args.get(self.field)
        if isinstance(value,str):self._publish(public_text(value))
