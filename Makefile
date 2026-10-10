.PHONY: dev run test test-backend test-frontend test-e2e openapi openapi-check

UV := uv
# corepack must run inside frontend/ so it honours the pinned packageManager.
FE := cd frontend && corepack pnpm

dev: ## backend (uvicorn --reload) + Vite dev server, Ctrl-C stops both
	@trap 'kill 0' INT TERM EXIT; \
	  $(UV) run uvicorn --app-dir backend app.main:app --reload --host 127.0.0.1 --port 8000 & \
	  $(FE) dev & \
	  wait

run: ## build the UI, serve everything on 127.0.0.1:8000
	$(FE) build
	$(UV) run uvicorn --app-dir backend app.main:app --host 127.0.0.1 --port 8000

test: test-backend test-frontend test-e2e openapi-check ## pytest + Vitest (+ Playwright where present)

test-backend:
	$(UV) run pytest backend/tests

test-frontend:
	$(FE) test:run

test-e2e:
	@if [ -d frontend/dist ] && ls "$$HOME"/.cache/ms-playwright/chromium* >/dev/null 2>&1; then \
	  $(FE) exec playwright test --reporter=line; \
	else \
	  echo "skip playwright (needs frontend/dist and installed browsers)"; \
	fi

openapi: ## export the OpenAPI schema and regenerate the frontend types
	$(UV) run python -c "import json; from app.main import app; print(json.dumps(app.openapi(), indent=2, sort_keys=True))" > openapi.json
	$(FE) run gen:types

openapi-check: ## fail when committed openapi.json drifts from the app
	@$(UV) run python -c "import json; from app.main import app; print(json.dumps(app.openapi(), indent=2, sort_keys=True))" \
		| diff -u openapi.json - \
		&& echo "openapi.json is up to date"
