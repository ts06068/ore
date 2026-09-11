"""Fixed JSON/stdin X11 control protocol; no shell, browser debugger, or DOM."""
import json
import math
import os
import subprocess
import sys
import time
from urllib.parse import urlsplit

WIDTH = int(os.environ.get('ORE_DESKTOP_WIDTH', '1280'))
HEIGHT = int(os.environ.get('ORE_DESKTOP_HEIGHT', '800'))
MAX_TEXT = 16384
KEYS = {'Return', 'Tab', 'Escape', 'BackSpace', 'Delete', 'Left', 'Right', 'Up', 'Down',
        'Home', 'End', 'Page_Up', 'Page_Down', 'space', 'F5', 'ctrl+l', 'ctrl+a', 'ctrl+c',
        'ctrl+v', 'ctrl+x', 'ctrl+z', 'ctrl+f', 'ctrl+r', 'ctrl+t', 'ctrl+w', 'ctrl+Tab',
        'ctrl+shift+Tab', 'shift+Tab', 'alt+Left', 'alt+Right'}
ALIASES = {'Enter':'Return', 'Control':'ctrl', 'ControlOrMeta':'ctrl', 'Ctrl':'ctrl', 'PageUp':'Page_Up',
           'PageDown':'Page_Down', 'Space':'space', 'ArrowLeft':'Left', 'ArrowRight':'Right',
           'ArrowUp':'Up', 'ArrowDown':'Down', 'Shift':'shift', 'Alt':'alt'}

def run(args, *, data=None, timeout=10):
    return subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=True).stdout

def window():
    result = run(['xdotool','search','--onlyvisible','--class','google-chrome']).decode().splitlines()
    if not result:
        raise ValueError('Chrome window unavailable')
    return result[-1]

def focus():
    wid=window()
    run(['xdotool','windowactivate','--sync',wid])
    return wid

def key(value):
    if not isinstance(value,str):
        raise ValueError('Invalid key')
    normalized='+'.join(ALIASES.get(part,part) for part in value.split('+'))
    if '+' in normalized and len(normalized.rsplit('+',1)[-1])==1:
        normalized=normalized[:-1]+normalized[-1].lower()
    if normalized not in KEYS:
        raise ValueError('Unsupported desktop key')
    run(['xdotool','key','--clearmodifiers',normalized])

def type_text(value):
    if not isinstance(value,str) or len(value)>MAX_TEXT or '\x00' in value:
        raise ValueError('Invalid desktop text')
    # Text remains on stdin, never in process arguments. Clipboard is private to this container.
    subprocess.run(['xclip','-selection','clipboard','-in'],input=value.encode(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5,check=True)
    key('ctrl+v')

def clear_clipboard():
    subprocess.run(['xclip','-selection','clipboard','-in'],input=b'',stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5,check=True)

def current_url():
    clear_clipboard()
    key('ctrl+l');key('ctrl+c')
    result = None
    try:
        # Chrome updates the clipboard asynchronously. Poll the read, not input
        # or navigation, so a fresh window cannot become a false URL failure.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                value = run(['xclip','-selection','clipboard','-out'], timeout=.25).decode('utf-8','replace').strip()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                value = ''
            parsed = urlsplit(value)
            if value == 'about:blank' or parsed.scheme in {'http','https'} and parsed.hostname and not parsed.username:
                result = value
                break
            time.sleep(.05)
    finally:
        # Ctrl+F6 restores web contents and its focused field. Escape could
        # cancel page loading and is deliberately absent from observation.
        run(['xdotool','key','--clearmodifiers','ctrl+F6'])
        clear_clipboard()
    return result

def screenshot():
    data=run(['import','-window','root','png:-'],timeout=15)
    if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data)>16*1024*1024:
        raise ValueError('Invalid desktop screenshot')
    return data

def main(payload):
    action=payload.get('action');args=payload.get('args') or {}
    if action=='ready':
        return {'ready':bool(window())}
    if action=='screenshot':
        return screenshot()
    if action=='observe':
        wid=focus()
        url=current_url() if args.get('read_url',True) else None
        title=run(['xdotool','getwindowname',wid]).decode('utf-8','replace').strip()[:2048]
        png=screenshot()
        text=''
        if args.get('include_text',True):
            text=run(['tesseract','stdin','stdout','--psm','11'],data=png,timeout=20).decode('utf-8','replace')[:32768]
        import base64
        return {'png_base64':base64.b64encode(png).decode(),'url':url,'url_observed':bool(url),
                'title':title,'text':text,'width':WIDTH,'height':HEIGHT,'text_method':'screenshot_ocr'}
    focus()
    if action=='current_url':
        return {'url':current_url()}
    if action=='navigate':
        url=args.get('url')
        p=urlsplit(url) if isinstance(url,str) else None
        if not p or p.scheme not in {'http','https'} or not p.hostname or p.username or p.password or len(url)>8192:
            raise ValueError('Only HTTP(S) navigation without userinfo is supported')
        key('ctrl+l');type_text(url);key('Return');clear_clipboard()
    elif action=='click':
        x,y=args.get('x'),args.get('y')
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (x,y)) or not 0<=x<WIDTH or not 0<=y<HEIGHT:
            raise ValueError('Click outside desktop')
        buttons={'left':'1','middle':'2','right':'3'}
        button=buttons.get(args.get('button','left'))
        if button is None:raise ValueError('Invalid mouse button')
        # X11 processes these ordered requests without waiting for a motion event.
        # --sync can hang when the pointer is already at the requested coordinates.
        run(['xdotool','mousemove',str(int(x)),str(int(y)),'click',button])
    elif action in {'type','fill'}:
        type_text(args.get('text',args.get('value')))
    elif action in {'press','key'}:
        key(args.get('key'))
    elif action=='scroll':
        for axis,positive,negative in [('delta_y','5','4'),('delta_x','7','6')]:
            delta=args.get(axis,0)
            if isinstance(delta,bool) or not isinstance(delta,(int,float)) or not math.isfinite(delta):
                raise ValueError('Invalid scroll distance')
            steps=min(20,max(0,int(abs(delta)/80)))
            if steps:run(['xdotool','click','--repeat',str(steps),'--delay','20',positive if delta>0 else negative])
    else:
        raise ValueError('Unsupported desktop action')
    return {'ok':True}

if __name__=='__main__':
    try:
        data=sys.stdin.buffer.read(65537)
        if len(data)>65536:raise ValueError('Desktop command too large')
        result=main(json.loads(data))
        sys.stdout.buffer.write(result if isinstance(result,bytes) else json.dumps(result).encode())
    except Exception as exc:
        # Do not print submitted text, URLs, clipboard contents, or subprocess arguments.
        sys.stderr.write(type(exc).__name__+': desktop command failed\n')
        raise SystemExit(2)
