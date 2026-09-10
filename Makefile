.PHONY: up down logs ps restart build health psql prune
up:      ; docker compose up -d --build
down:    ; docker compose down
logs:    ; docker compose logs -f --tail=100
ps:      ; docker compose ps
restart: ; docker compose restart
build:   ; docker compose build
health:  ; curl -s https://job-agent.tailf137cc.ts.net/health | jq
psql:    ; docker compose exec postgres psql -U $$(grep POSTGRES_USER .env | cut -d= -f2) -d $$(grep POSTGRES_DB .env | cut -d= -f2)
prune:   ; docker system prune -af --volumes=false && docker builder prune -af
