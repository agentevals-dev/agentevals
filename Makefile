VERSION := $(shell grep '^version' pyproject.toml | cut -d'"' -f2)
WHEEL := dist/agentevals_cli-$(VERSION)-py3-none-any.whl

DOCKER_REGISTRY ?= soloio
DOCKER_IMAGE ?= agentevals
DOCKER_TAG ?= $(VERSION)
DOCKER_IMAGE_REF := $(if $(DOCKER_REGISTRY),$(DOCKER_REGISTRY:%/=%)/$(DOCKER_IMAGE),$(DOCKER_IMAGE))

# Multi-arch build (requires docker buildx). Manifest lists must be pushed — use build-docker-local for a single-arch --load.
PLATFORMS ?= linux/amd64,linux/arm64

HELM_REPO ?= oci://ghcr.io/agentevals-dev/agentevals
HELM_DIST_FOLDER ?= dist/helm
HELM_CHART_DIR ?= charts/agentevals
HELM_CHART_OCI_URL ?= $(HELM_REPO)/helm
HELM_CHART_VERSION ?= $(VERSION)

.PHONY: build build-bundle build-docker build-ui release clean dev-backend dev-frontend dev-bundle test test-unit test-integration test-e2e helm-lint helm-template helm-test helm-cleanup helm-package helm-publish

build:
	uv build

build-docker:
	docker buildx build --platform $(PLATFORMS) -t $(DOCKER_IMAGE_REF):$(DOCKER_TAG) --push .

build-ui:
	cd ui && npm ci && npm run build

build-bundle: build-ui
	rm -rf src/agentevals/_static
	cp -r ui/dist src/agentevals/_static
	uv build
	rm -rf src/agentevals/_static

CORE_WHEEL_NAME := agentevals-$(VERSION)-core-py3-none-any.whl
BUNDLE_WHEEL_NAME := agentevals-$(VERSION)-bundle-py3-none-any.whl

release: clean build-ui
	mkdir -p dist/core dist/bundle
	uv build
	mv $(WHEEL) dist/core/$(CORE_WHEEL_NAME)
	mv dist/*.tar.gz dist/core/
	rm -rf src/agentevals/_static
	cp -r ui/dist src/agentevals/_static
	uv build
	mv $(WHEEL) dist/bundle/$(BUNDLE_WHEEL_NAME)
	mv dist/*.tar.gz dist/bundle/
	rm -rf src/agentevals/_static
	@echo "Built:"
	@echo "  core:   dist/core/$(CORE_WHEEL_NAME)"
	@echo "  bundle: dist/bundle/$(BUNDLE_WHEEL_NAME)"

dev-backend:
	uv run agentevals serve --dev

dev-frontend:
	cd ui && npm run dev

dev-bundle: build-ui
	rm -rf src/agentevals/_static
	cp -r ui/dist src/agentevals/_static
	uv run agentevals serve; rm -rf src/agentevals/_static

test:
	uv run pytest

test-unit:
	uv run pytest tests/ --ignore=tests/integration

test-integration:
	uv run pytest tests/integration/ -m "integration and not e2e" -v

test-e2e:
	uv run pytest tests/integration/ -m "e2e" -v

clean:
	rm -rf dist/ build/ src/agentevals/_static/ ui/dist/
	find . -name '*.egg-info' -type d -exec rm -rf {} + 2>/dev/null || true

.PHONY: helm-lint
helm-lint:
	helm lint "$(HELM_CHART_DIR)"

# Render templates to catch YAML/Helm errors (default values + ephemeralVolume disabled path).
.PHONY: helm-template
helm-template:
	helm template agentevals "$(HELM_CHART_DIR)" --namespace agentevals >/dev/null
	helm template agentevals "$(HELM_CHART_DIR)" --namespace agentevals \
		--set ephemeralVolume.enabled=false >/dev/null

.PHONY: helm-test
helm-test: helm-lint helm-template

.PHONY: helm-cleanup
helm-cleanup:
	rm -f $(HELM_DIST_FOLDER)/agentevals-*.tgz

.PHONY: helm-package
helm-package: helm-cleanup
	mkdir -p $(HELM_DIST_FOLDER)
	helm package "$(HELM_CHART_DIR)" -d "$(HELM_DIST_FOLDER)" \
		--version "$(HELM_CHART_VERSION)" --app-version "$(HELM_CHART_VERSION)"

.PHONY: helm-publish
helm-publish: helm-package
	helm push "$(HELM_DIST_FOLDER)/agentevals-$(HELM_CHART_VERSION).tgz" "$(HELM_CHART_OCI_URL)"
