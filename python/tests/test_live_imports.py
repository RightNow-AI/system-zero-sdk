"""The package imports using only its declared runtime dependency closure."""

import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


PACKAGE = Path(__file__).resolve().parents[1] / "src/system_zero"
RUNTIME_ROOTS = {"httpx", "httpcore", "websockets", "anyio", "certifi", "h11", "idna",
                 "typing_extensions", "exceptiongroup"}


class LiveImportTests(unittest.TestCase):
    def test_function_tools_load_resources_directly_from_a_zip(self):
        scratch = Path.cwd() / "tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="zip-import-", dir=scratch) as temporary:
            archive = Path(temporary) / "sdk.zip"
            with zipfile.ZipFile(archive, "w") as output:
                for path in PACKAGE.iterdir():
                    if path.suffix in {".py", ".json"} or path.name == "py.typed":
                        output.writestr("system_zero/" + path.name, path.read_bytes())
            script = "import sys; sys.path.insert(0, sys.argv[1]); import system_zero.tools as tools; assert len(tools.NESTED_FUNCTION_TOOLS) == len(tools.INPUT_SCHEMA_TOOLS) == 2"
            result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(archive)],
                                    capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_absolute_sdk_imports_are_standard_library_or_declared_dependencies(self):
        allowed = set(sys.stdlib_module_names) | {"httpx", "httpcore", "websockets", "system_zero"}
        unexpected = set()
        for path in PACKAGE.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    unexpected.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    unexpected.add(node.module.split(".")[0])
        self.assertEqual(unexpected - allowed, set())

    def test_import_with_site_disabled_and_only_runtime_dependencies_admitted(self):
        dependency_paths = set()
        for name in RUNTIME_ROOTS:
            found = importlib.util.find_spec(name)
            if found is None or found.origin is None:
                continue
            origin = Path(found.origin).resolve()
            dependency_paths.add(str(origin.parent.parent if found.submodule_search_locations else origin.parent))
        script = '''import importlib.abc, json, sys
from pathlib import Path
package_parent, dependency_paths, runtime_roots = json.loads(sys.argv[1])
allowed = set(sys.stdlib_module_names) | set(sys.builtin_module_names) | set(runtime_roots) | {"system_zero"}
class RuntimeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] not in allowed:
            raise ModuleNotFoundError("An undeclared runtime dependency was imported.")
        return None
sys.meta_path.insert(0, RuntimeImports())
sys.path[:0] = [package_parent, *dependency_paths]
import system_zero
import system_zero.tools
assert callable(system_zero.SystemZero)
assert callable(system_zero.tools.create_tool_handler)
print("runtime import passed")
'''
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", script,
                                 json.dumps([str(PACKAGE.parent), sorted(dependency_paths), sorted(RUNTIME_ROOTS)])],
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "runtime import passed")


if __name__ == "__main__":
    unittest.main()
