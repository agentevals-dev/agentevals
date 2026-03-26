VERSION := $(shell grep '^version' pyproject.toml | cut -d'"' -f2)
WHEEL := dist/agentevals_cli-$(VERSION)-py3-none-any.whl

DOCKER_REGISTRY ?= soloio
DOCKER_IMAGE ?= agentevals
DOCKER_TAG ?= $(VERSION)
DOCKER_IMAGE_REF := $(if $(DOCKER_REGISTRY),$(DOCKER_REGISTRY:%/=%)/$(DOCKER_IMAGE),$(DOCKER_IMAGE))

# Multi-arch build (requires docker buildx). Manifest lists must be pushed — use build-docker-local for a single-arch --load.
PLATFORMS ?= linux/amd64,linux/arm64

.PHONY: build build-bundle build-docker build-ui release clean dev-backend dev-frontend dev-bundle test test-unit test-integration test-e2e

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
