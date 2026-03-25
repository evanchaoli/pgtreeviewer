#!/usr/bin/env python3
"""Local web server for PostgreSQL tree viewer with OID resolution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent

QUERY_BY_KIND = {
    "relid": """
        SELECT c.oid::text,
               quote_ident(n.nspname) || '.' || quote_ident(c.relname)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.oid = ANY (ARRAY[{ids}]::oid[])
        ORDER BY c.oid
    """,
    "nspid": """
        SELECT n.oid::text,
               quote_ident(n.nspname)
        FROM pg_namespace n
        WHERE n.oid = ANY (ARRAY[{ids}]::oid[])
        ORDER BY n.oid
    """,
    "opno": """
        SELECT o.oid::text,
               quote_ident(n.nspname) || '.' || quote_ident(o.oprname) ||
               '(' || pg_catalog.format_type(o.oprleft, NULL) || ',' ||
               pg_catalog.format_type(o.oprright, NULL) || ')'
        FROM pg_operator o
        JOIN pg_namespace n ON n.oid = o.oprnamespace
        WHERE o.oid = ANY (ARRAY[{ids}]::oid[])
        ORDER BY o.oid
    """,
    "funcid": """
        SELECT p.oid::text,
               p.oid::regprocedure::text
        FROM pg_proc p
        WHERE p.oid = ANY (ARRAY[{ids}]::oid[])
        ORDER BY p.oid
    """,
    "typeid": """
        SELECT t.oid::text,
               t.oid::regtype::text
        FROM pg_type t
        WHERE t.oid = ANY (ARRAY[{ids}]::oid[])
        ORDER BY t.oid
    """,
    "collid": """
        SELECT c.oid::text,
               quote_ident(n.nspname) || '.' || quote_ident(c.collname)
        FROM pg_collation c
        JOIN pg_namespace n ON n.oid = c.collnamespace
        WHERE c.oid = ANY (ARRAY[{ids}]::oid[])
        ORDER BY c.oid
    """,
}


def _int_list(raw):
    out = []
    for v in raw or []:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            out.append(iv)
    return sorted(set(out))


def run_lookup(connection: dict, kind: str, oids: list[int]) -> dict[str, str]:
    psql = shutil.which("psql")
    if not psql:
        raise RuntimeError("psql not found in PATH")

    ids_sql = ",".join(str(x) for x in oids)
    sql = QUERY_BY_KIND[kind].format(ids=ids_sql)

    connection = dict(connection or {})
    if not (connection.get("database") or "").strip():
        connection["database"] = "postgres"

    def run_with_env(conn: dict):
        env = os.environ.copy()
        conn_map = {
            "host": "PGHOST",
            "port": "PGPORT",
            "database": "PGDATABASE",
            "user": "PGUSER",
            "password": "PGPASSWORD",
            "application_name": "PGAPPNAME",
        }
        for src, dst in conn_map.items():
            val = (conn or {}).get(src)
            if val:
                env[dst] = str(val)
            else:
                env.pop(dst, None)

        return subprocess.run(
            [
                psql,
                "-X",
                "-A",
                "-t",
                "-F",
                "\t",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    proc = run_with_env(connection)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(stderr or f"psql failed with exit code {proc.returncode}")

    resolved = {}
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        oid, label = parts
        resolved[oid.strip()] = " ".join(label.split())

    return resolved


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_POST(self):
        if self.path != "/api/resolve":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)

        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
            return

        connection = payload.get("connection") or {}
        lookups = payload.get("lookups") or {}

        resolved = {}
        errors = []

        try:
            for kind, query in QUERY_BY_KIND.items():
                oids = _int_list(lookups.get(kind))
                if not oids:
                    continue
                try:
                    resolved[kind] = run_lookup(connection, kind, oids)
                except RuntimeError as exc:
                    errors.append(f"{kind}: {exc}")
        except Exception as exc:  # unexpected failure
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        self._json(HTTPStatus.OK, {"resolved": resolved, "errors": errors})

    def _json(self, status: HTTPStatus, payload: dict):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    host = os.environ.get("PG_TREE_HOST", "127.0.0.1")
    port = int(os.environ.get("PG_TREE_PORT", "8765"))
    with ThreadingHTTPServer((host, port), Handler) as server:
        print(f"Serving http://{host}:{port}")
        server.serve_forever()
