
## Preparation

    python3 -m pip install --user --upgrade setuptools wheel
    python3 -m pip install --user --upgrade twine

## Distribution

  1. Build: `python3 setup.py sdist bdist_wheel`
  2. Twine Distribution:
      * `twine upload dist/*`
        * [Preview](https://pypi.org/project/rtsp/)
      * `twine upload --repository-url https://test.pypi.org/legacy/ dist/*`
        * [Preview](https://test.pypi.org/project/rtsp/) 
  3. Distribute github backup:
    1. `git tag 1.2.3 -m "tag for PyPI"`
    2. `git push --tags remotename branchname`
