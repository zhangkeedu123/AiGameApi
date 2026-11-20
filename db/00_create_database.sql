-- 创建应用角色与数据库。以具备 CREATEROLE / CREATEDB 权限的账号执行。
--   psql -X -d postgres -v db_name=jinyao -v app_role=jinyao_app -f db/00_create_database.sql
-- 应用角色密码由环境变量 JINYAO_DB_PASSWORD 提供。
\set ON_ERROR_STOP on
\getenv app_password JINYAO_DB_PASSWORD

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_role', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role')\gexec

SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'app_role', :'app_password')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role')\gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'db_name', :'app_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db_name')\gexec

SELECT format('GRANT ALL PRIVILEGES ON DATABASE %I TO %I', :'db_name', :'app_role')\gexec
