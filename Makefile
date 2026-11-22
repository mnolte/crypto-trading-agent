.PHONY: dev run test test-backend test-frontend test-e2e soak openapi openapi-check ci ci-backend ci-frontend ci-api-types ci-e2e fix hooks

UV := uv
# corepack must run inside frontend/ so it honours the pinned packageManager.
FE := cd frontend && corepack pnpm

dev: ## backend (uvicorn --reload) + Vite dev server, Ctrl-C stops both
	@trap 'trap - INT TERM EXIT; kill 0' INT TERM EXIT; \
	  $(UV) run uvicorn --app-dir backend app.main:app --reload --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 45 & \
	  $(FE) dev & \
	  wait

run: ## build the UI, serve everything on 127.0.0.1:8000
	$(FE) build
	$(UV) run uvicorn --app-dir backend app.main:app --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 45

test: test-backend test-frontend test-e2e openapi-check ## pytest + Vitest (+ Playwright where present)

test-backend:
	$(UV) run pytest backend/tests -m "not slow" -n auto --dist load

soak: ## the 2000-cycle engine soak (slow marker, excluded from the suite)
	$(UV) run pytest backend/tests -m slow -v

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

ci: ci-backend ci-frontend ci-api-types ci-e2e ## run the same checks as CI jobs 1-4, in order

ci-backend:
	$(UV) sync --frozen
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy .
	$(UV) run pytest -m "not slow" -n auto --dist load

ci-frontend:
	$(FE) install --frozen-lockfile
	$(FE) lint
	$(FE) typecheck
	$(FE) test:run
	$(FE) build

ci-api-types:
	@$(MAKE) openapi
	@git status --porcelain -- openapi.json frontend/src/api/schema.d.ts
	@git diff --exit-code -- openapi.json frontend/src/api/schema.d.ts \
		&& echo "api types are up to date"

ci-e2e:
	$(FE) exec playwright install chromium
	$(FE) build
	$(FE) e2e

fix: ## fix lint and formatting in place (what the backend CI job checks)
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
	$(FE) lint --fix

hooks: ## install the pre-commit hook (ruff check --fix and ruff format on each commit)
	uvx pre-commit install
