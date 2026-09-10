import type { BrowserFrame } from '@ore/sdk';
export function framePoint(clientX:number,clientY:number,rect:Pick<DOMRect,'left'|'top'|'width'|'height'>,frame:Pick<BrowserFrame,'width'|'height'>):{x:number;y:number}|null {
  if(rect.width<=0||rect.height<=0||clientX<rect.left||clientY<rect.top||clientX>rect.left+rect.width||clientY>rect.top+rect.height)return null;
  return {x:Math.max(0,Math.min(frame.width-1,Math.floor((clientX-rect.left)/rect.width*frame.width))),y:Math.max(0,Math.min(frame.height-1,Math.floor((clientY-rect.top)/rect.height*frame.height)))};
}
export function canControl(frame:BrowserFrame|null,readyState:number):boolean{return !!frame&&frame.control==='human'&&readyState===1;}
