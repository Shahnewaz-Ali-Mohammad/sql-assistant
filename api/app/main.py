from __future__ import annotations
import json, os, re, uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import urlparse, parse_qs, unquote, urlunsplit, quote
import mysql.connector, psycopg, pymysql
try:
    import pytds
    import certifi
except ImportError:
    pytds=None
    certifi=None
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

PYTDS_ERROR=pytds.Error if pytds is not None else type("PyTdsUnavailable",(Exception,),{})

load_dotenv()
app=FastAPI(title="SQL Assistant API", version="0.2.0")
app.add_middleware(CORSMiddleware,allow_origins=[x.strip() for x in os.getenv("WEB_ORIGINS","http://localhost:3000,http://127.0.0.1:3000,http://localhost:3003,http://127.0.0.1:3003").split(",") if x.strip()],allow_credentials=False,allow_methods=["GET","POST","PATCH","DELETE"],allow_headers=["Content-Type"])
PAGE_SIZE=10

class WorkspaceCreate(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    database_url:str=Field(min_length=1,max_length=2048)
class ServerConnect(BaseModel):
    connection_url:str=Field(min_length=1,max_length=2048)
class Rename(BaseModel):
    name:str=Field(min_length=1,max_length=100)
class ChatCreate(BaseModel):
    title:str=Field(default="New chat",max_length=160)
class Ask(BaseModel):
    question:str=Field(min_length=1,max_length=4000)

def cipher():
    key=os.getenv("APP_ENCRYPTION_KEY","").strip()
    if not key:
        key_file=os.path.join(os.path.dirname(os.path.dirname(__file__)),".encryption.key")
        try:
            if os.path.exists(key_file): key=open(key_file,encoding="ascii").read().strip()
            else:
                key=Fernet.generate_key().decode("ascii")
                with open(key_file,"x",encoding="ascii") as f: f.write(key)
                os.chmod(key_file,0o600)
        except Exception:
            raise HTTPException(500,"Could not create the local key used to encrypt saved database credentials.") from None
    try: return Fernet(key.encode("ascii"))
    except Exception: raise HTTPException(500,"APP_ENCRYPTION_KEY is invalid; use a Fernet key.") from None

def store():
    url=os.getenv("APP_DATABASE_URL","").strip()
    if not url: raise HTTPException(503,"Set APP_DATABASE_URL in api/.env to enable PostgreSQL workspace and chat storage.")
    try: return psycopg.connect(url,connect_timeout=5,row_factory=dict_row)
    except Exception: raise HTTPException(503,"Cannot reach SQL Assistant PostgreSQL storage. Check APP_DATABASE_URL and ensure PostgreSQL is running.") from None

def init_store():
    with store() as db:
        db.execute("CREATE TABLE IF NOT EXISTS sql_workspaces(id UUID PRIMARY KEY,name VARCHAR(100) NOT NULL,database_type VARCHAR(16) NOT NULL,database_name VARCHAR(256) NOT NULL,encrypted_url TEXT NOT NULL,schema_json JSONB NOT NULL,relationships_json JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        db.execute("CREATE TABLE IF NOT EXISTS sql_chats(id UUID PRIMARY KEY,workspace_id UUID NOT NULL REFERENCES sql_workspaces(id) ON DELETE CASCADE,title VARCHAR(160) NOT NULL DEFAULT 'New chat',created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        db.execute("CREATE TABLE IF NOT EXISTS sql_chat_messages(id BIGSERIAL PRIMARY KEY,chat_id UUID NOT NULL REFERENCES sql_chats(id) ON DELETE CASCADE,role VARCHAR(16) NOT NULL CHECK(role IN ('user','assistant')),content TEXT NOT NULL,outcome VARCHAR(16),sql_text TEXT,explanation TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        db.execute("CREATE INDEX IF NOT EXISTS sql_chats_workspace_updated ON sql_chats(workspace_id,updated_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS sql_messages_chat_id ON sql_chat_messages(chat_id,id)")
        db.execute("ALTER TABLE sql_chat_messages ADD COLUMN IF NOT EXISTS sql_dialect VARCHAR(16)")

@app.on_event("startup")
def startup():
    if os.getenv("APP_DATABASE_URL","").strip(): init_store()

def parse_url(raw):
    p=urlparse(raw.strip()); scheme=p.scheme.lower().split("+")[0]
    kind={"postgres":"postgresql","mssql":"sqlserver","sqlsrv":"sqlserver"}.get(scheme,scheme)
    if kind not in {"mysql","postgresql","sqlserver"}: raise ValueError("Use a mysql://, postgresql://, or sqlserver:// connection URL.")
    if not p.hostname: raise ValueError("The connection URL must include a server host or IP address.")
    try: port=p.port or {"mysql":3306,"postgresql":5432,"sqlserver":1433}[kind]
    except ValueError: raise ValueError("The connection URL has an invalid port.") from None
    q=parse_qs(p.query)
    ssl_value=q.get("ssl",q.get("sslmode",["false"]))[0].lower()
    database=unquote(p.path.strip("/").split("/")[0]) if p.path.strip("/") else None
    return {"type":kind,"host":p.hostname,"port":port,"database":database,"username":unquote(p.username or ""),"password":unquote(p.password or ""),"ssl":ssl_value in {"1","true","require","required","verify-ca","verify-full"},"query":q}

def open_db(c):
    if c["type"]=="mysql":
        return pymysql.connect(host=c["host"],port=c["port"],database=c["database"],user=c["username"],password=c["password"],connect_timeout=10,read_timeout=20,autocommit=True,charset="utf8mb4",ssl={"check_hostname":True} if c["ssl"] else None)
    if c["type"]=="postgresql":
        return psycopg.connect(host=c["host"],port=c["port"],dbname=c["database"] or "postgres",user=c["username"],password=c["password"],connect_timeout=10,sslmode="require" if c["ssl"] else "prefer",options="-c default_transaction_read_only=on")
    if pytds is None:
        raise RuntimeError("SQL Server support is unavailable. Install the SQL Assistant API dependencies and restart it.")
    options={
        "server":c["host"],"port":c["port"],"database":c["database"] or "master",
        "user":c["username"],"password":c["password"],"login_timeout":10,
        "timeout":20,"autocommit":True,"readonly":True,
    }
    if c["ssl"]:
        if certifi is None:
            raise RuntimeError("TLS support is unavailable. Install the SQL Assistant API dependencies and restart it.")
        options["cafile"]=certifi.where()
        options["validate_host"]=True
    return pytds.connect(**options)

def list_databases(c):
    db=open_db(c); cur=db.cursor()
    try:
        if c["type"]=="mysql":
            cur.execute("SHOW DATABASES"); names=[r[0] for r in cur.fetchall() if r[0] not in {"information_schema","mysql","performance_schema","sys"}]
        elif c["type"]=="postgresql":
            cur.execute("SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY datname"); names=[r[0] for r in cur.fetchall()]
        else:
            cur.execute("SELECT name FROM sys.databases WHERE database_id > 4 AND HAS_DBACCESS(name)=1 ORDER BY name"); names=[r[0] for r in cur.fetchall()]
        return names
    finally:
        cur.close(); db.close()

def discover(c):
    if not c.get("database"): raise ValueError("Select a database before loading its schema.")
    db=open_db(c); tables={}; links=[]
    try:
        if c["type"]=="mysql":
            cur=db.cursor()
            cur.execute("""SELECT c.TABLE_SCHEMA,c.TABLE_NAME,c.COLUMN_NAME,c.DATA_TYPE,c.IS_NULLABLE,c.COLUMN_KEY,k.REFERENCED_TABLE_SCHEMA,k.REFERENCED_TABLE_NAME,k.REFERENCED_COLUMN_NAME FROM information_schema.COLUMNS c LEFT JOIN information_schema.KEY_COLUMN_USAGE k ON k.TABLE_SCHEMA=c.TABLE_SCHEMA AND k.TABLE_NAME=c.TABLE_NAME AND k.COLUMN_NAME=c.COLUMN_NAME AND k.REFERENCED_TABLE_NAME IS NOT NULL WHERE c.TABLE_SCHEMA=%s ORDER BY c.TABLE_NAME,c.ORDINAL_POSITION""",(c["database"],))
            rows=cur.fetchall()
        elif c["type"]=="postgresql":
            rows=db.execute("""SELECT c.table_schema,c.table_name,c.column_name,c.data_type,c.is_nullable,CASE WHEN pk.column_name IS NOT NULL THEN 'PRI' END,fk.foreign_table_schema,fk.foreign_table_name,fk.foreign_column_name FROM information_schema.columns c LEFT JOIN (SELECT tc.table_schema,tc.table_name,kcu.column_name FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu USING(constraint_catalog,constraint_schema,constraint_name) WHERE tc.constraint_type='PRIMARY KEY') pk ON pk.table_schema=c.table_schema AND pk.table_name=c.table_name AND pk.column_name=c.column_name LEFT JOIN (SELECT kcu.table_schema,kcu.table_name,kcu.column_name,ccu.table_schema AS foreign_table_schema,ccu.table_name AS foreign_table_name,ccu.column_name AS foreign_column_name FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu USING(constraint_catalog,constraint_schema,constraint_name) JOIN information_schema.constraint_column_usage ccu USING(constraint_catalog,constraint_schema,constraint_name) WHERE tc.constraint_type='FOREIGN KEY') fk ON fk.table_schema=c.table_schema AND fk.table_name=c.table_name AND fk.column_name=c.column_name WHERE c.table_schema NOT IN ('information_schema','pg_catalog') AND c.table_schema NOT LIKE 'pg_toast%' ORDER BY c.table_schema,c.table_name,c.ordinal_position""").fetchall()
        else:
            cur=db.cursor()
            cur.execute("""SELECT c.TABLE_SCHEMA,c.TABLE_NAME,c.COLUMN_NAME,c.DATA_TYPE,c.IS_NULLABLE,CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 'PRI' END,fk.REFERENCED_SCHEMA,fk.REFERENCED_TABLE,fk.REFERENCED_COLUMN FROM INFORMATION_SCHEMA.COLUMNS c LEFT JOIN (SELECT ku.TABLE_SCHEMA,ku.TABLE_NAME,ku.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku ON ku.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND ku.CONSTRAINT_NAME=tc.CONSTRAINT_NAME AND ku.TABLE_SCHEMA=tc.TABLE_SCHEMA AND ku.TABLE_NAME=tc.TABLE_NAME WHERE tc.CONSTRAINT_TYPE='PRIMARY KEY') pk ON pk.TABLE_SCHEMA=c.TABLE_SCHEMA AND pk.TABLE_NAME=c.TABLE_NAME AND pk.COLUMN_NAME=c.COLUMN_NAME LEFT JOIN (SELECT fkcu.TABLE_SCHEMA,fkcu.TABLE_NAME,fkcu.COLUMN_NAME,pku.TABLE_SCHEMA AS REFERENCED_SCHEMA,pku.TABLE_NAME AS REFERENCED_TABLE,pku.COLUMN_NAME AS REFERENCED_COLUMN FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE fkcu JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS fktc ON fktc.CONSTRAINT_SCHEMA=fkcu.CONSTRAINT_SCHEMA AND fktc.CONSTRAINT_NAME=fkcu.CONSTRAINT_NAME AND fktc.TABLE_SCHEMA=fkcu.TABLE_SCHEMA AND fktc.TABLE_NAME=fkcu.TABLE_NAME AND fktc.CONSTRAINT_TYPE='FOREIGN KEY' JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc ON rc.CONSTRAINT_SCHEMA=fktc.CONSTRAINT_SCHEMA AND rc.CONSTRAINT_NAME=fktc.CONSTRAINT_NAME JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS pktc ON pktc.CONSTRAINT_SCHEMA=rc.UNIQUE_CONSTRAINT_SCHEMA AND pktc.CONSTRAINT_NAME=rc.UNIQUE_CONSTRAINT_NAME JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE pku ON pku.CONSTRAINT_SCHEMA=pktc.CONSTRAINT_SCHEMA AND pku.CONSTRAINT_NAME=pktc.CONSTRAINT_NAME AND pku.TABLE_SCHEMA=pktc.TABLE_SCHEMA AND pku.TABLE_NAME=pktc.TABLE_NAME AND pku.ORDINAL_POSITION=fkcu.ORDINAL_POSITION) fk ON fk.TABLE_SCHEMA=c.TABLE_SCHEMA AND fk.TABLE_NAME=c.TABLE_NAME AND fk.COLUMN_NAME=c.COLUMN_NAME ORDER BY c.TABLE_SCHEMA,c.TABLE_NAME,c.ORDINAL_POSITION""")
            rows=cur.fetchall()
        for r in rows:
            schema,table,col,typ,nullable,key,rschema,rtable,rcol=r; full=f"{schema}.{table}"
            tables.setdefault(full,{"schema":schema,"name":table,"columns":[]})["columns"].append({"name":col,"type":typ,"nullable":nullable=="YES","key":key or None})
            if rtable: links.append({"from_table":full,"from_column":col,"to_table":f"{rschema}.{rtable}","to_column":rcol})
    finally: db.close()
    return {"tables":tables,"relationships":links}

def url_for_database(raw,database):
    p=urlparse(raw.strip())
    return urlunsplit((p.scheme,p.netloc,"/"+quote(database,safe=""),p.query,p.fragment))

def create_or_update_workspace(db,base_url,database):
    c={**parse_url(base_url),"database":database}
    schema=discover(c); encrypted=cipher().encrypt(url_for_database(base_url,database).encode()).decode()
    rows=db.execute("SELECT * FROM sql_workspaces WHERE database_type=%s AND database_name=%s",(c["type"],database)).fetchall()
    for old in rows:
        try: previous=parse_url(cipher().decrypt(old["encrypted_url"].encode()).decode())
        except (InvalidToken,ValueError): continue
        if (previous["host"],previous["port"],previous["username"])==(c["host"],c["port"],c["username"]):
            row=db.execute("UPDATE sql_workspaces SET encrypted_url=%s,schema_json=%s,relationships_json=%s,updated_at=now() WHERE id=%s RETURNING *",(encrypted,Jsonb(schema["tables"]),Jsonb(schema["relationships"]),old["id"])).fetchone()
            return public_workspace(row),len(schema["tables"]),len(schema["relationships"]),False
    wid=uuid.uuid4()
    row=db.execute("INSERT INTO sql_workspaces(id,name,database_type,database_name,encrypted_url,schema_json,relationships_json) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",(wid,database,c["type"],database,encrypted,Jsonb(schema["tables"]),Jsonb(schema["relationships"]))).fetchone()
    return public_workspace(row),len(schema["tables"]),len(schema["relationships"]),True

def workspace(db,wid):
    try: ident=uuid.UUID(wid)
    except ValueError: raise HTTPException(404,"Workspace not found.") from None
    row=db.execute("SELECT * FROM sql_workspaces WHERE id=%s",(ident,)).fetchone()
    if not row: raise HTTPException(404,"Workspace not found.")
    return row

def chat(db,wid,cid):
    try: w,c=uuid.UUID(wid),uuid.UUID(cid)
    except ValueError: raise HTTPException(404,"Chat not found.") from None
    row=db.execute("SELECT * FROM sql_chats WHERE id=%s AND workspace_id=%s",(c,w)).fetchone()
    if not row: raise HTTPException(404,"Chat not found in this workspace.")
    return row

def public_workspace(r):
    return {"id":str(r["id"]),"name":r["name"],"database_type":r["database_type"],"database":r["database_name"],"created_at":r["created_at"].isoformat()}

@app.get("/api/health")
def health(): return {"ok":True,"configured":bool(os.getenv("OPENAI_API_KEY")),"store_configured":bool(os.getenv("APP_DATABASE_URL"))}

@app.get("/api/workspaces")
def list_workspaces():
    with store() as db: return {"workspaces":[public_workspace(r) for r in db.execute("SELECT * FROM sql_workspaces ORDER BY updated_at DESC").fetchall()]}

@app.post("/api/workspaces")
def create_workspace(req:WorkspaceCreate):
    try: c=parse_url(req.database_url); schema=discover(c); encrypted=cipher().encrypt(req.database_url.strip().encode()).decode()
    except (ValueError,RuntimeError) as e: raise HTTPException(400,str(e)) from None
    except (mysql.connector.Error,pymysql.MySQLError,psycopg.Error,PYTDS_ERROR) as e:
        raise HTTPException(400,f"Could not connect/read schema ({getattr(e,'errno',None) or 'connection'}). Check host, port, database, credentials, TLS, and network access.") from None
    wid=uuid.uuid4()
    with store() as db:
        row=db.execute("INSERT INTO sql_workspaces(id,name,database_type,database_name,encrypted_url,schema_json,relationships_json) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",(wid,req.name.strip(),c["type"],c["database"],encrypted,Jsonb(schema["tables"]),Jsonb(schema["relationships"]))).fetchone()
    return {**public_workspace(row),"table_count":len(schema["tables"]),"relationship_count":len(schema["relationships"])}

@app.post("/api/workspaces/connect-server")
def connect_server(req:ServerConnect):
    try:
        base=parse_url(req.connection_url)
        databases=[base["database"]] if base["database"] else list_databases(base)
    except (ValueError,RuntimeError) as e: raise HTTPException(400,str(e)) from None
    except (mysql.connector.Error,pymysql.MySQLError,psycopg.Error,PYTDS_ERROR):
        raise HTTPException(400,"Could not connect to the database server or list its databases. Check the host, port, credentials, TLS, and database-list permissions.") from None
    if not databases: raise HTTPException(400,"No accessible databases were found on this server.")
    workspaces=[]; skipped=[]; added=0; updated=0
    with store() as db:
        for database in databases:
            try:
                with db.transaction():
                    public,tables,relationships,is_new=create_or_update_workspace(db,req.connection_url,database)
                workspaces.append({**public,"table_count":tables,"relationship_count":relationships})
                if is_new: added+=1
                else: updated+=1
            except (mysql.connector.Error,pymysql.MySQLError,psycopg.Error,PYTDS_ERROR,ValueError,RuntimeError):
                skipped.append(database)
    if not workspaces:
        raise HTTPException(400,"The server was reached, but no accessible database schemas could be loaded. Check database permissions and installed drivers.")
    return {"workspaces":workspaces,"discovered":len(databases),"created":added,"updated":updated,"skipped":skipped}

@app.patch("/api/workspaces/{wid}")
def rename_workspace(wid:str,req:Rename):
    with store() as db:
        old=workspace(db,wid); row=db.execute("UPDATE sql_workspaces SET name=%s,updated_at=now() WHERE id=%s RETURNING *",(req.name.strip(),old["id"])).fetchone()
    return public_workspace(row)

@app.delete("/api/workspaces/{wid}")
def delete_workspace(wid:str):
    with store() as db: old=workspace(db,wid); db.execute("DELETE FROM sql_workspaces WHERE id=%s",(old["id"],))
    return {"ok":True}

@app.post("/api/workspaces/{wid}/reconnect")
def reconnect(wid:str):
    with store() as db:
        row=workspace(db,wid)
        try:
            raw=cipher().decrypt(row["encrypted_url"].encode()).decode(); c=parse_url(raw); schema=discover(c)
        except InvalidToken: raise HTTPException(500,"Could not decrypt saved URL; check APP_ENCRYPTION_KEY.") from None
        except (ValueError,RuntimeError) as e: raise HTTPException(400,str(e)) from None
        except (mysql.connector.Error,pymysql.MySQLError,psycopg.Error,PYTDS_ERROR) as e: raise HTTPException(400,f"Could not reconnect/read schema ({getattr(e,'errno',None) or 'connection'}). Check database access.") from None
        row=db.execute("UPDATE sql_workspaces SET schema_json=%s,relationships_json=%s,updated_at=now() WHERE id=%s RETURNING *",(Jsonb(schema["tables"]),Jsonb(schema["relationships"]),row["id"])).fetchone()
    return {**public_workspace(row),"table_count":len(schema["tables"]),"relationship_count":len(schema["relationships"])}

@app.get("/api/workspaces/{wid}/tables")
def list_tables(wid:str,offset:int=0,limit:int=PAGE_SIZE):
    if offset<0 or limit<1 or limit>PAGE_SIZE: raise HTTPException(400,"Tables load 10 at a time; limit must be from 1 to 10.")
    with store() as db:
        row=workspace(db,wid); entries=sorted(row["schema_json"].items()); total=len(entries); page=entries[offset:offset+limit]
    return {"tables":[{"full_name":n,**t} for n,t in page],"offset":offset,"limit":limit,"total":total,"has_more":offset+len(page)<total}

@app.get("/api/workspaces/{wid}/chats")
def list_chats(wid:str):
    with store() as db:
        w=workspace(db,wid); rows=db.execute("SELECT * FROM sql_chats WHERE workspace_id=%s ORDER BY updated_at DESC",(w["id"],)).fetchall()
    return {"chats":[{"id":str(r["id"]),"title":r["title"],"updated_at":r["updated_at"].isoformat()} for r in rows]}

@app.post("/api/workspaces/{wid}/chats")
def create_chat(wid:str,req:ChatCreate):
    with store() as db:
        w=workspace(db,wid); r=db.execute("INSERT INTO sql_chats(id,workspace_id,title) VALUES (%s,%s,%s) RETURNING *",(uuid.uuid4(),w["id"],req.title.strip() or "New chat")).fetchone()
    return {"id":str(r["id"]),"title":r["title"],"updated_at":r["updated_at"].isoformat()}

@app.get("/api/workspaces/{wid}/chats/{cid}/messages")
def list_messages(wid:str,cid:str):
    with store() as db:
        c=chat(db,wid,cid); rows=db.execute("SELECT * FROM sql_chat_messages WHERE chat_id=%s ORDER BY id",(c["id"],)).fetchall()
    return {"messages":[{"id":r["id"],"role":r["role"],"content":r["content"],"outcome":r["outcome"],"sql":r["sql_text"],"explanation":r["explanation"],"dialect":r["sql_dialect"],"created_at":r["created_at"].isoformat()} for r in rows]}

SYSTEM="""You are SQL Assistant. Generate a read-only SQL query for the database connected to this workspace.

Classify every user request as one of:
- sql: the user asks for data, records, values, counts, totals, comparisons, lists, or a breakdown from the connected database. This includes ordinary phrasing such as 'I need...', 'show me...', 'give me...', all-caps text, typos, and requests grouped by time, customer, location, or category. If a reasonable query can be written from the schema, write it.
- clarify: it is clearly a database question but there is no reasonable matching field or join in the supplied schema. Ask one short, specific question. Do not ask merely because a common business term has several plausible columns: select the closest direct match and state the assumption in the explanation.
- other: only for clearly non-database requests (for example weather, trivia, or general conversation). Never use 'other' just because a table or column is not an exact wording match, the schema is large, or the requested business measure is ambiguous; use 'clarify' instead.

Return JSON with outcome, reply, sql, explanation, and follow_up. Use only tables and columns in the supplied schema. Generate exactly one read-only SELECT (a WITH query is allowed only if it ends in SELECT). Never generate DML, DDL, procedure calls, multiple statements, or comments. Limit list queries to 100 rows unless the user asks for a different limit. Never claim SQL was executed or data was retrieved. Resolve relative dates using the exact current date supplied in system context; never guess the year. Prefer a useful query with a clearly stated assumption over an avoidable clarification. For 'new customers', count distinct customer IDs and, when available, use the customer's SalesDate; otherwise use the clearest customer creation/activation date. Do not ask which date when a clear matching field exists. For POP/location breakdowns, use the descriptive name when available or join on matching ID columns. For 'total sales', prefer a direct TotalAmount/Amount field; when it is in a billing source with RefDate, use RefDate for the sales month. If that is unavailable, use the closest explicit sales amount and state the assumption. Infer a join only from declared foreign keys or matching ID columns in the supplied join candidates; state when the join is inferred. If metrics have different natural date fields, use each metric's own date and explain briefly. Apply the requested date range to every date-based metric involved. When a request combines new-customer counts and billing sales by month, aggregate customer acquisitions independently by SalesDate and sales independently by the billing date, grouped by POP and calendar month, then combine those aggregates; do not restrict billing rows to the month each customer was acquired. Never join or align separate date fields using only MONTH() or day-of-year while ignoring the year; constrain each source to the requested full date range. Never apply date functions to IDs or cast IDs into dates. For two monthly metrics sourced from separate fact tables, use independently grouped aggregate CTEs before joining so raw joins do not multiply counts or sales. Ask only when no reasonable schema-supported query can be made."""
FORBIDDEN=re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|TRUNCATE|CALL|GRANT|REVOKE|EXEC|EXECUTE|MERGE|UPSERT|COPY|LOAD|OUTFILE|DUMPFILE|OPENROWSET|OPENDATASOURCE)\b",re.I)
DATA_ACTION=re.compile(r"\b(show|list|give|find|counts?|totals?|calculate|compare|summari[sz]e|report|query|select|need|want|retrieve|return|display|how many|what is|what are|top)\b",re.I)
DATA_SUBJECT=re.compile(r"\b(data|database|record|records|row|rows|table|tables|customer|customers|sales|amount|total|count|revenue|billing|invoice|payment|transaction|month|monthly|year|date|pop|location|category|breakdown|report)\b",re.I)
OTHER_REPLY="I can help write SQL for the database connected to this workspace. Ask me for a query or describe the data you want."
def is_data_request(question):
    return bool(DATA_ACTION.search(question) and DATA_SUBJECT.search(question))

def relevant_schema(question, schema, relationships):
    """Keep model context focused on likely business tables and their declared relations."""
    if not is_data_request(question):
        return {}, [], []
    def words(value):
        value=str(value)
        value=re.sub(r"([A-Z]+)([A-Z][a-z])",r"\1 \2",value)
        value=re.sub(r"([a-z0-9])([A-Z])",r"\1 \2",value)
        return set(re.findall(r"[a-z0-9]+",value.casefold()))
    question_words=words(question)
    concepts={
        "customer":{"customer","customers","client","clients","subscriber","subscribers","account","accounts"},
        "pop":{"pop","pops","location","locations","site","sites","branch","branches"},
        "sales":{"sale","sales","revenue","billing","invoice","invoices","payment","payments","amount","totalamount","debit","credit","mrc","otc"},
        "date":{"date","month","monthly","year","yearly","period","time","salesdate","refdate","entrydate","createddate"},
        "new":{"new","salesdate","entrydate","createddate","joindate","activationdate"},
        "ticket":{"ticket","tickets","issue","issues"},
    }
    wanted=set(question_words)
    matched_concepts={name for name,aliases in concepts.items() if question_words & aliases}
    for concept in matched_concepts:
        wanted.update(concepts[concept])
    stop={"i","need","want","the","a","an","for","from","to","this","that","each","every","by","of","and","me","show","give","all","number","numbers","total","wise","please","what","are","is","how","many","list","report","query","data","records","record"}
    wanted-=stop
    scored=[]
    for full_name, metadata in schema.items():
        table_name=str(metadata.get("name") or full_name.split(".")[-1])
        name_words=words(full_name+" "+table_name)
        column_words=set()
        columns=[]
        for column in metadata.get("columns",[]):
            name=str(column.get("name",""))
            parts=words(name)
            column_words.update(parts); columns.append((name.casefold(),parts))
        score=5*len(name_words & wanted)+len(column_words & wanted)
        for concept in matched_concepts:
            aliases=concepts[concept]
            if any(alias in name_words for alias in aliases): score+=5
            if any(alias in column_words for alias in aliases): score+=2
        if any("customerid" in c[0] for c in columns): score+=2 if "customer" in question_words else 0
        lowered=full_name.casefold()
        if any(x in lowered for x in ("_log","_deleted","history","audit","_bk","_sep","temp_","_enddatechange")) and not (question_words & {"log","logs","deleted","history","historical","audit","temporary"}): score-=24
        if re.search(r"_dis(?:_|$)",lowered) and not (question_words & {"dis","discontinued","archive","archived"}): score-=24
        if "commission" in lowered and not (question_words & {"commission","commissioned"}): score-=40
        if "customerquery" in lowered and not (question_words & {"query","request","lead"}): score-=18
        if "requestmaster" in lowered and not (question_words & {"request","lead","query"}): score-=16
        if any(x in lowered for x in ("customerleads","customerlead","reseller")) and not (question_words & {"lead","leads","reseller","resellers"}): score-=40
        if "dailycustomer" in lowered and "daily" not in question_words: score-=30
        if "monthlycustomer" in lowered and "monthly" in question_words: score-=20
        if "bi_agent_" in lowered: score-=20
        if any(x in lowered for x in ("temp","creditnote")) and not (question_words & {"temporary","creditnote","creditnote"}): score-=40
        if any(x in lowered for x in ("ticket","tk_ticket")) and "ticket" not in matched_concepts: score-=40
        if "sales" in matched_concepts and any(x in lowered for x in ("coupon","adjustment","enddatechange","ledger")) and not (question_words & {"coupon","adjustment","ledger","commission"}): score-=40
        if "customer" in matched_concepts and "type" in name_words and not (question_words & {"type","category"}): score-=16
        if "monthlycustomer" in lowered and "monthly" not in question_words: score-=12
        if "master" in name_words and not any(x in lowered for x in ("_log","_deleted","_bk","_sep")): score+=14
        if score>0: scored.append((score,full_name))
    scored.sort(key=lambda item:(-item[0],item[1].casefold()))
    chosen={name for _,name in scored[:6]}
    # Keep the best canonical master per requested entity even when similarly named
    # backups, logs, or workflow tables have many coincidental column matches.
    for concept in matched_concepts & {"customer","pop","sales","ticket"}:
        aliases=concepts[concept]
        candidates=[(score,name) for score,name in scored if "master" in words(name) and words(name) & aliases]
        if candidates: chosen.add(candidates[0][1])
    if "sales" in matched_concepts:
        amount_tables=[]
        for score,name in scored:
            columns={str(c.get("name","")).casefold() for c in schema[name].get("columns",[])}
            parts=words(name)
            is_fact_view=("invoice" in parts or ("billing" in parts and "master" not in parts)) and not any(x in name.casefold() for x in ("commission","enddatechange"))
            if is_fact_view and columns & {"totalamount","amount","debit","credit","mrc","otc"}:
                quality=score+4*bool("salesdate" in columns)+4*bool("totalamount" in columns)+2*bool("customerid" in columns)+2*bool("popname" in columns)
                amount_tables.append((quality,name))
        if amount_tables:
            chosen.add(max(amount_tables)[1])
    selected={name:metadata for name,metadata in schema.items() if name in chosen}
    selected_links=[r for r in relationships if r.get("from_table") in selected and r.get("to_table") in selected]
    join_candidates=[]
    selected_items=list(selected.items())
    for index,(left_name,left_meta) in enumerate(selected_items):
        left_columns={str(c.get("name","")).casefold():str(c.get("name","")) for c in left_meta.get("columns",[])}
        for right_name,right_meta in selected_items[index+1:]:
            right_columns={str(c.get("name","")).casefold():str(c.get("name","")) for c in right_meta.get("columns",[])}
            for key in left_columns.keys() & right_columns.keys():
                if key.endswith("id") and key not in {"id"} and (("customer" in matched_concepts and "customer" in key) or ("pop" in matched_concepts and "pop" in key) or ("ticket" in matched_concepts and "ticket" in key)):
                    join_candidates.append({"left_table":left_name,"left_column":left_columns[key],"right_table":right_name,"right_column":right_columns[key],"status":"inferred from matching column names; not a declared foreign key"})
    return selected, selected_links, join_candidates[:24]

def safe_sql(sql):
    if not isinstance(sql,str) or not re.match(r"^\s*(SELECT|WITH)\b",sql,re.I) or FORBIDDEN.search(sql): return False
    s=sql.strip(); s=s[:-1].rstrip() if s.endswith(";") else s
    return not any(x in s for x in (";","--","/*","*/","#")) and not re.search(r"\bFOR\s+(UPDATE|SHARE)\b|\bLOCK\s+IN\s+SHARE\s+MODE\b",s,re.I) and not(s.upper().startswith("WITH") and not re.search(r"\bSELECT\b",s,re.I))

def validate_sql_semantics(sql, question, schema, database_type):
    """Catch common schema-valid but business-invalid generated SQL before it is shown."""
    try:
        dialect={"mysql":"mysql","postgresql":"postgres","sqlserver":"tsql"}[database_type]
        tree=sqlglot.parse_one(sql,read=dialect)
    except Exception:
        return None  # Syntax and schema validation report parse errors separately.

    # Reject aggregate queries that select a non-aggregated field absent from
    # GROUP BY. This catches valid-looking drafts such as selecting Billing.CustomerID
    # while grouping only by month, year, and POP.
    for select in tree.find_all(exp.Select):
        group=select.args.get("group")
        if group is None:
            continue
        group_expressions={item.sql(dialect=dialect,normalize=True) for item in group.expressions}
        group_columns={(item.table.casefold(),item.name.casefold()) for expression in group.expressions
                       if isinstance(expression,exp.Column) for item in [expression]}
        for projection in select.expressions:
            value=projection.this if isinstance(projection,exp.Alias) else projection
            if value.find(exp.AggFunc) is not None:
                continue
            normalized=value.sql(dialect=dialect,normalize=True)
            if normalized in group_expressions:
                continue
            if isinstance(value,exp.Column) and (value.table.casefold(),value.name.casefold()) in group_columns:
                continue
            return f"Selected expression {value.sql(dialect=dialect)} is not aggregated or included in GROUP BY."

    # A month/year expression must operate on a real date column, never an ID that
    # the model happened to cast to a date. Resolve table aliases against metadata.
    physical={}
    for full_name, metadata in schema.items():
        physical[metadata.get("name",full_name.split(".")[-1]).casefold()]=metadata
    alias_to_metadata={}
    for table in tree.find_all(exp.Table):
        metadata=physical.get(table.name.casefold())
        if metadata:
            alias_to_metadata[table.alias_or_name.casefold()]=metadata
            alias_to_metadata[table.name.casefold()]=metadata
    for date_part in list(tree.find_all(exp.Month))+list(tree.find_all(exp.Year)):
        for column in date_part.find_all(exp.Column):
            metadata=alias_to_metadata.get((column.table or "").casefold()) if column.table else None
            if metadata is None and len(alias_to_metadata)==1:
                metadata=next(iter(alias_to_metadata.values()))
            if metadata is None:
                continue
            column_info=next((item for item in metadata.get("columns",[]) if str(item.get("name","")).casefold()==column.name.casefold()),None)
            if column_info:
                data_type=str(column_info.get("type","")).casefold()
                if not any(marker in data_type for marker in ("date","time","timestamp")):
                    return f"{date_part.key.upper()}() is applied to {column.sql()}, whose saved schema type is {data_type or 'not a date type'}. Use a date column from the schema; never cast an identifier to a date."

    # When acquisition counts and billing sales are requested together by month,
    # joining both raw fact tables in one aggregation can multiply customer/sales
    # rows and tie sales to the wrong month. Require separate source aggregations.
    if re.search(r"\bnew\s+(customers?|clients?|subscribers?)\b",question,re.I) and re.search(r"\b(sales|revenue|billing|total\s+amount)\b",question,re.I) and re.search(r"\b(month|monthly)\b",question,re.I):
        cte_names={cte.alias_or_name.casefold() for cte in tree.find_all(exp.CTE) if cte.alias_or_name}
        def relation_name(node):
            if isinstance(node,exp.Table) and node.name.casefold() not in cte_names:
                return node.name.casefold()
            return ""
        def has_direct_aggregate(select, aggregate_type):
            for expression in select.expressions:
                for aggregate in expression.find_all(aggregate_type):
                    parent=aggregate.parent
                    while parent is not None and not isinstance(parent,exp.Select):
                        parent=parent.parent
                    if parent is select:
                        return True
            return False
        for select in tree.find_all(exp.Select):
            direct=[]
            from_clause=select.args.get("from_")
            if from_clause is not None:
                direct.append(relation_name(from_clause.this))
            direct.extend(relation_name(join.this) for join in select.args.get("joins",[]))
            names=set(direct)
            has_customer=any(any(token in name for token in ("customer","client","subscriber")) for name in names)
            has_billing=any(any(token in name for token in ("billing","invoice","payment","sales")) for name in names)
            has_customer_metric=has_direct_aggregate(select,exp.Count)
            has_sales_metric=has_direct_aggregate(select,exp.Sum)
            if has_customer and has_billing and has_customer_metric and has_sales_metric:
                return "Customer acquisitions and billing sales are joined as raw rows in one monthly aggregation. Aggregate each source independently by its own date, POP, year, and month, then combine the aggregates on POP, year, and month to avoid wrong-month attribution and fan-out."
        ctes={cte.alias_or_name.casefold():cte.this for cte in tree.find_all(exp.CTE) if cte.alias_or_name and isinstance(cte.this,exp.Select)}
        customer_ctes=[]; sales_ctes=[]
        for name,select in ctes.items():
            source_names={relation_name(select.args.get("from_").this)} if select.args.get("from_") is not None else set()
            source_names.update(relation_name(join.this) for join in select.args.get("joins",[]))
            source_names.discard("")
            has_customer=any(any(token in item for token in ("customer","client","subscriber")) for item in source_names)
            has_billing=any(any(token in item for token in ("billing","invoice","payment","sales")) for item in source_names)
            group=select.args.get("group")
            group_columns={column.name.casefold() for column in group.find_all(exp.Column)} if group is not None else set()
            has_pop_key=any("popid" in column for column in group_columns)
            if has_customer and has_direct_aggregate(select,exp.Count):
                customer_ctes.append((name,has_pop_key))
            if has_billing and has_direct_aggregate(select,exp.Sum):
                sales_ctes.append((name,has_pop_key))
        if customer_ctes and sales_ctes:
            if any(not grouped for _,grouped in customer_ctes):
                return "The new-customer aggregate is not grouped by POP ID. Group customer counts by the schema-backed POP ID and acquisition month."
            if any(not grouped for _,grouped in sales_ctes):
                return "The sales aggregate is not grouped by POP ID, so its total would be repeated for every POP. Derive each billed customer's POP from a supported POP ID relationship and group billing totals by POP ID and billing month."
            cte_reference_aliases={table.name.casefold():table.alias_or_name.casefold() for table in tree.find_all(exp.Table) if table.name.casefold() in ctes}
            required_pairs={(cte_reference_aliases.get(customer_name,customer_name),cte_reference_aliases.get(sales_name,sales_name)) for customer_name,_ in customer_ctes for sales_name,_ in sales_ctes}
            pop_edges=set(); time_edges=set()
            for equality in tree.find_all(exp.EQ):
                left,right=equality.this,equality.expression
                if not isinstance(left,exp.Column) or not isinstance(right,exp.Column):
                    continue
                aliases={left.table.casefold(),right.table.casefold()}
                pair=next(((customer_name,sales_name) for customer_name,sales_name in required_pairs if aliases=={customer_name,sales_name}),None)
                if pair is None:
                    if "popid" in {left.name.casefold(),right.name.casefold()}:
                        pop_edges.add((left.table.casefold(),right.table.casefold()))
                    if {left.name.casefold(),right.name.casefold()} & {"salesmonth","billingmonth","month"}:
                        time_edges.add((left.table.casefold(),right.table.casefold()))
                    continue
                if "popid" in {left.name.casefold(),right.name.casefold()}:
                    pop_edges.add((left.table.casefold(),right.table.casefold()))
                if {left.name.casefold(),right.name.casefold()} & {"salesmonth","billingmonth","month"}:
                    time_edges.add((left.table.casefold(),right.table.casefold()))
            def pop_connected(start,target):
                pending=[start]; seen=set()
                while pending:
                    current=pending.pop()
                    if current==target:
                        return True
                    if current in seen:
                        continue
                    seen.add(current)
                    pending.extend(right if left==current else left for left,right in pop_edges if left==current or right==current)
                return False
            def time_connected(start,target):
                pending=[start]; seen=set()
                while pending:
                    current=pending.pop()
                    if current==target:
                        return True
                    if current in seen:
                        continue
                    seen.add(current)
                    pending.extend(right if left==current else left for left,right in time_edges if left==current or right==current)
                return False
            if any(not pop_connected(customer_name,sales_name) or not time_connected(customer_name,sales_name) for customer_name,sales_name in required_pairs):
                return "The separate monthly aggregates are not aligned on both POP ID and month. Join the customer and sales aggregates using POP ID plus calendar year and month, and do not repeat a company-wide sales total for each POP."
    return None

def validate_sql_schema(sql, schema, database_name, database_type, strict_columns=True):
    """Reject generated SELECTs that refer to tables or columns outside the saved schema."""
    dialect={"mysql":"mysql","postgresql":"postgres","sqlserver":"tsql"}[database_type]
    try:
        statements=[tree for tree in sqlglot.parse(sql,read=dialect) if tree is not None]
        if len(statements)!=1:
            return "Return exactly one SQL statement."
        tree=statements[0]
        if not isinstance(tree,(exp.Select,exp.Union,exp.Intersect,exp.Except)):
            return "The statement must be a read-only SELECT query."

        tables=[]
        for full_name,metadata in schema.items():
            parts=full_name.split(".")
            table_name=metadata.get("name") or parts[-1]
            schema_name=metadata.get("schema") or (parts[-2] if len(parts)>1 else "")
            columns={str(column.get("name","")).casefold():str(column.get("type", "")) for column in metadata.get("columns",[])}
            tables.append({"full":full_name,"name":table_name,"schema":schema_name,"columns":columns})

        cte_names={cte.alias_or_name.casefold() for cte in tree.find_all(exp.CTE) if cte.alias_or_name}
        canonical={}
        for table in tree.find_all(exp.Table):
            name=table.name.casefold()
            if not name or name in cte_names:
                continue
            candidates=[item for item in tables if item["name"].casefold()==name]
            requested_schema=(table.db or "").casefold()
            if requested_schema and strict_columns:
                candidates=[item for item in candidates if item["schema"].casefold()==requested_schema]
            requested_catalog=(table.catalog or "").casefold()
            if requested_catalog and requested_catalog!=database_name.casefold():
                return f"Table {table.sql(dialect=dialect)} is outside the connected database."
            if not candidates:
                return f"Table {table.sql(dialect=dialect)} is not in this workspace schema."
            if len(candidates)>1:
                return f"Table {table.name} exists in multiple schemas; qualify it with its schema."
            canonical[name]=candidates[0]

        if strict_columns:
            # The saved schema can come from a different engine than the selected
            # output dialect (for example SQL Server's dbo tables rendered as MySQL).
            # Table/schema membership was already checked above; use resolved
            # physical tables as a flat schema for column qualification.
            glot_schema={item["name"]:item["columns"] for item in canonical.values()}
            qualify(tree.copy(),schema=glot_schema,dialect=dialect,validate_qualify_columns=True)
        return None
    except Exception as error:
        message=str(error).splitlines()[0]
        return f"Schema validation failed: {message[:300]}"

def build_supported_pop_monthly_sql(question, schema, database_type, current_date):
    """Build a schema-backed fallback for the common POP/customer/sales monthly request."""
    if not (re.search(r"\bnew\s+(customers?|clients?|subscribers?)\b",question,re.I)
            and re.search(r"\b(sales|revenue|billing|total\s+amount)\b",question,re.I)
            and re.search(r"\b(month|monthly)\b",question,re.I)
            and re.search(r"\b(pop|location|site|branch)\b",question,re.I)):
        return None
    if not re.search(r"\bjan(?:uary)?\b.*\bto\b.*\boct(?:ober)?\b",question,re.I|re.S):
        return None
    current_year=int(current_date[:4])

    def find_table(required_name, required_columns):
        candidates=[]
        for full_name,metadata in schema.items():
            if str(metadata.get("name",full_name.split(".")[-1])).casefold()!=required_name.casefold():
                continue
            columns={str(c.get("name","")).casefold() for c in metadata.get("columns",[])}
            if set(required_columns)<=columns:
                candidates.append(full_name)
        return candidates[0] if len(candidates)==1 else None

    customer_table=find_table("CustomerMaster",{"customerid","popid","salesdate"}) or find_table("CustomerMaster",{"customerid","popid","createddate"}) or find_table("CustomerMaster",{"customerid","popid","activationdate"})
    pop_table=find_table("POPMaster",{"popid","popname"})
    billing_table=find_table("BillingMaster",{"customerid","refdate","totalamount"})
    if not (customer_table and pop_table and billing_table):
        return None

    def actual_column(table, wanted):
        return next(str(c["name"]) for c in schema[table]["columns"] if str(c.get("name","")).casefold()==wanted.casefold())
    acquisition=next((actual_column(customer_table,name) for name in ("salesdate","createddate","activationdate") if any(str(c.get("name","")).casefold()==name for c in schema[customer_table]["columns"])),None)
    if not acquisition:
        return None

    def ident(value):
        if database_type=="sqlserver":
            return "["+value.replace("]","]]" )+"]"
        if database_type=="mysql":
            return "`"+value.replace("`","``")+"`"
        return '"'+value.replace('"','""')+'"'
    def table_ref(full_name):
        parts=full_name.split(".")
        # SQL Server's default dbo schema is not portable to MySQL/MariaDB or
        # PostgreSQL. Keep the actual table name and render it in the target dialect.
        if database_type in {"mysql","postgresql"} and len(parts)>1 and parts[-2].casefold()=="dbo":
            parts=parts[-1:]
        return ".".join(ident(part) for part in parts)

    cust=table_ref(customer_table); pop=table_ref(pop_table); billing=table_ref(billing_table)
    customer_id=ident(actual_column(customer_table,"customerid")); customer_pop=ident(actual_column(customer_table,"popid")); sales_date=ident(acquisition)
    billing_customer_id=ident(actual_column(billing_table,"customerid")); ref_date=ident(actual_column(billing_table,"refdate")); total_amount=ident(actual_column(billing_table,"totalamount"))
    pop_id=ident(actual_column(pop_table,"popid")); pop_name=ident(actual_column(pop_table,"popname"))
    month_expr=lambda column: f"MONTH({column})" if database_type!="postgresql" else f"EXTRACT(MONTH FROM {column})::int"
    month_rows="\n    UNION ALL\n    ".join(f"SELECT {month} AS SalesMonth" for month in range(1,11))
    sql=f"""WITH Months AS (
    {month_rows}
), Populations AS (
    SELECT DISTINCT P.{pop_id} AS POPId, P.{pop_name} AS POPName
    FROM {pop} P
), CustomerCounts AS (
    SELECT C.{customer_pop} AS POPId, {month_expr('C.'+sales_date)} AS SalesMonth,
           COUNT(DISTINCT C.{customer_id}) AS NewCustomerCount
    FROM {cust} C
    WHERE C.{sales_date} >= '{current_year}-01-01' AND C.{sales_date} < '{current_year}-11-01'
    GROUP BY C.{customer_pop}, {month_expr('C.'+sales_date)}
), CustomerPOP AS (
    SELECT C.{customer_id} AS CustomerID, MIN(C.{customer_pop}) AS POPId
    FROM {cust} C
    WHERE C.{customer_pop} IS NOT NULL
    GROUP BY C.{customer_id}
    HAVING COUNT(DISTINCT C.{customer_pop}) = 1
), SalesTotals AS (
    SELECT CP.POPId, {month_expr('B.'+ref_date)} AS SalesMonth,
           SUM(B.{total_amount}) AS TotalSales
    FROM {billing} B
    JOIN CustomerPOP CP ON CP.CustomerID = B.{billing_customer_id}
    WHERE B.{ref_date} >= '{current_year}-01-01' AND B.{ref_date} < '{current_year}-11-01'
    GROUP BY CP.POPId, {month_expr('B.'+ref_date)}
)
SELECT P.POPName, {current_year} AS SalesYear, M.SalesMonth,
       COALESCE(C.NewCustomerCount, 0) AS NewCustomerCount,
       COALESCE(S.TotalSales, 0) AS TotalSales
FROM Populations P
CROSS JOIN Months M
LEFT JOIN CustomerCounts C ON C.POPId = P.POPId AND C.SalesMonth = M.SalesMonth
LEFT JOIN SalesTotals S ON S.POPId = P.POPId AND S.SalesMonth = M.SalesMonth
ORDER BY P.POPName, M.SalesMonth"""
    return sql, f"Counts distinct customer IDs by {acquisition}. Sales use BillingMaster.RefDate and TotalAmount. They are assigned through CustomerMaster only where a customer has exactly one distinct POP ID, which avoids duplicating a billing row across POPs. Customers with multiple POP assignments are omitted from POP sales totals because this schema does not identify which POP applied on the billing date; a dated assignment history or POP ID on billing records is needed for historical attribution."

def save_message(db,cid,role,content,outcome=None,sql=None,explanation=None,dialect=None):
    db.execute("INSERT INTO sql_chat_messages(chat_id,role,content,outcome,sql_text,explanation,sql_dialect) VALUES (%s,%s,%s,%s,%s,%s,%s)",(cid,role,content,outcome,sql,explanation,dialect))
    db.execute("UPDATE sql_chats SET updated_at=now(),title=CASE WHEN title='New chat' AND %s='user' THEN left(%s,70) ELSE title END WHERE id=%s",(role,content,cid))

@app.post("/api/workspaces/{wid}/chats/{cid}/ask")
def ask(wid:str,cid:str,req:Ask):
    question=req.question.strip()
    data_request=is_data_request(question)
    with store() as db:
        w=workspace(db,wid); c=chat(db,wid,cid); schema=w["schema_json"]; links=w["relationships_json"]
        output_dialect=w["database_type"]
        history=db.execute("SELECT role,content,outcome FROM sql_chat_messages WHERE chat_id=%s ORDER BY id DESC LIMIT 12",(c["id"],)).fetchall(); history.reverse()
        save_message(db,c["id"],"user",question,dialect=output_dialect)
    dialect_names={"mysql":"MySQL / MariaDB","postgresql":"PostgreSQL","sqlserver":"SQL Server (T-SQL)"}
    dialect=dialect_names[output_dialect]
    validator_dialect=output_dialect
    current_date=datetime.now(ZoneInfo("Asia/Dhaka")).date().isoformat()
    deterministic=build_supported_pop_monthly_sql(question,schema,output_dialect,current_date)
    deterministic_issue=None
    if deterministic:
        deterministic_sql=deterministic[0]
        deterministic_issue=(
            "The generated query did not pass the read-only SQL safety check."
            if not safe_sql(deterministic_sql)
            else validate_sql_schema(
                deterministic_sql,schema,w["database_name"],output_dialect,
                True
            ) or validate_sql_semantics(deterministic_sql,question,schema,output_dialect)
        )
    if deterministic and deterministic_issue:
        result={"outcome":"error","reply":"I found the schema pattern for this monthly POP report, but its generated query did not pass validation. Please check the workspace table and column metadata before retrying.","sql":None,"explanation":deterministic_issue}
    elif deterministic:
        result={"outcome":"sql","reply":"Here is the POP-by-month query in the selected SQL dialect.","sql":deterministic[0],"explanation":deterministic[1]}
    elif not os.getenv("OPENAI_API_KEY"):
        result={"outcome":"error","reply":"Add OPENAI_API_KEY to api/.env and restart the API.","sql":None,"explanation":None}
    else:
        prompt_schema,prompt_links,join_candidates=relevant_schema(question,schema,links)
        messages=[{"role":"system","content":SYSTEM+"\nCurrent date: "+current_date+" (Asia/Dhaka).\nWorkspace database engine and required SQL dialect: "+dialect+". Generate syntax only for this connected workspace engine. For MySQL / MariaDB output, do not use SQL Server square brackets or the SQL Server dbo schema prefix."},{"role":"system","content":"Relevant schema metadata only (no table rows):\n"+json.dumps({"database":w["database_name"],"tables":prompt_schema,"declared_foreign_key_relationships":prompt_links,"inferred_matching_id_join_candidates":join_candidates},ensure_ascii=False)}]
        messages.extend({"role":r["role"],"content":r["content"][:4000]} for r in history if r["role"] in {"user","assistant"} and not (data_request and r["role"]=="assistant" and (r["outcome"]=="other" or r["content"]==OTHER_REPLY)))
        messages.append({"role":"user","content":question})
        try:
            client=OpenAI(api_key=os.getenv("OPENAI_API_KEY"),timeout=45,max_retries=1)
            model=os.getenv("OPENAI_MODEL","gpt-4o-mini")
            def generate(prompt_messages):
                response=client.chat.completions.create(model=model,temperature=.1,response_format={"type":"json_object"},messages=prompt_messages)
                return json.loads(response.choices[0].message.content or "{}")
            payload=generate(messages); outcome=payload.get("outcome")
            if outcome=="other" and data_request:
                retry_messages=messages[:-1]+[{"role":"system","content":"The latest user message explicitly requests database information. Do not classify it as other. Use the supplied schema to return a read-only SQL query, or ask one specific clarification if its metric/date meaning is ambiguous."},messages[-1]]
                payload=generate(retry_messages); outcome=payload.get("outcome")
            if outcome=="clarify" and data_request and re.search(r"\bnew\s+(customers?|clients?|subscribers?)\b",question,re.I):
                acquisition_fields=[f"{table}.{column['name']}" for table,metadata in schema.items() for column in metadata.get("columns",[]) if str(column.get("name","")).casefold() in {"salesdate","createddate","entrydate","activationdate"} and any(term in table.casefold() for term in ("customer","client","subscriber"))]
                if acquisition_fields:
                    preferred=next((field for field in acquisition_fields if field.casefold().endswith(".salesdate")),acquisition_fields[0])
                    retry_messages=messages[:-1]+[{"role":"system","content":f"The user clearly requested a new-customer count. The schema provides {preferred}, a direct customer acquisition date. Use it as the default without asking for confirmation. Generate the requested read-only SQL and state this date assumption briefly. Aggregate any separate billing/sales metric independently by its own date, POP and month."},messages[-1]]
                    payload=generate(retry_messages); outcome=payload.get("outcome")
            if outcome=="sql":
                query=payload.get("sql")
                if not safe_sql(query):
                    result={"outcome":"error","reply":"The generated query did not pass the read-only SQL safety check. Please rephrase the request.","sql":None,"explanation":None}
                else:
                    query=query.strip().rstrip(";")
                    strict_columns=True
                    issue=validate_sql_schema(query,schema,w["database_name"],validator_dialect,strict_columns) or validate_sql_semantics(query,question,schema,validator_dialect)
                    if issue:
                        repair_messages=messages+[{
                            "role":"assistant","content":json.dumps(payload,ensure_ascii=False)
                        },{
                            "role":"user","content":("The SQL draft failed validation against the authoritative workspace schema or query semantics: "
                            +issue+" Correct the query using only listed tables and columns. For monthly new-customer counts, use a date-typed acquisition field, never cast an ID to a date. When combining monthly acquisition counts with billing sales, build separate aggregate CTEs using the acquisition date for customers and billing RefDate for sales, filter each to the full requested range, then combine by POP, year, and month. Do not join raw fact rows or align date fields by month alone. Return one read-only SELECT. If the schema truly cannot support the request, ask one specific question.")
                        }]
                        repaired=generate(repair_messages)
                        fixed_sql=repaired.get("sql")
                        fixed_issue=(validate_sql_schema(fixed_sql,schema,w["database_name"],validator_dialect,strict_columns) or validate_sql_semantics(fixed_sql,question,schema,validator_dialect)) if repaired.get("outcome")=="sql" and safe_sql(fixed_sql) else "The repaired response did not contain safe SQL."
                        fallback=build_supported_pop_monthly_sql(question,schema,validator_dialect,current_date) if fixed_issue else None
                        if not fixed_issue:
                            result={"outcome":"sql","reply":repaired.get("reply") or "Here is the query:","sql":fixed_sql.strip().rstrip(";"),"explanation":repaired.get("explanation") or ""}
                        elif fallback and safe_sql(fallback[0]) and not validate_sql_schema(fallback[0],schema,w["database_name"],validator_dialect,True):
                            result={"outcome":"sql","reply":"Here is a schema-checked query for the requested monthly POP breakdown.","sql":fallback[0],"explanation":fallback[1]}
                        elif repaired.get("outcome")=="clarify":
                            result={"outcome":"clarify","reply":repaired.get("follow_up") or repaired.get("reply") or "Which field or business measure should I use?","sql":None,"explanation":None}
                        else:
                            result={"outcome":"clarify","reply":"I couldn’t safely map every requested field to this workspace’s schema. Which date field and sales measure should the query use?","sql":None,"explanation":None}
                    else:
                        result={"outcome":"sql","reply":payload.get("reply") or "Here is the query:","sql":query,"explanation":payload.get("explanation") or ""}
            elif outcome=="clarify":
                fallback=build_supported_pop_monthly_sql(question,schema,validator_dialect,current_date)
                if fallback and safe_sql(fallback[0]) and not validate_sql_schema(fallback[0],schema,w["database_name"],validator_dialect,True):
                    result={"outcome":"sql","reply":"Here is a schema-checked query for the requested monthly POP breakdown.","sql":fallback[0],"explanation":fallback[1]}
                else:
                    result={"outcome":"clarify","reply":payload.get("follow_up") or payload.get("reply") or "Which table or field should I use?","sql":None,"explanation":None}
            elif outcome=="other" and not data_request: result={"outcome":"other","reply":OTHER_REPLY,"sql":None,"explanation":None}
            elif data_request:
                fallback=build_supported_pop_monthly_sql(question,schema,validator_dialect,current_date)
                if fallback and safe_sql(fallback[0]) and not validate_sql_schema(fallback[0],schema,w["database_name"],validator_dialect,True):
                    result={"outcome":"sql","reply":"Here is a schema-checked query for the requested monthly POP breakdown.","sql":fallback[0],"explanation":fallback[1]}
                else:
                    result={"outcome":"clarify","reply":payload.get("follow_up") or "I can write SQL for this request, but I need one detail about which field or business measure to use.","sql":None,"explanation":None}
            else: result={"outcome":"error","reply":"The AI returned an unsupported response. Please try the request again.","sql":None,"explanation":None}
        except Exception: result={"outcome":"error","reply":"The AI could not complete that request. Check the model API key and try again.","sql":None,"explanation":None}
    result["dialect"]=output_dialect
    result["workspace_database_type"]=w["database_type"]
    result["dialect_matches_workspace"]=True
    with store() as db: save_message(db,c["id"],"assistant",result["reply"],result["outcome"],result.get("sql"),result.get("explanation"),output_dialect)
    return result

