#!/bin/sh
# Runs once, when the `db` container creates its cluster on an empty volume
# (the postgres image runs /docker-entrypoint-initdb.d/* then, and never again).
#
# The app gets its own login role that owns only its database and is not a
# superuser. The superuser `postgres` loses its password, so it can log in only
# through the socket inside the container (`docker compose exec db psql -U postgres`),
# never over the network.
psql --username=postgres --dbname=postgres -v ON_ERROR_STOP=1 \
    -v pw="${TEAM_NOTES_DB_PASSWORD:?TEAM_NOTES_DB_PASSWORD must be set}" << 'SQL'
CREATE ROLE team_notes LOGIN PASSWORD :'pw';
CREATE DATABASE team_notes OWNER team_notes;
ALTER ROLE postgres PASSWORD NULL;
SQL
