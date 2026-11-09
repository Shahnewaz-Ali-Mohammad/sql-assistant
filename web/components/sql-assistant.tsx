'use client';

import { useEffect, useMemo, useState } from 'react';
import { Check, ChevronDown, CircleHelp, Clipboard, Database, KeyRound, LoaderCircle, MessageSquareText, PlugZap, Send, ShieldCheck, Sparkles, Table2, X, Plus, Pencil, MessageSquarePlus } from 'lucide-react';

type ColumnInfo = { name:string; type:string; nullable:boolean; key:string|null };
type TableInfo = { full_name:string; schema:string; name:string; columns:ColumnInfo[] };
type Workspace = { id:string; name:string; database_type:string; database:string; table_count?:number; relationship_count?:number };
type Chat = { id:string; title:string; updated_at:string };
type SqlDialect = 'mysql'|'postgresql'|'sqlserver';
type ChatMessage = { id?:number; role:'user'|'assistant'; content:string; sql?:string|null; explanation?:string|null; outcome?:string; dialect?:SqlDialect|null; notice?:string|null };
type TablePage = { tables:TableInfo[]; offset:number; limit:number; total:number; has_more:boolean };
type AskResult = { outcome:string; reply:string; sql?:string|null; explanation?:string|null; dialect?:SqlDialect; dialect_matches_workspace?:boolean; notice?:string|null };
const API=process.env.NEXT_PUBLIC_API_URL||'http://localhost:8000';
const fixedReply='I can help write SQL for the database connected to this workspace. Ask me for a query or describe the data you want.';
const starterQuestions=['Show me the 20 newest records','Count records by status','Show monthly totals for this year'];

async function request<T>(path:string,body?:unknown,method?:string):Promise<T>{
  const response=await fetch(API+path,{method:method||(body?'POST':'GET'),headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined});
  const payload=await response.json().catch(()=>({}));
  if(!response.ok) throw new Error(payload.detail||'Request failed.');
  return payload as T;
}
function dbTypeLabel(v:string){return v==='postgresql'?'PostgreSQL':v==='sqlserver'?'SQL Server':'MySQL / MariaDB';}

export function SqlAssistant(){
  const [workspaces,setWorkspaces]=useState<Workspace[]>([]);
  const [activeWorkspace,setActiveWorkspace]=useState<Workspace|null>(null);
  const [chats,setChats]=useState<Chat[]>([]);
  const [activeChat,setActiveChat]=useState<Chat|null>(null);
  const [tables,setTables]=useState<TableInfo[]>([]);
  const [tableOffset,setTableOffset]=useState(0);
  const [tableTotal,setTableTotal]=useState(0);
  const [hasMoreTables,setHasMoreTables]=useState(false);
  const [expandedTable,setExpandedTable]=useState<string|null>(null);
  const [connectionOpen,setConnectionOpen]=useState(false);
  const [databaseUrl,setDatabaseUrl]=useState('');
  const [connecting,setConnecting]=useState(false);
  const [question,setQuestion]=useState('');
  const [messages,setMessages]=useState<ChatMessage[]>([]);
  const [asking,setAsking]=useState(false);
  const [copied,setCopied]=useState<number|null>(null);
  const [error,setError]=useState('');
  const [health,setHealth]=useState<{configured:boolean;store_configured:boolean}|null>(null);
  const [loadingWorkspace,setLoadingWorkspace]=useState(false);
  const [loadingMessages,setLoadingMessages]=useState(false);

  useEffect(()=>{void loadInitial();},[]);
  async function loadInitial(){
    try{
      const h=await request<{configured:boolean;store_configured:boolean}>('/api/health');
      setHealth(h);
      if(!h.store_configured)return;
      const w=await request<{workspaces:Workspace[]}>('/api/workspaces');
      setWorkspaces(w.workspaces);
      if(w.workspaces.length) setActiveWorkspace(w.workspaces[0]);
    }catch(e){setError((e as Error).message);}
  }

  useEffect(()=>{
    if(!activeWorkspace){setTables([]);setChats([]);setActiveChat(null);setMessages([]);return;}
    let alive=true;
    setLoadingWorkspace(true);setError('');setActiveChat(null);setMessages([]);setExpandedTable(null);
    Promise.all([
      request<TablePage>('/api/workspaces/'+activeWorkspace.id+'/tables?offset=0&limit=10'),
      request<{chats:Chat[]}>('/api/workspaces/'+activeWorkspace.id+'/chats')
    ]).then(async([page,chatData])=>{
      if(!alive)return;
      setTables(page.tables);setTableOffset(page.tables.length);setTableTotal(page.total);setHasMoreTables(page.has_more);setChats(chatData.chats);
      if(chatData.chats.length) setActiveChat(chatData.chats[0]);
      else{
        const created=await request<Chat>('/api/workspaces/'+activeWorkspace.id+'/chats',{title:'New chat'});
        if(alive){setChats([created]);setActiveChat(created);}
      }
    }).catch(e=>{if(alive)setError((e as Error).message);}).finally(()=>{if(alive)setLoadingWorkspace(false);});
    return()=>{alive=false;};
  },[activeWorkspace?.id]);

  useEffect(()=>{
    if(!activeWorkspace||!activeChat){setMessages([]);return;}
    let alive=true;setLoadingMessages(true);
    request<{messages:ChatMessage[]}>('/api/workspaces/'+activeWorkspace.id+'/chats/'+activeChat.id+'/messages')
      .then(r=>{if(alive)setMessages(r.messages);}).catch(e=>{if(alive)setError((e as Error).message);}).finally(()=>{if(alive)setLoadingMessages(false);});
    return()=>{alive=false;};
  },[activeWorkspace?.id,activeChat?.id]);

  const schemaLabel=useMemo(()=>activeWorkspace?tableTotal+' tables · '+(activeWorkspace.relationship_count??0)+' relationships':'No database connected',[activeWorkspace,tableTotal]);
  async function connect(event:React.FormEvent){
    event.preventDefault();if(!databaseUrl.trim())return;
    setConnecting(true);setError('');
    try{
      const result=await request<{workspaces:Workspace[];discovered:number;created:number;updated:number;skipped:string[]}>('/api/workspaces/connect-server',{connection_url:databaseUrl.trim()});
      setWorkspaces(current=>[...result.workspaces,...current.filter(old=>!result.workspaces.some(next=>next.id===old.id))]);
      if(result.workspaces.length)setActiveWorkspace(result.workspaces[0]);
      setDatabaseUrl('');setConnectionOpen(false);
      const skipped=result.skipped.length?` ${result.skipped.length} database(s) could not be loaded: ${result.skipped.join(', ')}.`:'';
      setError(`Connected ${result.workspaces.length} database workspace(s)${result.created?`; ${result.created} new`:''}${result.updated?`; ${result.updated} refreshed`:''}.${skipped}`);
    }catch(e){setError((e as Error).message);}finally{setConnecting(false);}
  }
  async function loadMoreTables(){
    if(!activeWorkspace||!hasMoreTables)return;
    try{
      const page=await request<TablePage>('/api/workspaces/'+activeWorkspace.id+'/tables?offset='+tableOffset+'&limit=10');
      setTables(current=>[...current,...page.tables]);setTableOffset(tableOffset+page.tables.length);setHasMoreTables(page.has_more);
    }catch(e){setError((e as Error).message);}
  }
  async function newChat(){
    if(!activeWorkspace)return;
    try{
      const created=await request<Chat>('/api/workspaces/'+activeWorkspace.id+'/chats',{title:'New chat'});
      setChats(current=>[created,...current]);setActiveChat(created);setMessages([]);
    }catch(e){setError((e as Error).message);}
  }
  async function renameWorkspace(w:Workspace){
    const name=window.prompt('Workspace name',w.name)?.trim();if(!name||name===w.name)return;
    try{
      const updated=await request<Workspace>('/api/workspaces/'+w.id,{name},'PATCH');
      setWorkspaces(current=>current.map(x=>x.id===w.id?{...x,...updated}:x));
      if(activeWorkspace?.id===w.id)setActiveWorkspace({...w,...updated});
    }catch(e){setError((e as Error).message);}
  }
  async function ask(value=question){
    const text=value.trim();if(!activeWorkspace||!activeChat||!text||asking)return;
    setQuestion('');setAsking(true);setError('');
    try{
      const dialect=activeWorkspace.database_type as SqlDialect;
      const result=await request<AskResult>('/api/workspaces/'+activeWorkspace.id+'/chats/'+activeChat.id+'/ask',{question:text});
      setMessages(current=>[...current,{role:'user',content:text,dialect},{role:'assistant',content:result.reply,sql:result.sql,explanation:result.explanation,outcome:result.outcome,dialect:result.dialect,notice:result.notice}]);
      const chatsNow=await request<{chats:Chat[]}>('/api/workspaces/'+activeWorkspace.id+'/chats');
      setChats(chatsNow.chats);
    }catch(e){setMessages(current=>[...current,{role:'user',content:text},{role:'assistant',content:(e as Error).message,outcome:'error'}]);}
    finally{setAsking(false);}
  }
  async function copySql(sql:string,index:number){await navigator.clipboard.writeText(sql);setCopied(index);window.setTimeout(()=>setCopied(null),1600);}

  return <main className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Database size={20}/></div><div><strong>SQL Assistant</strong><span>Database query studio</span></div></div>
      <div className="side-heading"><div className="side-label">WORKSPACES</div><button className="small-add" onClick={()=>{setError('');setConnectionOpen(true);}} title="Add workspace"><Plus size={16}/></button></div>
      <div className="workspace-list">
        {workspaces.map(w=><div className={'workspace-entry '+(activeWorkspace?.id===w.id?'selected':'')} key={w.id}>
          <button className="workspace-select" onClick={()=>setActiveWorkspace(w)}><span className="connection-icon"><Database size={16}/></span><span className="connection-copy"><strong>{w.name}</strong><small>{dbTypeLabel(w.database_type)} · {w.database}</small></span></button>
          <button className="rename-workspace" title="Rename workspace" onClick={()=>void renameWorkspace(w)}><Pencil size={13}/></button>
        </div>)}
        {!workspaces.length&&<p className="empty-workspaces">No workspaces yet. Add a database to begin.</p>}
      </div>
      {activeWorkspace&&<><div className="side-heading schema-heading"><div className="side-label">TABLES <span className="count-pill">{tableTotal}</span></div></div>
        <div className="table-list">{tables.map(t=><div key={t.full_name}>
          <button className="table-item" onClick={()=>setExpandedTable(expandedTable===t.full_name?null:t.full_name)}><Table2 size={15}/><span>{t.full_name}</span><ChevronDown size={14} className={expandedTable===t.full_name?'rotated':''}/></button>
          {expandedTable===t.full_name&&<div className="column-list">{t.columns.map(c=><div key={c.name}><span>{c.name}</span><small>{c.type}{c.key==='PRI'?' · PK':''}</small></div>)}</div>}
        </div>)}</div>
        {hasMoreTables&&<button className="load-more-tables" onClick={()=>void loadMoreTables()}>Load 10 more tables</button>}
        <div className="side-heading chats-heading"><div className="side-label">CHATS</div><button className="small-add" onClick={()=>void newChat()} title="New chat"><MessageSquarePlus size={16}/></button></div>
        <div className="chat-list">{chats.map(c=><button key={c.id} className={'chat-select '+(activeChat?.id===c.id?'selected':'')} onClick={()=>setActiveChat(c)}><MessageSquareText size={14}/><span>{c.title}</span></button>)}</div>
      </>}
      <div className="sidebar-bottom"><div className="security-note"><ShieldCheck size={16}/><span>Generated queries are never run.<br/>Connection passwords are encrypted.</span></div><button className="manage-link" onClick={()=>setConnectionOpen(true)}>{activeWorkspace?'Add another workspace':'Connect a database'}<span>→</span></button></div>
    </aside>

    <section className="main-panel">
      <header className="topbar"><div className="crumb"><span>SQL Assistant</span><span className="slash">/</span><span>{activeWorkspace?.name||'New workspace'}</span></div><div className="topbar-right"><span className="model-status"><i className={health?.configured?'online':''}/>{health===null?'Checking AI':health.configured?'AI ready':'AI key needed'}</span><button className="top-connect" onClick={()=>{setError('');setConnectionOpen(true);}}><Plus size={16}/>Add workspace</button></div></header>
      <div className="chat-wrap">
        <div className="chat-heading"><div><div className="eyebrow"><Sparkles size={14}/> QUERY GENERATION</div><h1>{activeWorkspace?activeWorkspace.name:'Create a database workspace'}</h1><p>{activeWorkspace?dbTypeLabel(activeWorkspace.database_type)+' · '+schemaLabel:'Add a server connection URL. A database name in the URL selects one; leave it off to discover all accessible databases.'}</p></div>{activeWorkspace&&<div className="db-pill"><Database size={14}/><span>{activeWorkspace.database}</span></div>}</div>
        <div className="chat-scroll">
          {!activeWorkspace?<div className="welcome-card"><div className="welcome-icon"><Database size={23}/></div><h2>Start with a workspace</h2><p>Connect MySQL, PostgreSQL, or SQL Server. Put a database name at the end of the URL to connect to one database, or leave it off to discover all accessible databases. Tables load 10 at a time.</p><button className="primary-button" onClick={()=>setConnectionOpen(true)}><PlugZap size={17}/>Add workspace</button><div className="welcome-foot"><ShieldCheck size={14}/>Queries are generated only and are never executed.</div></div>
          :loadingWorkspace?<div className="loading-state"><LoaderCircle className="spin" size={17}/>Loading workspace…</div>
          :!activeChat?<div className="loading-state">Choose a chat or start a new one.</div>
          :loadingMessages?<div className="loading-state"><LoaderCircle className="spin" size={17}/>Loading saved conversation…</div>
          :messages.length===0?<div className="welcome-connected"><div className="assistant-avatar"><Sparkles size={17}/></div><div className="welcome-message"><span className="message-label">SQL ASSISTANT</span><p>Connected to <strong>{activeWorkspace.database}</strong>. I can see {schemaLabel}. Describe the information you want and I’ll write a {dbTypeLabel(activeWorkspace.database_type)} query.</p><div className="suggestions">{starterQuestions.map(s=><button key={s} onClick={()=>void ask(s)}>{s}<span>↗</span></button>)}</div></div></div>
          :<div className="message-list">{messages.map((m,index)=><div className={'message-row '+m.role} key={m.id||index}>
            {m.role==='assistant'&&<div className="assistant-avatar"><Sparkles size={16}/></div>}
            <article className={'message-bubble '+m.role}>
              {m.role==='assistant'&&<span className="message-label">{m.outcome==='sql'?'QUERY GENERATED':'SQL ASSISTANT'}</span>}
              <p>{m.content}</p>
              {m.sql&&<div className="sql-card"><div className="sql-toolbar"><span><i/> {dbTypeLabel(m.dialect||activeWorkspace.database_type)}</span><button onClick={()=>void copySql(m.sql!,index)}>{copied===index?<Check size={14}/>:<Clipboard size={14}/>}{copied===index?'Copied':'Copy SQL'}</button></div><pre><code>{m.sql}</code></pre></div>}
              {m.notice&&<div className="dialect-notice">{m.notice}</div>}
              {m.explanation&&<div className="explanation"><CircleHelp size={15}/><span>{m.explanation}</span></div>}
            </article>
            {m.role==='user'&&<div className="user-avatar">You</div>}
          </div>)}{asking&&<div className="message-row assistant"><div className="assistant-avatar"><Sparkles size={16}/></div><div className="thinking"><LoaderCircle size={15}/>Writing a query from the saved schema…</div></div>}</div>}
        </div>
        <form className="composer" onSubmit={e=>{e.preventDefault();void ask();}}><textarea value={question} onChange={e=>setQuestion(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();void ask();}}} placeholder={activeWorkspace?'Ask for a query, e.g. “Show monthly ticket totals for this year”':'Add a workspace to start asking questions'} disabled={!activeWorkspace||!activeChat||asking} rows={2}/><div className="composer-bottom"><div/><button className="send-button" disabled={!activeWorkspace||!activeChat||!question.trim()||asking}>{asking?<LoaderCircle className="spin" size={17}/>:<Send size={16}/>}Generate SQL</button></div></form>
        <div className="chat-foot"><span><ShieldCheck size={13}/> Generated SQL is never executed</span><span>{activeWorkspace?tableTotal+' tables in schema':'Create a workspace to begin'}</span></div>
      </div>
    </section>

    {connectionOpen&&<div className="modal-backdrop" onMouseDown={e=>{if(e.target===e.currentTarget)setConnectionOpen(false);}}><section className="connection-modal" role="dialog" aria-modal="true" aria-labelledby="connect-title"><header className="modal-header"><div><div className="eyebrow"><PlugZap size={14}/> NEW WORKSPACE</div><h2 id="connect-title">Connect a database server</h2><p>Paste the server URL. Include a database name at the end for one database, or leave it off to discover all accessible databases.</p></div><button className="icon-button" onClick={()=>setConnectionOpen(false)} aria-label="Close"><X size={19}/></button></header>
      <form className="connect-form" onSubmit={e=>void connect(e)}><label>Server connection URL<input required value={databaseUrl} onChange={e=>setDatabaseUrl(e.target.value)} placeholder="mysql://user:password@127.0.0.1:3306" autoComplete="off" spellCheck={false}/></label><div className="url-examples"><strong>One database: add its name after the final slash. All databases: leave the slash and name off.</strong><span>MySQL: mysql://user:password@host:3306/database</span><span>PostgreSQL: postgresql://user:password@host:5432/database</span><span>SQL Server: sqlserver://user:password@host:1433/database</span><span>All databases example: postgresql://user:password@host:5432</span></div><div className="form-hint"><KeyRound size={13}/>The URL is encrypted in the PostgreSQL app store. Only schema names are read; business rows are not loaded.</div>{error&&<div className="form-error">{error}</div>}{health&&!health.store_configured&&<div className="form-error">PostgreSQL app storage still needs APP_DATABASE_URL in api/.env before workspaces and chats can be saved. This is separate from the server URL above.</div>}<button className="primary-button full-button" disabled={connecting||Boolean(health&&!health.store_configured)}>{connecting?<><LoaderCircle size={16} className="spin"/>Discovering databases and saving workspaces…</>:<><PlugZap size={16}/>Connect and discover databases</>}</button></form>
    </section></div>}
    {error&&!connectionOpen&&<div className="global-error"><span>{error}</span><button onClick={()=>setError('')} aria-label="Dismiss"><X size={15}/></button></div>}
  </main>;
}

