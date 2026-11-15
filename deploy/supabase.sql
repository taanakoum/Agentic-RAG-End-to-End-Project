-- Fresh project only. Review in Supabase SQL Editor; this is NOT run by the app.
-- The role starts without a password and cannot sign in until you set one separately.
BEGIN;
CREATE ROLE rag_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
GRANT CONNECT ON DATABASE postgres TO rag_app;
CREATE SCHEMA rag AUTHORIZATION rag_app;
REVOKE ALL ON SCHEMA rag FROM PUBLIC, anon, authenticated;
ALTER ROLE rag_app SET search_path TO rag;
COMMIT;
-- Set a unique password through your administrative tooling before connecting.
-- Use POSTGRES_USER=rag_app.<PROJECT_REF> with Supavisor session pooler port 5432.
-- POSTGRES_SCHEMA=rag. Keep rag OUT of Supabase's Exposed schemas list.
