"""setup.py 兼容入口。

说明：
- 依赖与元数据以 pyproject.toml 为唯一真相源（SSOT）。
- 本文件仅用于兼容旧安装流程，不再手写 install_requires。
"""

from pathlib import Path

from setuptools import find_packages, setup

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent
PYPROJECT = ROOT / "pyproject.toml"


def _load_project_metadata() -> dict:
    with PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    return data["project"]


project = _load_project_metadata()

setup(
    name=project["name"],
    version=project["version"],
    description=project.get("description", ""),
    python_requires=project.get("requires-python"),
    install_requires=project.get("dependencies", []),
    extras_require=project.get("optional-dependencies", {}),
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    entry_points={
        "console_scripts": [
            "akshare-mcp=akshare_mcp.server:main",
        ],
    },
)
