"""
    Run all scripts in TEST_DIRECTORY
    Each script:
      * should import and test a module from WORKING_DIR
      * need to print, assert, or throw error on failed tests.
      * don't have to announce themselves "Passed"
"""

import sys
from os import listdir, path, getcwd, chdir
from importlib import import_module
import traceback

TEST_DIRECTORY = path.dirname(__file__)
WORKING_DIR = getcwd()

testdir = path.abspath(TEST_DIRECTORY)
modules = [path.splitext(filename)[0] for filename in listdir(testdir) if path.splitext(filename)[1]=='.py']
modules = list(set(modules)-set(['__init__','__main__']))
modules.sort()

print("Testing modules [{}]:\n".format(','.join(modules)))

sys.path.append(path.abspath(WORKING_DIR))

for module in modules:
    try:
        import_module(module)
        print("tesd PASS - module {}".format(module))
    except Exception as e:
        print("tesd FAIL - module {}:".format(module))
        print("  {}".format(traceback.format_exc()))

