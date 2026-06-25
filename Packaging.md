
## Publishing

Releases are published to PyPI automatically via GitHub Actions using OIDC trusted publishing (no API tokens stored).

### To release a new version

1. Update `version` in `pyproject.toml`
2. Add an entry to `CHANGELOG.md`
3. Commit and push to `main`
4. Create and publish a GitHub Release (the tag value does not affect the published version - that comes from `pyproject.toml`)

The `publish.yml` workflow triggers when the release is published. Tests run across Python 3.10-3.14 first; on success the package is built and uploaded to PyPI.

