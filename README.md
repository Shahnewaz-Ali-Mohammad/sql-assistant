# SQL Assistant

SQL Assistant is a local, schema-aware workspace for generating safe, read-only SQL from plain-language requests. Connect a MySQL/MariaDB, PostgreSQL, or SQL Server database, inspect its schema, and keep separate query conversations for every database workspace.

## Features

- One workspace per connected database
- Automatic database-engine and SQL-dialect detection
- MySQL/MariaDB, PostgreSQL, and SQL Server support
- Server-level database discovery when the database name is omitted
- Schema browser with incremental table loading
- Persistent workspaces and chat history in a private PostgreSQL store
- Encrypted database connection URLs at rest
- Schema-aware joins, column validation, and clarification questions
- Read-only SQL generation; generated statements are never executed
- One command starts the API, web app, and local storage

## Stack

- Next.js 15 and React 19
- FastAPI and Python
- PostgreSQL application storage
- Docker Compose for local storage

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer
- Docker Desktop with Docker Compose

## Installation

From the project directory:

```bash
python3 -m venv api/.venv
api/.venv/bin/pip install -r api/requirements.txt
cp -n api/.env.example api/.env
npm --prefix web install
cp -n web/.env.example web/.env.local
```

Add your model API key to `api/.env`:

```dotenv
OPENAI_API_KEY=your-key
```

The startup script creates private PostgreSQL credentials automatically and saves them in `api/.env`. It also creates a local encryption key in `api/.encryption.key` unless `APP_ENCRYPTION_KEY` is configured.

## Run

```bash
./start.sh
```

Open [http://localhost:3003](http://localhost:3003). Keep the terminal open and press `Ctrl+C` to stop the services.

## Connect a database

Select **Add workspace** and paste a connection URL. Include a database name to connect only that database. Omit the database name to discover every database accessible to the supplied account.

### MySQL or MariaDB

```text
mysql://user:password@127.0.0.1:3306/database_name
mysql://user:password@127.0.0.1:3306
```

### PostgreSQL

```text
postgresql://user:password@127.0.0.1:5432/database_name
postgresql://user:password@127.0.0.1:5432
```

### SQL Server

```text
sqlserver://user:password@127.0.0.1:1433/database_name
sqlserver://user:password@127.0.0.1:1433
```

URL-encode reserved characters in usernames and passwords. For example, encode `@` as `%40`.

SQL Server uses a pure-Python TDS driver and does not require unixODBC or a Microsoft ODBC driver. Add `?ssl=true` to require TLS certificate and hostname validation.

## How it works

1. A connection URL creates or refreshes a database workspace.
2. The API reads table, column, key, and relationship metadata without loading business rows.
3. The workspace stores its database engine, schema, conversations, and generated queries.
4. Plain-language requests are mapped against the workspace schema.
5. The resulting statement is checked for read-only safety, valid tables and columns, and the workspace’s SQL dialect.

## Data and security

- Connected database rows are not loaded into application storage.
- Generated SQL is returned for review and is never executed by SQL Assistant.
- Connection URLs are encrypted before being saved.
- Secrets, local keys, virtual environments, dependencies, and build output are excluded from Git.
- Each workspace maintains its own connection, schema cache, chats, and messages.
- The current release is intended for local single-user use and does not include authentication or access control.

## Environment variables

The example files document all supported settings:

- `api/.env.example`
- `web/.env.example`

Do not commit `api/.env`, `web/.env.local`, or `api/.encryption.key`.
