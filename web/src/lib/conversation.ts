import type {Conversation,ConversationEvent,ConversationMessage,ConversationSummary,PlanRevision,WorkflowRun} from '@ore/sdk';
import {record,text} from './format';

/** Replay only public response deltas. Provider reasoning never enters this reducer. */
export function applyPublicMessageEvent(current:Conversation|null,event:ConversationEvent):Conversation|null{
 if(!current||!['message.delta','message.complete','message'].includes(event.type))return current;
 const data=record(event.data);const id=text(data.message_id??data.id,'');if(!id)return current;
 const messages=[...current.messages];const index=messages.findIndex(message=>message.id===id);const existing=messages[index];
 const sequence=typeof data.sequence==='number'?data.sequence:0;const previous=typeof existing?.sequence==='number'?existing.sequence:0;
 if(event.type==='message.delta'){
  if(sequence<=previous||sequence>previous+1)return current; // A snapshot repairs missing deltas.
  const message:ConversationMessage={...existing,id,role:'assistant',content:(existing?.content??'')+text(data.delta,''),sequence,status:'streaming',operator_message_id:typeof data.operator_message_id==='string'?data.operator_message_id:existing?.operator_message_id,created_at:existing?.created_at??event.created_at};
  if(index<0)messages.push(message);else messages[index]=message;
 }else{
  if(existing&&sequence<previous)return current;
  const message:ConversationMessage={...existing,...data,id,role:(data.role??existing?.role??'assistant') as ConversationMessage['role'],content:text(data.content,existing?.content??''),status:(['streaming','interrupted','failed'].includes(text(data.status,''))?data.status:'complete') as ConversationMessage['status'],sequence:Math.max(previous,sequence),created_at:existing?.created_at??event.created_at};
  if(index<0)messages.push(message);else messages[index]=message;
 }
 return {...current,messages,events_cursor:Math.max(Number(current.events_cursor??0),Number(event.id))};
}
export interface ConversationTurn{id:string;user?:ConversationMessage;messages:ConversationMessage[];plans:PlanRevision[];runs:WorkflowRun[];events:ConversationEvent[];legacy?:boolean;}
export function conversationTurns(conversation:Conversation|null,events:ConversationEvent[]):ConversationTurn[]{
 if(!conversation)return [];
 const users=conversation.messages.filter(message=>message.role==='user');
 const turns:ConversationTurn[]=users.map(user=>({id:user.id,user,messages:[],plans:[],runs:[],events:[]}));
 const legacy:ConversationTurn={id:'earlier-activity',messages:[],plans:[],runs:[],events:[],legacy:true};
 const byId=new Map(turns.map(turn=>[turn.id,turn]));
 function owner(item:Record<string,unknown>):ConversationTurn{
  const explicit=text(item.operator_message_id??item.turn_id,'');if(explicit&&byId.has(explicit))return byId.get(explicit)!;
  if(turns.length===1)return turns[0]!;
  if(typeof item.created_at==='string'){
   const stamp=Date.parse(item.created_at);const user=[...users].reverse().find(message=>message.created_at&&Date.parse(message.created_at)<=stamp);
   if(user)return byId.get(user.id)!;
  }
  return legacy;
 }
 const planOwners=new Map<string,ConversationTurn>();const runOwners=new Map<string,ConversationTurn>();
 for(const message of conversation.messages){if(message.role==='user'||!['assistant','tool'].includes(message.role))continue;const turn=owner(message);turn.messages.push(message);if(typeof message.plan_id==='string')planOwners.set(message.plan_id,turn);}
 for(const plan of conversation.plans??[]){const turn=planOwners.get(plan.id)??owner(plan);turn.plans.push(plan);planOwners.set(plan.id,turn);}
 for(const run of conversation.runs??[]){const turn=planOwners.get(text(run.plan_id,''))??owner(run);turn.runs.push(run);runOwners.set(run.id,turn);}
 for(const event of events){if(['created','message','message.delta','message.complete','clarification','plan.proposed','plan.approved'].includes(event.type))continue;const data=record(event.data);const turn=runOwners.get(text(data.run_id,''))??planOwners.get(text(data.plan_id,''))??owner({...data,operator_message_id:data.operator_message_id??event.operator_message_id,created_at:event.created_at});turn.events.push(event);}
 if(legacy.messages.length||legacy.plans.length||legacy.runs.length||legacy.events.length)turns.unshift(legacy);
 return turns;
}

export interface ConversationBranch {conversation:ConversationSummary;children:ConversationBranch[];}

/** Build one folder's branch tree; absent parents stay visible as roots. */
export function conversationBranches(conversations:ConversationSummary[]):ConversationBranch[]{
 const nodes=new Map(conversations.map(conversation=>[conversation.id,{conversation,children:[]} as ConversationBranch]));
 const parents=new Map<string,string>();
 for(const item of conversations){const parent=item.branched_from?.conversation_id;if(parent&&parent!==item.id&&nodes.has(parent))parents.set(item.id,parent);}
 // Imported or stale lineage must never hide chats in a cycle or recurse forever.
 for(const id of [...nodes.keys()].sort()){
  const seen=new Set<string>();let current:string|undefined=id;
  while(current&&parents.has(current)){if(seen.has(current)){parents.delete(current);break;}seen.add(current);current=parents.get(current);}
 }
 const roots:ConversationBranch[]=[];
 for(const node of nodes.values()){const parent=parents.get(node.conversation.id);if(parent)nodes.get(parent)!.children.push(node);else roots.push(node);}
 return roots;
}
