#!/bin/sh
# Generated from topology.yaml. Runs once, when the PostgreSQL data directory is created.
# Service accounts keep a password in the database; every other login is checked against the
# directory, so the same person and the same password work in ClickHouse and PostgreSQL.
set -e
{
cat <<'RULES'
host app postgres all scram-sha-256
host app /^svc_.* all scram-sha-256
host app all all ldap ldapserver=ldap ldapport=389 ldapprefix="cn=" ldapsuffix=",ou=people,dc=charlie,dc=local"
RULES
cat "$PGDATA/pg_hba.conf"
} > /tmp/pg_hba.new
cat /tmp/pg_hba.new > "$PGDATA/pg_hba.conf"
