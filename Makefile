VERSION := $(shell grep '^version' pyproject.toml | cut -d'"' -f2)
WHEEL := dist/agentevals-$(VERSION)-py3-none-any.whl

.PHONY: build build-bundle build-ui release clean dev-backend dev-frontend dev-bundle test

build:
	uv build

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

clean:
	rm -rf dist/ build/ src/agentevals/_static/ ui/dist/
	find . -name '*.egg-info' -type d -exec rm -rf {} + 2>/dev/null || true
