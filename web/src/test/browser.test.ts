import {describe,it,expect} from 'vitest';
import {canControl,framePoint} from '../lib/browser';
import {safeUrl} from '../lib/format';
import type {BrowserFrame} from '@ore/sdk';
describe('remote browser input',()=>{
 it('maps a scaled frame back to original image coordinates',()=>{expect(framePoint(350,200,{left:100,top:100,width:500,height:250},{width:1000,height:500})).toEqual({x:500,y:200});});
 it('rejects input outside the frame or a collapsed element',()=>{expect(framePoint(0,0,{left:10,top:10,width:100,height:100},{width:100,height:100})).toBeNull();expect(framePoint(0,0,{left:0,top:0,width:0,height:0},{width:100,height:100})).toBeNull();});
 it('never sends human input during agent ownership or a closed connection',()=>{const frame={control:'agent'} as BrowserFrame;expect(canControl(frame,1)).toBe(false);expect(canControl({...frame,control:'human'},3)).toBe(false);expect(canControl({...frame,control:'human'},1)).toBe(true);});
 it('does not turn untrusted javascript or file links into navigation',()=>{expect(safeUrl('javascript:alert(1)')).toBeUndefined();expect(safeUrl('file:///etc/passwd')).toBeUndefined();expect(safeUrl('https://example.org/a.pdf')).toBe('https://example.org/a.pdf');});
});
