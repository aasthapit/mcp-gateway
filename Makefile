# MCP gateway — developer entrypoints. See docs/PLAN.md §3.7.
SHELL := /bin/bash
.DEFAULT_GOAL := help

# `podman compose` delegates to docker-compose when installed. Override for Docker:
#   make up COMPOSE="docker compose"
COMPOSE ?= $(shell command -v podman >/dev/null 2>&1 && echo "podman compose" || echo "docker compose")
LOCAL   := local
ENVFILE := $(LOCAL)/.env
CF      := $(COMPOSE) -f $(LOCAL)/compose.yaml --env-file $(ENVFILE)
PY      ?= python3

# Keycloak (host-visible). Overridable: make token KC_USER=admin1 KC_PASS=admin1
# (not USER/PASS: the shell exports USER as the login name and would win over ?=)
KC_PORT ?= $(shell grep -E '^KEYCLOAK_PORT=' $(ENVFILE) 2>/dev/null | cut -d= -f2 || echo 8180)
KC      := http://localhost:$(KC_PORT)/realms/mcp
KC_USER ?= dev1
KC_PASS ?= $(KC_USER)

.PHONY: help env up down logs ps restart seed validators validate-seed token token-ci decode mongosh smoke lint fmt pre-commit clean \
	deploy-check deploy-lint deploy-template deploy-kubeconform deploy-policy deploy-schemas deploy-image

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

env: ## create local/.env from the example if missing
	@test -f $(ENVFILE) || { cp $(LOCAL)/.env.example $(ENVFILE); echo "created $(ENVFILE)"; }

up: env validators ## build + start the stack, initiate the replica set, apply validators
	$(CF) up -d --build --remove-orphans
	$(CF) wait mongo-init >/dev/null 2>&1 || $(CF) run --rm -T mongo-init
	@$(CF) logs --no-log-prefix mongo-init | tail -n 5
	@printf 'waiting for keycloak realm import'; for i in $$(seq 1 60); do \
	  curl -fs -o /dev/null $(KC)/.well-known/openid-configuration && break; printf '.'; sleep 2; done; echo
	@curl -fsS -o /dev/null $(KC)/.well-known/openid-configuration || { echo "keycloak not ready at $(KC); see: make logs SVC=keycloak"; exit 1; }
	@echo "stack up: mongo :27017, keycloak :$(KC_PORT), sample-public :8081, sample-private :8082"

down: ## stop and remove containers (keeps volumes)
	$(CF) down --remove-orphans

clean: ## stop and remove containers AND volumes (fresh Mongo + Keycloak)
	$(CF) down --remove-orphans --volumes

logs: ## follow logs (SVC=name to filter)
	$(CF) logs -f $(SVC)

ps: ## container status
	$(CF) ps

restart: down up ## down + up

validators: ## regenerate local/mongo-init/validators.js from schemas/
	$(PY) schemas/build-mongo-validators.py

validate-seed: ## validate local/seed/*.json against schemas/ (needs `pip install jsonschema`)
	$(PY) schemas/build-mongo-validators.py --check --validate-seed

seed: env ## load local/seed/*.json (w:majority) and bump meta.version
	$(CF) run --rm -T mongo-init bash -ec 'mongosh --quiet "$$MONGO_URI" /seed/seed.js'

mongosh: env ## interactive mongosh against the local replica set
	$(CF) run --rm mongo-init bash -ec 'exec mongosh "$$MONGO_URI"'

token: ## print an access token for KC_USER (default dev1) via the password grant
	@curl -fsS -d client_id=mcp-gateway -d grant_type=password -d username=$(KC_USER) -d password=$(KC_PASS) \
	  $(KC)/protocol/openid-connect/token | $(PY) -c 'import sys,json; print(json.load(sys.stdin)["access_token"])'

token-ci: ## print a client-credentials token for ci-runner
	@curl -fsS -d client_id=ci-runner -d client_secret=ci-runner-secret -d grant_type=client_credentials \
	  $(KC)/protocol/openid-connect/token | $(PY) -c 'import sys,json; print(json.load(sys.stdin)["access_token"])'

decode: ## decode the claims of a token: make decode TOKEN=... (defaults to `make token`)
	@tok="$(TOKEN)"; [ -n "$$tok" ] || tok=$$($(MAKE) -s token); \
	$(PY) -c 'import sys,base64,json; p=sys.argv[1].split(".")[1]; print(json.dumps(json.loads(base64.urlsafe_b64decode(p+"="*(-len(p)%4))), indent=2))' "$$tok"

smoke: env ## end-to-end checks: infra, then a guest MCP client through the gateway (GATEWAY_URL=... to test a running one)
	COMPOSE="$(COMPOSE)" $(LOCAL)/smoke.sh
	@echo "gateway (guest client)"
	@cd gateway && uv run --frozen python -m mcp_gateway.smoke $(GATEWAY_URL)

lint: ## ruff + prettier + schema check via pre-commit
	pre-commit run --all-files --show-diff-on-failure

fmt: ## auto-format (ruff format, prettier)
	pre-commit run ruff-format --all-files || true
	pre-commit run prettier --all-files || true

pre-commit: ## install the git hook
	pre-commit install

# ---------------------------------------------------------------------------------------
# deploy/ (WP6): Helm charts + OpenShift manifests. See deploy/README.md.
# ---------------------------------------------------------------------------------------
GATEWAY_IMAGE ?= $(shell sed -n 's/^  repository: *//p' deploy/helm/mcp-gateway/values.yaml | head -1)
GATEWAY_TAG   ?= $(shell sed -n 's/^appVersion: *"\{0,1\}\([^"]*\)"\{0,1\}/\1/p' deploy/helm/mcp-gateway/Chart.yaml)

deploy-check: ## lint + template + kubeconform + conftest for deploy/ (needs helm, kubeconform, conftest)
	deploy/deploy-check.sh

deploy-lint: ## helm lint both charts with every values overlay
	@for c in mcp-gateway mcp-admin; do for v in "" values-dev.yaml values-prod.yaml; do \
	  f=""; [ -n "$$v" ] && [ -f deploy/helm/$$c/$$v ] && f="-f deploy/helm/$$c/$$v"; helm lint --strict deploy/helm/$$c $$f || exit 1; done; done

deploy-template: ## render both charts (ENV=dev|prod|staging, default prod) to stdout
	@env="$${ENV:-prod}"; for c in mcp-gateway mcp-admin; do \
	  f="-f deploy/helm/$$c/values-prod.yaml"; [ "$$env" = dev ] && f="-f deploy/helm/$$c/values-dev.yaml"; \
	  [ "$$env" = staging ] && f="-f deploy/helm/$$c/values-prod.yaml -f deploy/helm/$$c/values-staging.yaml"; \
	  helm template $$c deploy/helm/$$c -n mcp-gateway $$f; done

deploy-schemas: ## (re)fetch CRD JSON schemas for kubeconform into deploy/kubeconform/cache
	deploy/kubeconform/fetch-schemas.sh

deploy-kubeconform: deploy-schemas ## kubeconform on the current renders (run deploy-check first)
	kubeconform -strict -summary -schema-location default \
	  -schema-location 'deploy/kubeconform/cache/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' deploy/.render/*.yaml

deploy-policy: ## conftest restricted-SCC policy on the current renders + unit tests
	conftest verify --policy deploy/policy
	conftest test --policy deploy/policy --data deploy/policy/data-prod.yaml deploy/.render/*-prod.yaml

deploy-image: ## build the gateway image with podman under the chart's image name:tag
	podman build -t $(GATEWAY_IMAGE):$(GATEWAY_TAG) -f gateway/Containerfile gateway
	@echo "built $(GATEWAY_IMAGE):$(GATEWAY_TAG)  (chart: deploy/helm/mcp-gateway image.repository + appVersion)"
