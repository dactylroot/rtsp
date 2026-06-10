
## Publishing

Releases are published to PyPI automatically via GitHub Actions using OIDC trusted publishing (no API tokens stored).

### To release a new version

1. Update `version` in `pyproject.toml`
2. Add an entry to `CHANGELOG.md`
3. Commit and push to `main`
4. Tag the commit and push the tag:

```
git tag v2.0.1 -m "v2.0.1"
git push origin v2.0.1
```

The `publish.yml` workflow triggers on any `v*` tag, builds the package, and uploads to PyPI.

### Prerequisites (one-time setup)

The PyPI project must have a trusted publisher configured at https://pypi.org/manage/project/rtsp/settings/publishing/:

- **Owner**: `dactylroot`
- **Repository**: `rtsp`
- **Workflow filename**: `publish.yml`
- **Environment**: `pypi`

### Local build (for testing)

```
pip install build
python -m build
```

Artifacts are written to `dist/`. To test against TestPyPI before a real release:

```
pip install twine
twine upload --repository-url https://test.pypi.org/legacy/ dist/*
```
