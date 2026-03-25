#!/usr/bin/env python3
"""Infrastructure management CLI. See USAGE constant for full usage."""

import json
import os
import threading
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
import psycopg2
import typer

# Fix Typer 0.9.0 / Click 8.1+ incompatibility:
# Click 8.1+ calls make_metavar(ctx) and get_metavar(param, ctx); Typer 0.9 doesn't expect these args.
try:
    from typer.main import TyperArgument as _TyperArgument
    def _patched_make_metavar(self, *args) -> str:
        ctx = args[0] if args else None
        if self.metavar is not None:
            return self.metavar
        var = (self.name or "").upper()
        if not self.required:
            var = "[{}]".format(var)
        try:
            type_var = self.type.get_metavar(self, ctx)
        except TypeError:
            type_var = self.type.get_metavar(self)
        if type_var:
            var += f":{type_var}"
        if self.nargs != 1:
            var += "..."
        return var
    _TyperArgument.make_metavar = _patched_make_metavar
except Exception:
    pass

# LAYERS defines the ordered layer names for db flows (must match project layout)
LAYERS = ("bootstrap", "core", "derived", "functions", "seed", "rls")

USAGE = """\
Usage:
    # List all available targets
    python run.py list

    # Apply a single DB file (flat or schema-qualified)
    python run.py db apply <layer> <target>
    python run.py db apply <layer> <schema>.<target>

    # CRITICAL targets require --critical to execute
    python run.py db apply <layer> <target> --critical

    python run.py db dangerous <target> --confirm

    # Apply an integration script — namespace.target
    python run.py integrations apply <namespace>.<target>
    python run.py integrations apply <namespace>.<target> --critical  # if CRITICAL

    # Run data pipeline jobs — namespace.job
    python run.py jobs apply <namespace>.<job>
    python run.py jobs apply <namespace>.<job> --critical  # if CRITICAL

    # Validate all run.py commands inside flows/
    python run.py flows check

    # Find targets not referenced in any flow (or a specific flow)
    python run.py flows unused
    python run.py flows unused <flow>

    # Run a read-only inspect target — namespace.target
    python run.py inspect <namespace>.<target>"""

try:
    from miniflow_config import CRITICAL, ENV_CONSTRAINTS
except ImportError:
    raise SystemExit("Error: miniflow_config.py not found. Create it at the project root.")


#* Load .env if present — overrides shell environment variables
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        # strip optional 'export ' prefix
        if _line.startswith("export "):
            _line = _line[7:]
        _k, _, _v = _line.partition("=")
        # strip surrounding quotes from value
        _v = _v.strip().strip('"').strip("'")
        os.environ[_k.strip()] = _v

def get_verbose_level() -> int:
    if not sys.stdout.isatty():
        return 1
    env_value = os.getenv("VERBOSE")
    if env_value is not None:
        try:
            level = int(env_value)
            return level if level in (1, 2) else 1
        except ValueError:
            return 1
    return 2


VERBOSE = get_verbose_level()

app = typer.Typer(name="infra", help="Infrastructure management CLI", invoke_without_command=True)
db_app = typer.Typer(help="Database operations")
integrations_app = typer.Typer(help="External system configuration via API")
jobs_app = typer.Typer(help="Data pipeline jobs")
flows_app = typer.Typer(help="Flow validation")
@app.callback()
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo("Error: no command provided.", err=True)
        typer.echo("", err=True)
        typer.echo(USAGE, err=True)
        raise typer.Exit(1)


app.add_typer(db_app, name="db")
app.add_typer(integrations_app, name="integrations")
app.add_typer(jobs_app, name="jobs")
app.add_typer(flows_app, name="flows")

DB_DIR = Path(__file__).parent / "db"
ROOT = Path(__file__).parent

LOG_DIR = ROOT / ".logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "infra.log"


def log_execution(command: str, status: str, started_at: datetime, finished_at: datetime) -> None:
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    entry = {
        "command": command,
        "status": status,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": duration_ms,
    }
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def check_env_constraint(target: str) -> None:
    allowed = ENV_CONSTRAINTS.get(target)
    if allowed is None:
        return
    current = os.getenv("MINIFLOW_ENV")
    if current is None:
        typer.echo(
            f"Error: target '{target}' requires environment {allowed}, but MINIFLOW_ENV is not set.",
            err=True,
        )
        raise typer.Exit(1)
    if current not in allowed:
        typer.echo(
            f"Error: target '{target}' is not allowed in environment '{current}'.\n"
            f"Allowed environments: {allowed}",
            err=True,
        )
        raise typer.Exit(1)


def _run_start(started_at: datetime) -> None:
    if VERBOSE < 1:
        return
    cmd = " ".join(sys.argv[1:])
    ts = started_at.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n▶▶▶ RUN START ─────────────────────────────")
    print(f"Command: {cmd}")
    print(f"Time:    {ts}")


def _run_end(status: str, started_at: datetime) -> None:
    if VERBOSE < 1:
        return
    ms = int((datetime.now().astimezone() - started_at).total_seconds() * 1000)
    icon, label = ("✔", "SUCCESS") if status == "success" else ("✖", "FAILURE")
    print(f"\n{icon} {label} ({ms} ms)")
    print("─" * 40)


def _step(label: str) -> None:
    if VERBOSE >= 1:
        print(f"[STEP] Applying: {label}")


def _ok(label: str, started_at: datetime) -> None:
    if VERBOSE >= 1:
        ms = int((datetime.now().astimezone() - started_at).total_seconds() * 1000)
        print(f"[OK] {label} ({ms} ms)")


def _error_block(label: str, exc: BaseException) -> None:
    print(f"\n{'='*10} ERROR {'='*10}")
    print(f"Step:   {label}")
    print(f"Reason: {exc}")
    print('='*27)


def _stream_process(args: list[str], env) -> int:
    """Run a subprocess with real-time streaming. Returns exit code.

    VERBOSE >= 2: streams both stdout and stderr.
    VERBOSE >= 1: streams stderr only (stdout suppressed).
    """
    proc = subprocess.Popen(
        args, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    has_output = [False]
    first_line_lock = threading.Lock()

    def _drain(src, dest, show: bool) -> None:
        for line in src:
            if show:
                with first_line_lock:
                    if not has_output[0]:
                        print("", file=dest, flush=True)  # blank line before first output
                        has_output[0] = True
                print(line, end="", file=dest, flush=True)

    t_out = threading.Thread(target=_drain, args=(proc.stdout, sys.stdout, VERBOSE >= 2), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, sys.stderr, VERBOSE >= 1), daemon=True)
    t_out.start()
    t_err.start()
    t_out.join()
    t_err.join()
    if has_output[0]:
        print(flush=True)  # blank line after last output
    return proc.wait()


def check_critical(target: str, critical_flag: bool) -> None:
    if target in CRITICAL and not critical_flag:
        typer.echo(
            f"Error: '{target}' is marked as CRITICAL.\n"
            f"Re-run with --critical to confirm execution.",
            err=True,
        )
        raise typer.Exit(1)



@contextmanager
def _connection():
    missing = [v for v in ("PGHOST", "PGUSER", "PGPASSWORD") if not os.environ.get(v)]
    if missing:
        typer.echo(f"Error: missing environment variables: {missing}. Check your .env file.", err=True)
        raise typer.Exit(1)
    conn = psycopg2.connect(
        host=os.environ["PGHOST"],
        port=int(os.environ.get("PGPORT", "5432")),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        dbname=os.environ.get("PGDATABASE", "postgres"),
        connect_timeout=10,
    )
    try:
        yield conn
    finally:
        conn.close()


def _substitute_env_vars(sql: str) -> str:
    pattern = re.compile(r"__([A-Z0-9_]+)__")

    def replace(match):
        var_name = match.group(1)
        value = os.getenv(var_name)
        if value is None:
            raise ValueError(f"Missing environment variable: {var_name}")
        return value

    return pattern.sub(replace, sql)


def _run_sql_file(conn, path: Path) -> None:
    with conn.cursor() as cur:
        sql = _substitute_env_vars(path.read_text())
        cur.execute(sql)
    conn.commit()
    if conn.notices:
        if VERBOSE >= 2:
            for notice in conn.notices:
                print(f"  {notice.strip()}")
        else:
            print(f"[NOTICE] {len(conn.notices)} message(s)")
        conn.notices.clear()


#* --- DB commands ---

@db_app.command(name="apply")
def apply(
    layer: str = typer.Argument(..., help="Layer: bootstrap | core | derived | functions | seed | rls"),
    name: Optional[str] = typer.Argument(None, help="File name without .sql (e.g. vias or public.vias)"),
    critical: bool = typer.Option(False, "--critical", help="Confirm execution of CRITICAL targets."),
) -> None:
    """Apply a single SQL file from the given layer. CRITICAL targets require --critical."""
    if layer == "dangerous":
        typer.echo(
            "Error: 'dangerous' targets must be executed using 'db dangerous <target> --confirm'.",
            err=True,
        )
        raise typer.Exit(1)
    if layer not in LAYERS:
        typer.echo(f"Error: unknown layer '{layer}'. Choose from: {list(LAYERS)}", err=True)
        raise typer.Exit(1)
    if not name:
        available = sorted(f.stem for f in (DB_DIR / layer).glob("*.sql"))
        typer.echo(f"Error: missing required argument <target>. Available in {layer}/: {available}", err=True)
        raise typer.Exit(1)

    layer_dir = DB_DIR / layer

    if "." in name:
        schema, file = name.split(".", 1)
        # Support both flat (public.foo.sql) and subdirectory (public/foo.sql)
        flat_file = layer_dir / f"{name}.sql"
        nested_file = layer_dir / schema / f"{file}.sql"
        if flat_file.exists() and nested_file.exists():
            typer.echo(f"Error: ambiguous target '{name}': found as both {layer}/{name}.sql and {layer}/{schema}/{file}.sql", err=True)
            raise typer.Exit(1)
        elif flat_file.exists():
            sql_file = flat_file
        elif nested_file.exists():
            sql_file = nested_file
        else:
            available = sorted(
                f.stem for f in layer_dir.glob("*.sql")
                if f.stem.startswith(f"{schema}.")
            )
            typer.echo(f"Error: '{name}' not found in {layer}/. Available: {available}", err=True)
            raise typer.Exit(1)
    else:
        sql_file = layer_dir / f"{name}.sql"
        if not sql_file.exists():
            # Detect if the target exists under a schema prefix (ambiguous without qualification)
            schema_matches = sorted(layer_dir.glob(f"*.{name}.sql"))
            if schema_matches:
                schemas = [f.stem.rsplit(f".{name}", 1)[0] for f in schema_matches]
                typer.echo(f"Error: ambiguous target '{name}': found in multiple schemas: {schemas}. Use <schema>.{name}", err=True)
            else:
                available = sorted(f.stem for f in layer_dir.glob("*.sql"))
                typer.echo(f"Error: '{name}' not found in {layer}/. Available: {available}", err=True)
            raise typer.Exit(1)

    target = f"{layer}.{name}"
    check_critical(target, critical)
    check_env_constraint(target)
    label = f"{layer}/{sql_file.name}"
    started_at = datetime.now().astimezone()
    _run_start(started_at)
    _step(label)
    status = "success"
    try:
        with _connection() as conn:
            _run_sql_file(conn, sql_file)
        _ok(label, started_at)
    except Exception as exc:
        status = "error"
        _error_block(label, exc)
        raise
    finally:
        _run_end(status, started_at)
        log_execution(f"db apply {layer} {name}", status, started_at, datetime.now().astimezone())


@db_app.command()
def dangerous(
    target: Optional[str] = typer.Argument(None, help="Dangerous operation target (e.g. drop_all)"),
    confirm: bool = typer.Option(False, "--confirm", is_flag=True, help="--confirm is required to execute destructive operations."),
) -> None:
    """Execute a dangerous destructive operation. Requires --confirm."""
    _ = confirm  # kept for Typer to register --confirm; actual check uses sys.argv below
    if not target:
        available = sorted(f.stem for f in (DB_DIR / "dangerous").glob("*.sql"))
        typer.echo(f"Error: missing required argument <target>. Available: {available}", err=True)
        raise typer.Exit(1)
    # Only accept --confirm if it appears after "dangerous" in this invocation.
    # Avoids contamination from global flags or unrelated argv entries.
    try:
        danger_idx = sys.argv.index("dangerous")
        confirmed = "--confirm" in sys.argv[danger_idx:]
    except ValueError:
        confirmed = False
    if not confirmed:
        typer.echo(
            "Error: destructive operation requires explicit --confirm flag.",
            err=True,
        )
        raise typer.Exit(1)

    sql_file = DB_DIR / "dangerous" / f"{target}.sql"
    if not sql_file.exists():
        available = sorted(f.stem for f in (DB_DIR / "dangerous").glob("*.sql"))
        typer.echo(f"Error: '{target}' not found. Available: {available}", err=True)
        raise typer.Exit(1)

    label = f"dangerous/{sql_file.name}"
    started_at = datetime.now().astimezone()
    _run_start(started_at)
    _step(label)
    status = "success"
    try:
        with _connection() as conn:
            _run_sql_file(conn, sql_file)
        _ok(label, started_at)
    except Exception as exc:
        status = "error"
        _error_block(label, exc)
        raise
    finally:
        _run_end(status, started_at)
        log_execution(f"db dangerous {target.replace('.', ' ')}", status, started_at, datetime.now().astimezone())


#* --- Integrations commands ---

@integrations_app.command(name="apply", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def integrations_apply(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Target in 'namespace.target' format (e.g. geoserver.setup)"),
    critical: bool = typer.Option(False, "--critical", help="Confirm execution of CRITICAL targets."),
) -> None:
    """Apply an integration script. Format: namespace.target (e.g. geoserver.setup)."""
    if ctx.args:
        bad = f"{ref} {' '.join(ctx.args)}"
        typer.echo(f"Error: expected format 'namespace.target', got '{bad}'", err=True)
        typer.echo("Usage: run.py integrations apply <namespace.target>", err=True)
        raise typer.Exit(1)
    if "." not in ref or ref.startswith(".") or ref.endswith("."):
        typer.echo(f"Error: expected format 'namespace.target', got '{ref}'", err=True)
        typer.echo("Usage: run.py integrations apply <namespace.target>", err=True)
        raise typer.Exit(1)
    layer, name = ref.split(".", 1)

    layer_path = ROOT / "integrations" / layer
    if not layer_path.exists():
        available = sorted(p.name for p in (ROOT / "integrations").iterdir() if p.is_dir())
        typer.echo(f"Error: unknown integration namespace '{layer}'. Available: {available}", err=True)
        raise typer.Exit(1)

    script = layer_path / f"{name}.sh"
    if not script.exists():
        available = sorted(f.stem for f in layer_path.glob("*.sh"))
        typer.echo(f"Error: target '{name}' not found in {layer}/. Available: {available}", err=True)
        raise typer.Exit(1)

    target = f"integrations.{layer}.{name}"
    check_critical(target, critical)
    check_env_constraint(target)
    label = f"integrations/{layer}/{script.name}"
    started_at = datetime.now().astimezone()
    _run_start(started_at)
    _step(label)
    status = "success"
    try:
        returncode = _stream_process(["bash", str(script)], os.environ)
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, ["bash", str(script)])
        _ok(label, started_at)
    except Exception as exc:
        status = "error"
        _error_block(label, exc)
        raise
    finally:
        _run_end(status, started_at)
        log_execution(f"integrations apply {layer}.{name}", status, started_at, datetime.now().astimezone())


#* --- Jobs commands ---

@jobs_app.command(name="apply")
def jobs_apply(
    ref: str = typer.Argument(..., help="Job in 'namespace.job' format (e.g. geodata.vias_apelidos)"),
    critical: bool = typer.Option(False, "--critical", help="Confirm execution of CRITICAL targets."),
) -> None:
    """Run a data pipeline job. Format: namespace.job (e.g. geodata.vias_apelidos). CRITICAL targets require --critical."""
    if "." not in ref or ref.startswith(".") or ref.endswith("."):
        typer.echo(f"Error: expected format 'namespace.job', got '{ref}'", err=True)
        typer.echo("Usage: run.py jobs apply <namespace>.<job>", err=True)
        raise typer.Exit(1)
    namespace, job = ref.split(".", 1)

    namespace_path = ROOT / "jobs" / namespace
    if not namespace_path.exists():
        available = sorted(p.name for p in (ROOT / "jobs").iterdir() if p.is_dir())
        typer.echo(f"Error: unknown job namespace '{namespace}'. Available: {available}", err=True)
        raise typer.Exit(1)

    script = namespace_path / job / "apply.sh"
    if not script.exists():
        available = sorted(p.name for p in namespace_path.iterdir() if p.is_dir())
        typer.echo(f"Error: job '{job}' not found in {namespace}/. Available: {available}", err=True)
        raise typer.Exit(1)

    target = f"jobs.{namespace}.{job}"
    check_critical(target, critical)
    check_env_constraint(target)
    label = f"jobs/{namespace}/{job}/apply.sh"
    started_at = datetime.now().astimezone()
    _run_start(started_at)
    _step(label)
    status = "success"
    try:
        returncode = _stream_process(["bash", str(script)], os.environ)
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, ["bash", str(script)])
        _ok(label, started_at)
    except Exception as exc:
        status = "error"
        _error_block(label, exc)
        raise
    finally:
        _run_end(status, started_at)
        log_execution(f"jobs apply {namespace}.{job}", status, started_at, datetime.now().astimezone())


@app.command(name="list")
def list_targets() -> None:
    """List all available targets grouped by layer."""
    typer.echo("[db]")
    for layer in LAYERS:
        layer_path = DB_DIR / layer
        if not layer_path.exists():
            continue
        typer.echo(f"\n{layer}")
        for file in sorted(layer_path.glob("*.sql")):
            typer.echo(f"  {file.stem}")
        for schema_dir in sorted(p for p in layer_path.iterdir() if p.is_dir()):
            typer.echo(f"  {schema_dir.name}")
            for file in sorted(schema_dir.glob("*.sql")):
                typer.echo(f"    {file.stem}")

    integrations_path = ROOT / "integrations"
    if integrations_path.exists():
        typer.echo("\n[integrations]")
        for layer_dir in sorted(p for p in integrations_path.iterdir() if p.is_dir()):
            typer.echo(f"\n{layer_dir.name}")
            for file in sorted(layer_dir.glob("*.sh")):
                typer.echo(f"  {file.stem}")

    jobs_path = ROOT / "jobs"
    if jobs_path.exists():
        typer.echo("\n[jobs]")
        for ns_dir in sorted(p for p in jobs_path.iterdir() if p.is_dir()):
            typer.echo(f"\n{ns_dir.name}")
            for job_dir in sorted(p for p in ns_dir.iterdir() if p.is_dir()):
                typer.echo(f"  {job_dir.name}")

    inspect_path = ROOT / "inspect"
    if inspect_path.exists():
        typer.echo("\n[inspect]")
        for group_dir in sorted(p for p in inspect_path.iterdir() if p.is_dir()):
            typer.echo(f"\n{group_dir.name}")
            for file in sorted([*group_dir.glob("*.sql"), *group_dir.glob("*.sh")]):
                typer.echo(f"  {file.stem}")


#* --- Flows commands ---

def _validate_db_target(target: str) -> str | None:
    """Return error message if target is invalid, else None."""
    parts = target.split(".")
    if len(parts) == 2:
        layer, name = parts
        if layer not in LAYERS:
            return f"unknown layer '{layer}'"
        if not (DB_DIR / layer / f"{name}.sql").exists():
            return f"file not found: db/{layer}/{name}.sql"
    elif len(parts) == 3:
        layer, schema, name = parts
        if layer not in LAYERS:
            return f"unknown layer '{layer}'"
        flat = DB_DIR / layer / f"{schema}.{name}.sql"
        nested = DB_DIR / layer / schema / f"{name}.sql"
        if not flat.exists() and not nested.exists():
            return f"file not found: db/{layer}/{schema}.{name}.sql or db/{layer}/{schema}/{name}.sql"
    else:
        return f"invalid db target format: '{target}'"
    return None


@flows_app.command(name="check")
def flows_check() -> None:
    """Validate all run.py commands inside the flows/ directory."""
    flows_path = ROOT / "flows"
    if not flows_path.exists():
        typer.echo("No flows/ directory found.", err=True)
        raise typer.Exit(1)

    errors: list[str] = []
    warnings: list[str] = []

    # Per-file state for layer order tracking
    last_layer_index: dict[str, int] = {}

    for flow_file in sorted(flows_path.rglob("*")):
        if not flow_file.is_file():
            continue

        file_key = str(flow_file)
        last_layer_index[file_key] = -1

        for lineno, line in enumerate(flow_file.read_text().splitlines(), start=1):
            stripped = line.strip()

            if not stripped or stripped.startswith("#"):
                continue
            if not any(p.endswith("run.py") for p in stripped.split()):
                continue
            if "<" in stripped and ">" in stripped:
                errors.append(f"{flow_file.relative_to(ROOT)}:{lineno} → unresolved placeholder in active command: {stripped}")
                continue

            parts = stripped.split()
            ref = f"{flow_file.relative_to(ROOT)}:{lineno}"

            # Find index of the token ending with 'run.py'
            rpy_idx = next((i for i, p in enumerate(parts) if p.endswith("run.py")), None)
            if rpy_idx is None:
                errors.append(f"{ref} → cannot parse command: {stripped}")
                continue

            cmd_parts = parts[rpy_idx + 1:]  # tokens after 'run.py'

            if len(cmd_parts) < 2:
                errors.append(f"{ref} → invalid command format: {stripped}")
                continue

            domain = cmd_parts[0]
            action = cmd_parts[1]

            if domain == "inspect":
                # python run.py inspect <namespace>.<target>
                raw = action  # second token is the ref, not a subcommand
                if "." not in raw or raw.startswith(".") or raw.endswith("."):
                    errors.append(f"{ref} → invalid inspect target format (expected 'namespace.target'): {raw}")
                    continue
                namespace, name = raw.split(".", 1)
                sql_file = ROOT / "inspect" / namespace / f"{name}.sql"
                sh_file = ROOT / "inspect" / namespace / f"{name}.sh"
                if sql_file.exists() and sh_file.exists():
                    errors.append(f"{ref} → ambiguous inspect target (both .sql and .sh exist): inspect/{namespace}/{name}")
                elif not sql_file.exists() and not sh_file.exists():
                    errors.append(f"{ref} → inspect target not found: inspect/{namespace}/{name}.sql or .sh")
                continue

            # Only validate 'apply' subcommands — skip seed, dangerous, list, etc.
            if action != "apply":
                continue

            has_critical_flag = "--critical" in cmd_parts

            if domain == "db":
                # python run.py db apply <layer> <name> [flags]
                if len(cmd_parts) < 4:
                    errors.append(f"{ref} → invalid db command: expected 'db apply <layer> <name>': {stripped}")
                    continue
                layer = cmd_parts[2]
                target = f"{layer}.{cmd_parts[3]}"
                err = _validate_db_target(target)
                if err:
                    errors.append(f"{ref} → {err}")
                    continue

                # Layer order warning
                if layer in LAYERS:
                    current_idx = LAYERS.index(layer)
                    if current_idx < last_layer_index[file_key]:
                        prev_layer = LAYERS[last_layer_index[file_key]]
                        warnings.append(f"layer order regression at {ref} → '{layer}' after '{prev_layer}'")
                    last_layer_index[file_key] = current_idx

                # CRITICAL warnings
                if target in CRITICAL:
                    warnings.append(f"CRITICAL target used at {ref} → {target}")
                    if not has_critical_flag:
                        warnings.append(f"missing --critical flag for CRITICAL target at {ref} → {target}")
                elif has_critical_flag:
                    warnings.append(f"unnecessary --critical flag at {ref} → {target}")

                # ENV_CONSTRAINTS warning
                if target in ENV_CONSTRAINTS:
                    warnings.append(f"target '{target}' is restricted to environments {ENV_CONSTRAINTS[target]} at {ref}")

            elif domain == "integrations":
                # python run.py integrations apply <namespace.target> [flags]
                if len(cmd_parts) < 3:
                    errors.append(f"{ref} → invalid integrations command: expected 'integrations apply <namespace.target>': {stripped}")
                    continue
                raw = cmd_parts[2]
                if "." not in raw or raw.startswith(".") or raw.endswith("."):
                    errors.append(f"{ref} → invalid integrations target format (expected 'namespace.target'): {raw}")
                    continue
                layer, name = raw.split(".", 1)
                target = f"integrations.{layer}.{name}"
                layer_path = ROOT / "integrations" / layer
                if not layer_path.exists():
                    errors.append(f"{ref} → integration namespace not found: {layer}")
                elif not (layer_path / f"{name}.sh").exists():
                    errors.append(f"{ref} → script not found: integrations/{layer}/{name}.sh")
                else:
                    if target in CRITICAL:
                        warnings.append(f"CRITICAL target used at {ref} → {target}")
                        if not has_critical_flag:
                            warnings.append(f"missing --critical flag for CRITICAL target at {ref} → {target}")
                    elif has_critical_flag:
                        warnings.append(f"unnecessary --critical flag at {ref} → {target}")
                    if target in ENV_CONSTRAINTS:
                        warnings.append(f"target '{target}' is restricted to environments {ENV_CONSTRAINTS[target]} at {ref}")

            elif domain == "jobs":
                # python run.py jobs apply <namespace.job> [flags]
                if len(cmd_parts) < 3:
                    errors.append(f"{ref} → invalid jobs command: expected 'jobs apply <namespace.job>': {stripped}")
                    continue
                raw = cmd_parts[2]
                if "." not in raw or raw.startswith(".") or raw.endswith("."):
                    errors.append(f"{ref} → invalid jobs target format (expected 'namespace.job'): {raw}")
                    continue
                namespace, job = raw.split(".", 1)
                target = f"jobs.{namespace}.{job}"
                namespace_path = ROOT / "jobs" / namespace
                if not namespace_path.exists():
                    errors.append(f"{ref} → job namespace not found: jobs/{namespace}/")
                elif not (namespace_path / job).exists():
                    errors.append(f"{ref} → job not found: jobs/{namespace}/{job}/")
                else:
                    if target in CRITICAL:
                        warnings.append(f"CRITICAL target used at {ref} → {target}")
                        if not has_critical_flag:
                            warnings.append(f"missing --critical flag for CRITICAL target at {ref} → {target}")
                    elif has_critical_flag:
                        warnings.append(f"unnecessary --critical flag at {ref} → {target}")
                    if target in ENV_CONSTRAINTS:
                        warnings.append(f"target '{target}' is restricted to environments {ENV_CONSTRAINTS[target]} at {ref}")

            else:
                errors.append(f"{ref} → invalid domain: '{domain}'")

    if errors:
        typer.echo(f"Found {len(errors)} error(s) in flows:", err=True)
        for e in errors:
            typer.echo(f"  - {e}", err=True)

    if warnings:
        if errors:
            typer.echo("")
        typer.echo(f"Found {len(warnings)} warning(s):")
        for w in warnings:
            typer.echo(f"  - {w}")

    if errors:
        raise typer.Exit(1)

    if not warnings:
        typer.echo("All flows are valid.")


@flows_app.command(name="unused")
def flows_unused(
    flow: Optional[str] = typer.Argument(None, help="Optional flow file to scope the analysis (e.g. orchestrator.sh)"),
) -> None:
    """Report targets that exist in the repo but are not referenced in any flow."""
    flows_path = ROOT / "flows"

    # --- Build available targets ---
    available: dict[str, set[str]] = {"db": set(), "jobs": set(), "integrations": set()}

    for layer in LAYERS:
        layer_dir = DB_DIR / layer
        if not layer_dir.exists():
            continue
        for f in layer_dir.glob("*.sql"):
            available["db"].add(f"{layer}.{f.stem}")
        for schema_dir in (p for p in layer_dir.iterdir() if p.is_dir()):
            for f in schema_dir.glob("*.sql"):
                available["db"].add(f"{layer}.{schema_dir.name}.{f.stem}")

    jobs_path = ROOT / "jobs"
    if jobs_path.exists():
        for ns_dir in (p for p in jobs_path.iterdir() if p.is_dir()):
            for job_dir in (p for p in ns_dir.iterdir() if p.is_dir()):
                if (job_dir / "apply.sh").exists():
                    available["jobs"].add(f"jobs.{ns_dir.name}.{job_dir.name}")

    integrations_path = ROOT / "integrations"
    if integrations_path.exists():
        for ns_dir in (p for p in integrations_path.iterdir() if p.is_dir()):
            for f in ns_dir.glob("*.sh"):
                available["integrations"].add(f"integrations.{ns_dir.name}.{f.stem}")

    # --- Resolve which flow files to parse ---
    if flow is not None:
        # Strip leading "flows/" so both "orchestrator.sh" and "flows/orchestrator.sh" work
        flow_clean = flow.removeprefix("flows/")
        flow_file_path = flows_path / flow_clean
        if not flow_file_path.exists():
            typer.echo(f"Error: flow not found: {flow_file_path}", err=True)
            raise typer.Exit(1)
        flow_files: list[Path] = [flow_file_path]
        typer.echo(f"Unused targets relative to flow: {flow_clean}")
    else:
        flow_files = sorted(f for f in flows_path.rglob("*") if f.is_file()) if flows_path.exists() else []

    # --- Parse used targets from flows ---
    used: set[str] = set()

    for flow_file in flow_files:
        for line in flow_file.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not any(p.endswith("run.py") for p in stripped.split()):
                continue
            if "<" in stripped and ">" in stripped:
                continue  # unresolved placeholder

            parts = stripped.split()
            rpy_idx = next((i for i, p in enumerate(parts) if p.endswith("run.py")), None)
            if rpy_idx is None:
                continue
            cmd = parts[rpy_idx + 1:]
            if len(cmd) < 2:
                continue

            domain, action = cmd[0], cmd[1]

            if domain == "db" and action == "apply" and len(cmd) >= 4:
                layer, name = cmd[2], cmd[3]
                if layer in LAYERS:
                    used.add(f"{layer}.{name}")

            elif domain == "jobs" and action == "apply" and len(cmd) >= 3:
                ref = cmd[2]
                if "." in ref and not ref.startswith(".") and not ref.endswith("."):
                    ns, job = ref.split(".", 1)
                    used.add(f"jobs.{ns}.{job}")

            elif domain == "integrations" and action == "apply" and len(cmd) >= 3:
                ref = cmd[2]
                if "." in ref and not ref.startswith(".") and not ref.endswith("."):
                    ns, name = ref.split(".", 1)
                    used.add(f"integrations.{ns}.{name}")

    # --- Report ---
    any_unused = False
    for domain in ("db", "jobs", "integrations"):
        unused = sorted(available[domain] - used)
        if unused:
            any_unused = True
            typer.echo(f"[{domain}]")
            for t in unused:
                typer.echo(f"  {t}")

    if not any_unused:
        typer.echo("No unused targets found.")


# --- Inspect command ---

@app.command(name="inspect", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def inspect_run(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Target in 'namespace.target' format (e.g. db.active_users)"),
) -> None:
    """Run a read-only inspect target. Format: namespace.target (e.g. db.active_users)."""
    if ctx.args:
        bad = f"{ref} {' '.join(ctx.args)}"
        typer.echo(f"Error: expected format 'namespace.target', got '{bad}'", err=True)
        typer.echo("Usage: run.py inspect <namespace.target>", err=True)
        raise typer.Exit(1)
    if "." not in ref or ref.startswith(".") or ref.endswith("."):
        typer.echo(f"Error: expected format 'namespace.target', got '{ref}'", err=True)
        typer.echo("Usage: run.py inspect <namespace.target>", err=True)
        raise typer.Exit(1)
    group, name = ref.split(".", 1)

    group_path = ROOT / "inspect" / group
    if not group_path.exists():
        available = sorted(p.name for p in (ROOT / "inspect").iterdir() if p.is_dir()) if (ROOT / "inspect").exists() else []
        typer.echo(f"Error: inspect namespace '{group}' not found. Available: {available}", err=True)
        raise typer.Exit(1)

    sql_file = group_path / f"{name}.sql"
    sh_file = group_path / f"{name}.sh"

    if sql_file.exists() and sh_file.exists():
        typer.echo(f"Error: ambiguous target (both .sql and .sh found): {ref}", err=True)
        raise typer.Exit(1)

    if not sql_file.exists() and not sh_file.exists():
        typer.echo(f"Error: target not found: {ref}", err=True)
        raise typer.Exit(1)

    if VERBOSE >= 1:
        print(f"[STEP] inspect: {ref}")
    if sql_file.exists():
        if not shutil.which("psql"):
            typer.echo("Error: psql is required to execute SQL files in inspect/", err=True)
            raise typer.Exit(1)
        subprocess.run(["psql", "-f", str(sql_file)], env=os.environ, check=True)
    else:
        subprocess.run(["bash", str(sh_file)], env=os.environ, check=True)


if __name__ == "__main__":
    try:
        app(standalone_mode=False)
    except click.exceptions.Exit as e:
        sys.exit(e.exit_code)
    except click.exceptions.Abort:
        sys.exit(1)
    except click.UsageError as e:
        typer.echo(f"Error: {e.format_message()}", err=True)
        typer.echo("", err=True)
        typer.echo(USAGE, err=True)
        sys.exit(2)
    except TypeError as e:
        if "make_metavar" in str(e):
            typer.echo("Error: --help unavailable. Use 'python run.py list' to see available targets.", err=True)
            sys.exit(1)
        raise
