# Scripts Folder

This folder contains small helper scripts for local development.

## `db.js`

`db.js` is a database helper for local setup and recovery.

Its job is to run the SQL files in this repository against the PostgreSQL container started by Docker Compose, without requiring PowerShell-specific commands or manual `psql` piping.

It exists mainly to make these package scripts work the same way across macOS, Linux, and Windows:

- `npm run migrate`
- `npm run seed`
- `npm run db:reset`

## What it does

### `npm run migrate`

Runs the main schema file:

- `backend/database/schema.sql`

That file creates the database objects the app needs, such as:

- tables
- enums
- indexes
- triggers
- views

If PostgreSQL is still starting up, the script retries several times before failing.

### `npm run seed`

Runs the seed SQL files in order:

- `backend/database/seeds/01_roles.sql`
- `backend/database/seeds/02_users.sql`
- `backend/database/seeds/03_widget_types.sql`
- `backend/database/seeds/04_approval_rules.sql`
- `backend/database/seeds/05_service_health.sql`

These files load initial data used by the app, such as:

- roles
- test users
- widget definitions
- approval rules
- service health rows

### `npm run db:reset`

This is the destructive option.

It does the following:

1. Stops Compose services and removes database volumes.
2. Starts fresh `postgres` and `redis` containers.
3. Runs migrations.
4. Runs seeds.

Use it when your local DB is in a bad state and you want to rebuild it from scratch.

## How it works

You do not need to know JavaScript to use it, but it helps to know the high-level flow.

### 1. It reads local DB settings

The script checks:

- shell environment variables
- then the local `.env` file

It uses those values to figure out which database name and user it should try.

### 2. It tries to detect the correct PostgreSQL user

Different local setups may use different DB usernames.

Instead of assuming only one value, the script tries likely options, including:

- `POSTGRES_USER` from the environment
- `POSTGRES_USER` from `.env`
- fallback values such as `devops_user` and `devops`

The first one that can connect successfully is used for the rest of the run.

### 3. It executes SQL through Docker Compose

The SQL files stay in the repository on your machine.

The script reads each SQL file as plain text and sends it into:

- `docker compose exec -T postgres psql ...`

The script passes the SQL file contents directly to `psql` over standard input.

That means it does not depend on:

- PowerShell
- shell-specific piping syntax
- the SQL files being mounted inside the Postgres container

It also avoids a common failure mode where `psql` starts without input and appears to hang while waiting for an interactive session.

### 4. It fails fast when something important is wrong

Examples:

- Docker is not running
- the `postgres` service is not available
- the SQL file does not exist
- no working DB user can be found

When that happens, it stops and prints the failing command or error message.

## When to use it

Typical flow for local setup:

1. Start the containers.
2. Run `npm run migrate`.
3. Run `npm run seed`.
4. Restart the API if needed.

If you want a clean rebuild of the local database, use:

- `npm run db:reset`

## Important notes

### Seeds are not meant to be endlessly re-run

The seed files insert fixed records.

If you run them again against an already-seeded database, you may get duplicate key or unique constraint errors.

If that happens, either:

- reset the DB first with `npm run db:reset`
- or manually clear the affected tables before seeding again

### `db:reset` only prepares the database side

`db:reset` starts `postgres` and `redis`, then rebuilds the database contents.

It does not start every app service for you. If the backend is not running afterward, start or restart it separately.

### The script is local-development focused

This helper is intended for developer machines using Docker Compose.

It is not a production migration tool.

## If something fails

Useful checks:

- `docker compose ps`
- `docker compose logs postgres`
- verify your `.env` values
- verify the database container is using the user you expect

If auth still does not work after migration and seeding, the first thing to check is whether the `users` table exists and whether the seed users were inserted successfully.
