import ast
import importlib.util
import subprocess
import sys
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# Modules de la bibliothèque standard Python
STDLIB_MODULES = set(sys.stdlib_module_names) if hasattr(sys, 'stdlib_module_names') else {
    'os', 'sys', 'io', 're', 'json', 'time', 'math', 'random', 'datetime',
    'collections', 'itertools', 'functools', 'pathlib', 'typing', 'enum',
    'dataclasses', 'abc', 'copy', 'string', 'struct', 'hashlib', 'base64',
    'logging', 'threading', 'asyncio', 'subprocess', 'shutil', 'tempfile',
    'glob', 'fnmatch', 'pickle', 'csv', 'configparser', 'argparse', 'uuid',
    'traceback', 'inspect', 'warnings', 'contextlib', 'weakref', 'gc',
    'socket', 'http', 'urllib', 'email', 'html', 'xml', 'sqlite3',
    'unittest', 'signal', 'platform', 'multiprocessing', 'concurrent',
    'queue', 'heapq', 'bisect', 'array', 'decimal', 'fractions', 'statistics',
    'operator', 'pprint', 'textwrap', 'difflib', 'codecs', 'binascii',
    '__future__', 'builtins', 'types', 'numbers',
}

# Noms génériques qui sont PRESQUE TOUJOURS des fichiers locaux écrits par l'utilisateur,
# même si un package PyPI du même nom existe (ex: 'config', 'utils').
LIKELY_LOCAL_NAMES = {
    'config', 'configuration', 'settings', 'constants', 'const',
    'utils', 'util', 'helpers', 'helper', 'tools', 'tool',
    'models', 'model', 'database', 'db', 'schema',
    'handlers', 'handler', 'middlewares', 'middleware',
    'keyboards', 'keyboard', 'buttons', 'button',
    'states', 'state', 'filters', 'filter',
    'functions', 'func', 'decorators', 'decorator',
    'validators', 'validator', 'parsers', 'parser',
    'messages', 'texts', 'strings', 'locales', 'lang',
    'bot', 'main', 'app', 'core', 'base',
    'api', 'client', 'server', 'routes', 'views',
    'tasks', 'jobs', 'scheduler', 'cron',
    'auth', 'token', 'tokens', 'session',
    'errors', 'exceptions', 'logger',
    'data', 'storage', 'cache', 'store',
}


def extract_imports(code: str) -> list[str]:
    """Extrait tous les noms de modules importés depuis le code Python."""
    modules = set()
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    modules.add(node.module.split('.')[0])
                elif node.level > 0 and node.module:
                    # Import relatif (.models, ..utils) → fichier local
                    modules.add(node.module.split('.')[0])
    except SyntaxError:
        # Fallback regex
        for match in re.finditer(r'^\s*import\s+(\w+)', code, re.MULTILINE):
            modules.add(match.group(1))
        for match in re.finditer(r'^\s*from\s+(\w+)\s+import', code, re.MULTILINE):
            modules.add(match.group(1))
    return list(modules)


def is_installed_module(module_name: str) -> bool:
    """Vérifie si le module est déjà installé dans l'environnement Python."""
    if module_name in STDLIB_MODULES:
        return True
    spec = importlib.util.find_spec(module_name)
    return spec is not None


def is_pip_package(module_name: str) -> bool:
    """
    Vérifie si le module est disponible sur PyPI (installable via pip).
    Utilise 'pip index versions' qui requête PyPI sans installer.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "index", "versions", module_name],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def detect_local_dependencies(code: str) -> tuple[list[str], list[str]]:
    """
    Analyse le code et retourne deux listes :
    - local_files  : fichiers Python locaux à demander à l'utilisateur (ex: config.py)
    - pip_packages : packages à installer automatiquement via pip (ex: telethon)

    Logique :
    1. Module déjà installé → ignoré
    2. Nom dans LIKELY_LOCAL_NAMES → fichier local (même si existe sur PyPI)
    3. Sinon → vérification PyPI en parallèle :
         sur PyPI  → pip package
         absent    → fichier local
    """
    all_imports = extract_imports(code)
    unknown = [m for m in all_imports if m and not m.startswith('_') and not is_installed_module(m)]

    if not unknown:
        return [], []

    local_files = []
    pip_packages = []
    to_check_pypi = []

    for module in unknown:
        if module.lower() in LIKELY_LOCAL_NAMES:
            local_files.append(f"{module}.py")
        else:
            to_check_pypi.append(module)

    # Vérification PyPI en parallèle pour les modules non-génériques
    if to_check_pypi:
        with ThreadPoolExecutor(max_workers=min(8, len(to_check_pypi))) as executor:
            future_to_module = {executor.submit(is_pip_package, m): m for m in to_check_pypi}
            for future in as_completed(future_to_module):
                module = future_to_module[future]
                try:
                    on_pypi = future.result()
                except Exception:
                    on_pypi = False

                if on_pypi:
                    pip_packages.append(module)
                else:
                    local_files.append(f"{module}.py")

    return sorted(local_files), sorted(pip_packages)
