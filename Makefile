.PHONY: up down logs ps restart build health psql prune
up:      ; docker compose up -d --build
down:    ; docker compose down
logs:    ; docker compose logs -f --tail=100
ps:      ; docker compose ps
restart: ; docker compose restart
build:   ; docker compose --profile tools build
health:  ; curl -s https://job-agent.tailf137cc.ts.net/health | jq
psql:    ; docker compose exec postgres psql -U $$(grep POSTGRES_USER .env | cut -d= -f2) -d $$(grep POSTGRES_DB .env | cut -d= -f2)
prune:   ; docker system prune -af --volumes=false && docker builder prune -af

.PHONY: migrate migrate-down migrate-status revision bootstrap grants seed backup restore-test psql-ro
bootstrap:      ; @DB=$$(grep '^POSTGRES_DB=' .env | cut -d= -f2); SU=$$(grep '^POSTGRES_USER=' .env | cut -d= -f2); \
                  docker compose exec -T postgres sh -c "PGPASSWORD=\$$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $$SU -d $$DB -v DBNAME=$$DB -v ON_ERROR_STOP=1" < ops/sql/00-bootstrap.sql
grants:         ; @DB=$$(grep '^POSTGRES_DB=' .env | cut -d= -f2); SU=$$(grep '^POSTGRES_USER=' .env | cut -d= -f2); \
                  docker compose exec -T postgres sh -c "PGPASSWORD=\$$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $$SU -d $$DB -v ON_ERROR_STOP=1" < ops/sql/10-grants.sql
seed:           ; @DB=$$(grep '^POSTGRES_DB=' .env | cut -d= -f2); SU=$$(grep '^POSTGRES_USER=' .env | cut -d= -f2); \
                  docker compose exec -T postgres sh -c "PGPASSWORD=\$$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $$SU -d $$DB -v ON_ERROR_STOP=1" < ops/sql/20-seed.sql
migrate:        ; docker compose run --rm migrator alembic upgrade head
migrate-down:   ; docker compose run --rm migrator alembic downgrade -1
migrate-status: ; docker compose run --rm migrator alembic current -v
revision:       ; docker compose run --rm migrator alembic revision -m "$(m)"
psql-ro:        ; docker compose exec -e PGPASSWORD=$$(cat /opt/job-agent/secrets/ja_readonly_password) postgres psql -h 127.0.0.1 -U ja_readonly -d $$(grep '^POSTGRES_DB=' .env | cut -d= -f2)
backup:         ; sudo /opt/job-agent/bin/pg-backup.sh
restore-test:   ; sudo /opt/job-agent/bin/pg-restore-test.sh
.PHONY: profile-check profile-load
profile-check: ; docker compose run --rm profile-loader python -m job_agent.profile.loader
profile-load:  ; docker compose run --rm profile-loader
.PHONY: test
test: ; docker compose run --rm worker python -m pytest tests/unit -q -p no:cacheprovider
