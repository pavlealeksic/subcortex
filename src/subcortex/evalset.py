"""Labeled examples for calibrating subcortex's questions and thresholds.

A decision model's probability is not an accuracy: thresholds only mean
something against labeled data, per backend and per question wording. These
examples back ``subcortex eval`` and the calibration notes in docs/.

PROMPTS: (prompt, simple) — simple = a lookup or a small, local edit.
OUTPUTS: (task, tool call, output, needed) — needed = the output's middle holds
details the task relies on. Failing commands are left out: subcortex never
trims output that looks like a failure, whatever the model says.
"""

from __future__ import annotations

from typing import List, Tuple

SIMPLE = [
    "what is 2+2?",
    "what does git rebase do?",
    "fix the typo 'recieve' in README.md",
    "rename the variable tmp to buffer in utils.py",
    "what port does the dev server use?",
    "show me the last commit message",
    "is python's dict ordered?",
    "add a newline at the end of main.go",
    "what's the difference between let and const in JS?",
    "bump the version in package.json to 1.4.2",
    "run the tests",
    "which node version does this repo need?",
    "delete the unused import of os in cli.py",
    "what does the -p flag do in mkdir?",
    "change the default port from 8080 to 3000 in config.yaml",
    "print the current git branch",
    "add .DS_Store to .gitignore",
    "how do I list docker containers?",
    'make the error message in login.ts say "Invalid password" instead of "Error"',
    "what license is this project under?",
]

COMPLEX = [
    "implement OAuth login with Google and GitHub, including token refresh",
    "refactor the parser into a visitor pattern across all modules",
    "debug the intermittent race condition in the job scheduler",
    "migrate the database schema to support multi-tenancy",
    "why is the docker build so slow? speed it up",
    "add end-to-end tests for the checkout flow and fix whatever fails",
    "port the CLI from argparse to click and keep all flags compatible",
    "find where we parse the CSV upload and add a size limit",
    "set up CI with GitHub Actions for lint, test and release",
    "our memory usage grows over time in production, find the leak",
    "add pagination to every list endpoint in the API",
    "upgrade the project from React 17 to 18 and fix the breakages",
    "design a caching layer for the product catalog with invalidation",
    "the build fails on Linux but not macOS, figure out why",
    "write a migration script that backfills user timezones from IP logs",
    "split the monolith's billing module into its own service",
    "make the app work offline with a service worker and sync queue",
    "add role-based access control to the admin panel",
    "investigate why p99 latency doubled after yesterday's deploy",
    "convert the codebase from JavaScript to TypeScript with strict mode",
]

PROMPTS: List[Tuple[str, bool]] = [(p, True) for p in SIMPLE] + [(p, False) for p in COMPLEX]


def _lines(template: str, n: int) -> str:
    return "\n".join(template.format(i=i, j=i * 7 % 97, k=i * 13 % 1000) for i in range(n))


_NPM = _lines("npm http fetch GET 200 https://registry.npmjs.org/pkg-{i} {j}ms (cache miss)", 60) \
    + "\nadded 1402 packages, and audited 1403 packages in 38s\nfound 0 vulnerabilities"
_CARGO = _lines("   Compiling crate_{i} v0.{j}.{i}", 70) + "\n    Finished dev [unoptimized + debuginfo] target(s) in 41.20s"
_PIP = _lines("Collecting package-{i}==1.{j}.0\n  Downloading package_{i}-1.{j}.0-py3-none-any.whl ({k} kB)", 40) \
    + "\nSuccessfully installed " + " ".join(f"package-{i}-1.{i * 7 % 97}.0" for i in range(40))
_DOCKER_PULL = _lines("{i:012x}: Pull complete", 50) + "\nDigest: sha256:" + "ab" * 32 + "\nStatus: Downloaded newer image for node:20"
_PYTEST_OK = _lines("tests/test_module_{i}.py::test_case_{j} PASSED                      [{k:3d}%]", 70) \
    + "\n============================== 70 passed in 12.31s =============================="
_WEBPACK = _lines("[webpack] <s> [webpack.Progress] {i}% building {j}/{k} entries {i}/{j} dependencies", 60) \
    + "\nwebpack 5.90.0 compiled successfully in 8412 ms"
_APT = _lines("Get:{i} http://archive.ubuntu.com/ubuntu jammy/main amd64 lib{j}-dev amd64 1.{i}-1 [{k} kB]", 50) \
    + "\nFetched 48.2 MB in 6s (8,120 kB/s)"
_LS_NODE = _lines("node_modules/pkg-{i}/package.json\nnode_modules/pkg-{i}/index.js\nnode_modules/pkg-{i}/README.md", 50)
_TERRAFORM = _lines("aws_security_group_rule.rule_{i}: Refreshing state... [id=sgrule-{k}]", 60)
_GIT_LOG = _lines("{k:07x} chore(deps): bump dependency-{i} from 1.{j}.0 to 1.{j}.1", 60)

_GREP_CSV = _lines("src/ui/table_{i}.tsx:{j}:  rows.map(row => <Row key={{row.id}} {{...row}} />)", 30) + "\n" + "\n".join([
    "src/api/upload.py:88:def parse_csv(file: UploadFile) -> list[dict]:",
    "src/api/upload.py:91:    reader = csv.DictReader(io.TextIOWrapper(file.file, encoding='utf-8'))",
    "src/api/upload.py:94:    return [row for row in reader]",
    "src/api/routes.py:41:    rows = parse_csv(upload)",
]) + "\n" + _lines("src/ui/list_{i}.tsx:{j}:  items.map(item => <Item key={{item.id}} />)", 30)
_DOCKER_BUILD = "\n".join([
    "#1 [internal] load build definition from Dockerfile", "#1 DONE 0.0s",
    "#4 [builder 1/7] FROM python:3.12-slim", "#4 DONE 0.4s",
    "#5 [builder 2/7] COPY . /app", "#5 DONE 41.3s",
    "#6 [builder 3/7] RUN pip install -r requirements.txt", "#6 212.7s (no cache: COPY . /app changed)",
    "#7 [builder 4/7] RUN npm ci", "#7 DONE 98.0s",
    "#8 [builder 5/7] RUN npm run build", "#8 DONE 64.2s",
]) + "\n" + _lines("#9 sending tarball layer {i} {k}kB", 40)
_SESSION_PY = "\n".join([
    "import time", "from .tokens import decode, TokenExpired", "",
    "class Session:",
    "    def __init__(self, user_id, expires_at):",
    "        self.user_id = user_id", "        self.expires_at = expires_at", "",
    "    def expired(self, now=None):",
    "        return (now or time.time()) > self.expires_at", "",
    "def login(token):",
    "    claims = decode(token)",
    "    session = Session(claims['sub'], claims['exp'])",
    "    if session.expired():",
    "        raise TokenExpired(claims['sub'])",
    "    return session",
] * 4)
_PYTEST_LOGIN = _lines("tests/test_module_{i}.py::test_case_{j} PASSED", 30) + "\n" + "\n".join([
    "tests/auth/test_session.py::test_login_rejects_expired_token PASSED",
    "tests/auth/test_session.py::test_login_accepts_clock_skew XFAIL (skew not handled)",
    "tests/auth/test_session.py::test_login_sets_cookie PASSED",
]) + "\n" + _lines("tests/test_module_{i}.py::test_other_{j} PASSED", 30)
_DU = "\n".join(f"{size}\t{name}" for size, name in [
    ("48G", "./data/raw_exports"), ("12G", "./.git"), ("3.1G", "./node_modules"), ("820M", "./build"),
    ("410M", "./logs"), ("96M", "./coverage")] + [(f"{i}K", f"./src/module_{i}") for i in range(80)])
_GIT_DIFF = "\n".join([
    "diff --git a/src/api/upload.py b/src/api/upload.py",
    "@@ -85,10 +85,14 @@ def parse_csv(file: UploadFile) -> list[dict]:",
    "+MAX_UPLOAD_BYTES = 10 * 1024 * 1024",
    "+    if file.size and file.size > MAX_UPLOAD_BYTES:",
    "+        raise HTTPException(413, 'CSV upload too large')",
    "     reader = csv.DictReader(io.TextIOWrapper(file.file, encoding='utf-8'))",
] * 8)
_EXPLAIN = "\n".join([
    "Gather  (cost=1000.00..184732.41 rows=1 width=64) (actual time=1893.112..1905.442 rows=12 loops=1)",
    "  Workers Planned: 2",
    "  ->  Parallel Seq Scan on orders  (cost=0.00..183732.31 rows=1 width=64) (actual time=1874.2..1880.9 rows=4 loops=3)",
    "        Filter: ((customer_email)::text = 'a@b.com'::text)",
    "        Rows Removed by Filter: 4166663",
    "Planning Time: 0.211 ms", "Execution Time: 1905.520 ms",
] * 6)
_KUBECTL = "NAME                         READY   STATUS             RESTARTS   AGE\n" + "\n".join(
    [f"api-7d9f8c6b5-{i:05d}         1/1     Running            0          2d" for i in range(20)]
    + ["worker-5c4b8d7f9-x2k9p        0/1     ImagePullBackOff   0          14m",
       "worker-5c4b8d7f9-q8w3e        0/1     ImagePullBackOff   0          14m"]
    + [f"cron-{i:05d}                  0/1     Completed          0          3h" for i in range(20)])
_NPM_LS = "app@2.3.0 /srv/app\n" + _lines("├─┬ @scope/widget-{i}@3.{j}.0\n│ └── react@17.0.2 deduped", 25) \
    + "\n├── react@17.0.2\n├── react-dom@17.0.2\n└─┬ react-router-dom@5.3.4\n  └── react@17.0.2 deduped"
_CURL = "\n".join([
    "*   Trying 10.0.4.12:443...", "* Connected to api.example.com (10.0.4.12) port 443",
    "> OPTIONS /v2/orders HTTP/2", "> Origin: https://app.example.com",
    "> Access-Control-Request-Method: POST",
    "< HTTP/2 204", "< access-control-allow-origin: https://admin.example.com",
    "< access-control-allow-methods: GET, OPTIONS", "< vary: Origin",
] * 5)
_PROFILE = "   ncalls  tottime  percall  cumtime  percall filename:lineno(function)\n" + "\n".join([
    "   120000   14.212    0.000   31.877    0.000 cache.py:48(_store)",
    "   120000    9.101    0.000    9.101    0.000 {method 'append' of 'list' objects}",
    "        1    0.002    0.002   42.120   42.120 worker.py:12(run)",
]) + "\n" + _lines("      {k}    0.00{i:02d}    0.000    0.00{i:02d}    0.000 util_{i}.py:{j}(helper_{i})", 60)
_ENV = "\n".join([f"VAR_{i}=value_{i}" for i in range(40)]
                 + ["DATABASE_URL=postgres://app@db.internal:5433/app", "DATABASE_POOL=5", "PGSSLMODE=require"]
                 + [f"OTHER_{i}=x" for i in range(40)])

OUTPUTS: List[Tuple[str, str, str, bool]] = [
    ("fix the failing login test in auth/session.py", "Bash: npm install", _NPM, False),
    ("find where we parse the CSV upload and add a size limit", "Bash: cargo build", _CARGO, False),
    ("rename the variable tmp to buffer in utils.py", "Bash: pip install -r requirements.txt", _PIP, False),
    ("fix the typo in README.md", "Bash: docker pull node:20", _DOCKER_PULL, False),
    ("add pagination to the orders endpoint", "Bash: pytest", _PYTEST_OK, False),
    ('make the error message in login.ts say "Invalid password"', "Bash: npm run build", _WEBPACK, False),
    ("set up CI with GitHub Actions", "Bash: apt-get install -y build-essential", _APT, False),
    ("fix the failing login test in auth/session.py", "Bash: find node_modules -name '*.js*'", _LS_NODE, False),
    ("rename the output variable in main.tf", "Bash: terraform plan", _TERRAFORM, False),
    ("add a size limit to the CSV upload", "Bash: git log --oneline -60", _GIT_LOG, False),
    ("upgrade the project from React 17 to 18", "Bash: cargo build", _CARGO, False),
    ("why is the docker build so slow? speed it up", "Bash: npm install", _NPM, False),
    ("find where we parse the CSV upload and add a size limit", "Bash: rg -n 'csv|rows' src", _GREP_CSV, True),
    ("why is the docker build so slow? speed it up", "Bash: docker build --progress=plain .", _DOCKER_BUILD, True),
    ("fix the failing login test in auth/session.py", "Bash: cat auth/session.py", _SESSION_PY, True),
    ("fix the failing login test in auth/session.py", "Bash: pytest -v", _PYTEST_LOGIN, True),
    ("find what is using all the disk space", "Bash: du -sh ./*", _DU, True),
    ("review my changes before I commit", "Bash: git diff", _GIT_DIFF, True),
    ("speed up the slow orders-by-email query", "Bash: psql -c 'EXPLAIN ANALYZE ...'", _EXPLAIN, True),
    ("why is the worker deployment not ready?", "Bash: kubectl get pods", _KUBECTL, True),
    ("upgrade the project from React 17 to 18", "Bash: npm ls react", _NPM_LS, True),
    ("why can't the app connect to the database?", "Bash: env", _ENV, True),
    ("debug the CORS error when posting orders", "Bash: curl -v -X OPTIONS https://api.example.com/v2/orders", _CURL, True),
    ("our worker gets slower over time, find the hotspot", "Bash: python -m cProfile worker.py", _PROFILE, True),
]
