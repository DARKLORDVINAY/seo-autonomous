\set ON_ERROR_STOP on

-- This Compose database is an application-dedicated cluster. PostgreSQL gives
-- PUBLIC CONNECT and TEMPORARY on every database by default; remove those
-- ambient capabilities cluster-wide before runtime roles are provisioned.
-- Database owners retain access, and grant_runtime.py grants each runtime role
-- explicit CONNECT only to the selected application database.
SELECT format(
    'REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC',
    datname
)
FROM pg_database
WHERE datallowconn
ORDER BY datname
\gexec
