.PHONY: setup models up down hardware probe seed ingest scenarios evaluate-smoke evaluate test test-fakecloud backend frontend observability compose-up

NODE = fnm exec --using=default --

setup:
	uv sync
	cd frontend && $(NODE) npm install
	test -f frontend/.env.local || cp frontend/.env.example frontend/.env.local

models:
	ollama pull qwen3:4b-instruct
	ollama pull qwen3:1.7b

up:
	docker compose up -d --wait postgres fakecloud

down:
	docker compose --profile observability down

hardware:
	uv run python -m backend.hardware

probe: up
	uv run python scripts/probe_fakecloud.py

seed: up
	uv run python scripts/seed_fakecloud.py

ingest: up
	uv run python scripts/ingest_docs.py

scenarios:
	uv run python scripts/generate_scenarios.py

# The evaluation resets FakeCloud before every scenario, so the demo is reseeded afterwards.
evaluate-smoke: up
	uv run python scripts/run_evaluation.py --subset smoke; status=$$?; uv run python scripts/seed_fakecloud.py > /dev/null; exit $$status

evaluate: up
	uv run python scripts/run_evaluation.py --subset all; status=$$?; uv run python scripts/seed_fakecloud.py > /dev/null; exit $$status

observability:
	docker compose --profile observability up -d prometheus grafana
	@echo "Prometheus: http://localhost:9090   Grafana: http://localhost:3001"

compose-up:
	docker compose up -d --build --wait

backend:
	uv run python -m backend.main

frontend:
	cd frontend && $(NODE) npm run dev

test:
	uv run pytest
	cd frontend && $(NODE) npm run lint

test-fakecloud: up
	uv run pytest -m fakecloud
	uv run python scripts/seed_fakecloud.py
