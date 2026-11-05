"""Config loading and validation (research/41 sections 2.2, 2.3.1, 2.5, 2.9).

`load_settings()` reads configs/experiment.yaml into nested frozen dataclasses (attribute access:
`settings.budget.cap_usd`, `settings.scheduler.k_init`, ...). Relative paths in `paths` resolve against the
repo root (the parent of configs/). Private text is never read here; `paths.private_dir` and `paths.key_file`
are paths only.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from wtth.textnorm import canon_json, sha256_text
from wtth.types import CellSpec, Caps, ToolSpec, Toolset, World

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS_DIR = REPO_ROOT / "configs"

C2_VALUES = ("C2.R", "C2.CN", "CLAIM", "CLAIM+FORGED", "C2.OBJ")
V_LINES_VALUES = ("F", "BN")
TOOLSET_NAMES = ("main", "backup", "board_only")
PHASE_MODES = ("live", "fake")
TIERS = ("full", "lean", "mvp")


# MCP server names Claude Code 2.1.317 refuses to load ('reserved_name'), after normalization (lowercase, every
# character outside [a-z0-9_-] and '-' mapped to '_').
RESERVED_MCP_NAMES = frozenset({"workspace", "terminal", "office", "visualize", "window_halo", "dev_debug",
                                "ccd_session", "ccd_session_mgmt"})


def normalize_mcp_name(name: str) -> str:
    return "".join(ch if ("a" <= ch <= "z" or "0" <= ch <= "9" or ch == "_") else "_" for ch in name.lower())


def tool_prefix(settings: "Settings") -> str:
    """'mcp__<server>__': the prefix Claude Code puts on every tool of the configured MCP server."""
    return f"mcp__{settings.mcp_server_name}__"


class ConfigError(ValueError):
    """Invalid or inconsistent configuration."""


# ---- settings dataclasses ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Paths:
    repo_root: Path
    runs_dir: Path
    prereg_dir: Path
    private_dir: Path
    binary: Path
    binary_sha256_file: Path
    cc_settings: Path
    cwd: Path
    slots_root: Path
    python: Path
    key_file: Path
    rehearsal_dir: Path
    probe_dir: Path
    tools_yaml: Path

    @property
    def db_path(self) -> Path:
        return self.runs_dir / "wtth.sqlite"

    @property
    def stimuli_dir(self) -> Path:
        return self.runs_dir / "stimuli"

    @property
    def turns_dir(self) -> Path:
        return self.runs_dir / "turns"

    @property
    def faults_dir(self) -> Path:
        return self.runs_dir / "faults"

    @property
    def manifests_dir(self) -> Path:
        return self.runs_dir / "manifests"

    @property
    def backups_dir(self) -> Path:
        return self.runs_dir / "backups"

    @property
    def reports_dir(self) -> Path:
        return self.runs_dir / "reports"

    @property
    def lock_path(self) -> Path:
        return self.runs_dir / "wtth.lock"

    @property
    def health_log(self) -> Path:
        return self.runs_dir / "health.log"

    @property
    def alert_path(self) -> Path:
        return self.runs_dir / "ALERT.json"

    @property
    def denyprobe_settings(self) -> Path:
        return self.stimuli_dir / "cc-settings-denyprobe.json"


@dataclass(frozen=True, slots=True)
class Models:
    haiku: str
    sonnet: str
    judge: str
    effort: str
    max_budget_usd: dict[str, float]

    def id_for(self, key: str) -> str:
        """Resolve 'haiku'/'sonnet'/'judge' (or an id already) to a model id."""
        if key in ("haiku", "sonnet", "judge"):
            return getattr(self, key)
        if key in self.max_budget_usd:
            return key
        raise ConfigError(f"unknown model key {key!r}")

    def budget_for(self, model_id: str) -> float:
        return self.max_budget_usd[model_id]


@dataclass(frozen=True, slots=True)
class Budget:
    cap_usd: float
    warn_usd: float
    prior_spend_usd: float
    reserve_calls: int
    projection_after_episodes: int
    phase_soft_usd: dict[str, float]
    judge_reserve_usd: float = 15.0   # experiment launches test against cap_usd - judge_reserve_usd


@dataclass(frozen=True, slots=True)
class Scheduler:
    k_init: int
    k_target: int
    k_max: int
    k_min: int
    episodes_per_k: int
    ramp_interval_s: float
    launch_gap_ms: float
    pressure_interval_s: float
    k_grow_interval_s: float
    pressure_high_stop_s: float
    max_total_rss_mb: int
    slots: int
    health_interval_s: float
    checkpoint_every_episodes: int
    backup_interval_s: float
    requeue_delay_s: float
    api_backoff_base_s: float
    api_backoff_cap_s: float
    reconcile_page_after: int
    breaker_trip_after: int
    breaker_pause_s: float
    breaker_stop_trips: int
    breaker_window_min: float
    breaker_resume_min: float
    debrief_tries: int
    debrief_backoff_s: tuple[float, ...]
    error_rate_stop: float = 0.02            # D-005 / 42 A-21: STOP at >= this technical-failure rate per attempt
    error_rate_min_attempts: int = 50        # below this the rate is recorded but not acted on
    error_rate_check_after_s: float = 7200   # first check: phase launch + 2 h
    error_rate_abs_check: str = ""            # second check: an absolute ISO time (T0 + 10.5 h)
    remote_check_interval_s: float = 30
    drift_check_interval_s: float = 60


@dataclass(frozen=True, slots=True)
class Timeouts:
    turn_s: float
    grace_int_s: float
    grace_term_s: float


@dataclass(frozen=True, slots=True)
class MaxTurns:
    witness: int
    debrief: int
    frozen: bool


@dataclass(frozen=True, slots=True)
class Audit:
    production_sample_mod: int
    all_turns_phases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Cuts:
    tier: str
    max_seed_by_cell: dict[str, int]
    tiers: dict[str, dict[str, int]]


@dataclass(frozen=True, slots=True)
class ScenarioCfg:
    name: str
    handle_prefix: str
    toolset_map: dict[str, str]    # cell.toolset -> toolset used in this scenario
    system_map: dict[str, str]     # cell.system -> system prompt id used in this scenario
    paragraph_map: dict[str, str]  # runbook paragraph id -> id used in this scenario
    keywords: tuple[str, ...]      # P1 keyword list (casefold substring)


@dataclass(frozen=True, slots=True)
class ArmCell:
    cell: str
    seed_start: int
    count: int
    probe: bool = False

    @property
    def seeds(self) -> range:
        return range(self.seed_start, self.seed_start + self.count)


@dataclass(frozen=True, slots=True)
class Arm:
    model: str                 # resolved model id
    model_key: str             # 'haiku' | 'sonnet'
    scenario: str
    level: str | None          # 'L1'..'L3', 'F' (= frozen level) or None (given on the CLI)
    cells: tuple[ArmCell, ...]


@dataclass(frozen=True, slots=True)
class DenyProbe:
    cell: str
    seed: int
    model: str
    level: str


@dataclass(frozen=True, slots=True)
class Phase:
    name: str
    ordinal: int
    mode: str                  # 'live' | 'fake'
    arms: tuple[Arm, ...]
    deny_probe: DenyProbe | None = None


@dataclass(frozen=True, slots=True)
class Settings:
    config_path: Path
    repo_root: Path
    fake: bool
    frozen_level: str | None
    permutation_seed: int
    max_attempts: int
    paths: Paths
    models: Models
    budget: Budget
    scheduler: Scheduler
    timeouts: Timeouts
    max_turns: MaxTurns
    audit: Audit
    cuts: Cuts
    scenarios: dict[str, ScenarioCfg]
    runbooks: dict[str, list[str]]
    core_cells: tuple[str, ...]
    cells: dict[str, CellSpec]
    phases: dict[str, Phase]
    mcp_env_guard: str = "refuse"  # WP2-ADD: "refuse" | "scrub" (server behaviour on credential-like env names)
    mcp_server_name: str = "workspace"  # WP4-ADD: the MCP server key; derives tool prefix, init checks, deny probe
    allowed_tools_separate: bool = False  # WP3-ADD: --allowedTools as separate argv elements (research/41 4.3 item 3)

    def keywords(self, scenario: str) -> tuple[str, ...]:
        return self.scenarios[scenario].keywords


# ---- loading ----------------------------------------------------------------------------------------

def _deep_merge(base: dict, extra: dict) -> dict:
    for key, val in extra.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], val)
        else:
            base[key] = copy.deepcopy(val)
    return base


def _apply_overrides(raw: dict, overrides: dict) -> dict:
    for key, val in overrides.items():
        node = raw
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        last = parts[-1]
        if isinstance(val, dict) and isinstance(node.get(last), dict):
            _deep_merge(node[last], val)
        else:
            node[last] = copy.deepcopy(val)
    return raw


def _path(root: Path, value: Any) -> Path:
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else root / p


def _req(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise ConfigError(f"missing key {where}.{key}")
    return d[key]


def _build_cells(raw_cells: dict, runbooks: dict) -> dict[str, CellSpec]:
    cells: dict[str, CellSpec] = {}
    for name, c in raw_cells.items():
        try:
            spec = CellSpec(
                name=name, world=World(c["world"]), runbook=c["runbook"], system=c["system"],
                toolset=c["toolset"], c2=c["c2"], c_note=bool(c["c_note"]), v_lines=c["v_lines"],
                header_owner=bool(c.get("header_owner", False)), owner=c.get("owner"))
        except (KeyError, ValueError) as exc:
            raise ConfigError(f"cell {name}: {exc}") from exc
        if spec.c2 not in C2_VALUES:
            raise ConfigError(f"cell {name}: bad c2 {spec.c2!r}")
        if spec.v_lines not in V_LINES_VALUES:
            raise ConfigError(f"cell {name}: bad v_lines {spec.v_lines!r}")
        if spec.toolset not in TOOLSET_NAMES:
            raise ConfigError(f"cell {name}: bad toolset {spec.toolset!r}")
        if spec.runbook not in runbooks:
            raise ConfigError(f"cell {name}: unknown runbook {spec.runbook!r}")
        if spec.header_owner and not spec.owner:
            raise ConfigError(f"cell {name}: header_owner needs an owner")
        cells[name] = spec
    return cells


def _build_phases(raw_phases: dict, models: Models, cells: dict, scenarios: dict) -> dict[str, Phase]:
    phases: dict[str, Phase] = {}
    ordinals: set[int] = set()
    for name, p in raw_phases.items():
        ordinal = int(_req(p, "ordinal", f"phases.{name}"))
        if ordinal in ordinals:
            raise ConfigError(f"phase {name}: duplicate ordinal {ordinal}")
        ordinals.add(ordinal)
        mode = p.get("mode", "live")
        if mode not in PHASE_MODES:
            raise ConfigError(f"phase {name}: bad mode {mode!r}")
        arms: list[Arm] = []
        for arm in p.get("arms") or []:
            if arm["scenario"] not in scenarios:
                raise ConfigError(f"phase {name}: unknown scenario {arm['scenario']!r}")
            acs = []
            for ac in arm["cells"]:
                if ac["cell"] not in cells:
                    raise ConfigError(f"phase {name}: unknown cell {ac['cell']!r}")
                acs.append(ArmCell(ac["cell"], int(ac["seed_start"]), int(ac["count"]), bool(ac.get("probe", False))))
            arms.append(Arm(models.id_for(arm["model"]), arm["model"], arm["scenario"], arm.get("level"), tuple(acs)))
        dp = None
        if p.get("deny_probe"):
            d = p["deny_probe"]
            dp = DenyProbe(d["cell"], int(d["seed"]), models.id_for(d["model"]), d["level"])
        phases[name] = Phase(name, ordinal, mode, tuple(arms), dp)
    return phases


def _mcp_env_guard(v: Any) -> str:  # WP2-ADD
    if v not in ("refuse", "scrub"):
        raise ConfigError(f"mcp_env_guard {v!r} not in ('refuse', 'scrub')")
    return v


def load_settings(path: Path | None = None, overrides: dict | None = None) -> Settings:
    """Load and validate configs/experiment.yaml. `overrides` merges into the raw mapping; keys may be dotted
    ("paths.runs_dir") or nested dicts."""
    cfg_path = Path(path) if path is not None else CONFIGS_DIR / "experiment.yaml"
    root = cfg_path.resolve().parent.parent
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if overrides:
        _apply_overrides(raw, overrides)

    rp = raw["paths"]
    paths = Paths(
        repo_root=root, runs_dir=_path(root, rp["runs_dir"]), prereg_dir=_path(root, rp["prereg_dir"]),
        private_dir=_path(root, rp["private_dir"]), binary=_path(root, rp["binary"]),
        binary_sha256_file=_path(root, rp["binary_sha256_file"]), cc_settings=_path(root, rp["cc_settings"]),
        cwd=_path(root, rp["cwd"]), slots_root=_path(root, rp["slots_root"]), python=_path(root, rp["python"]),
        key_file=_path(root, rp["key_file"]), rehearsal_dir=_path(root, rp["rehearsal_dir"]),
        probe_dir=_path(root, rp["probe_dir"]), tools_yaml=_path(root, rp["tools_yaml"]))

    rm = raw["models"]
    models = Models(rm["haiku"], rm["sonnet"], rm["judge"], rm["effort"],
                    {k: float(v) for k, v in rm["max_budget_usd"].items()})
    rb = raw["budget"]
    budget = Budget(float(rb["cap_usd"]), float(rb["warn_usd"]), float(rb["prior_spend_usd"]),
                    int(rb["reserve_calls"]), int(rb["projection_after_episodes"]),
                    {k: float(v) for k, v in rb["phase_soft_usd"].items()},
                    float(rb.get("judge_reserve_usd", 15.0)))
    rs = dict(raw["scheduler"])
    rs["debrief_backoff_s"] = tuple(float(x) for x in rs["debrief_backoff_s"])
    try:
        scheduler = Scheduler(**rs)
    except TypeError as exc:
        raise ConfigError(f"scheduler: {exc}") from exc
    timeouts = Timeouts(**raw["timeouts"])
    max_turns = MaxTurns(int(raw["max_turns"]["witness"]), int(raw["max_turns"]["debrief"]),
                         bool(raw["max_turns"]["frozen"]))
    audit = Audit(int(raw["audit"]["production_sample_mod"]), tuple(raw["audit"]["all_turns_phases"]))
    rc = raw["cuts"]
    if rc["tier"] not in TIERS:
        raise ConfigError(f"cuts.tier {rc['tier']!r} not in {TIERS}")
    cuts = Cuts(rc["tier"], {k: int(v) for k, v in (rc.get("max_seed_by_cell") or {}).items()},
                {t: {k: int(v) for k, v in m.items()} for t, m in rc["tiers"].items()})
    scenarios = {
        n: ScenarioCfg(n, s["handle_prefix"], dict(s["toolset_map"]), dict(s["system_map"]),
                       dict(s["paragraph_map"]), tuple(s["keywords"]))
        for n, s in raw["scenarios"].items()}
    runbooks = {k: list(v) for k, v in raw["runbooks"].items()}
    cells = _build_cells(raw["cells"], runbooks)
    core_cells = tuple(raw["core_cells"])
    for c in core_cells:
        if c not in cells:
            raise ConfigError(f"core_cells: unknown cell {c!r}")
    phases = _build_phases(raw["phases"], models, cells, scenarios)
    fl = raw.get("frozen_level")
    if fl is not None and fl not in ("L1", "L2", "L3"):
        raise ConfigError(f"frozen_level {fl!r}")
    return Settings(
        config_path=cfg_path, repo_root=root, fake=bool(raw.get("fake", False)), frozen_level=fl,
        permutation_seed=int(raw["permutation_seed"]), max_attempts=int(raw["max_attempts"]),
        paths=paths, models=models, budget=budget, scheduler=scheduler, timeouts=timeouts,
        max_turns=max_turns, audit=audit, cuts=cuts, scenarios=scenarios, runbooks=runbooks,
        core_cells=core_cells, cells=cells, phases=phases,
        mcp_server_name=str(raw.get("mcp_server_name", "workspace")),  # WP4-ADD
        allowed_tools_separate=bool(raw.get("allowed_tools_separate", False)),  # WP3-ADD
        mcp_env_guard=_mcp_env_guard(raw.get("mcp_env_guard", "refuse")))  # WP2-ADD


# ---- tools.yaml -------------------------------------------------------------------------------------

def tools_sha(tools: tuple[ToolSpec, ...] | list[ToolSpec]) -> str:
    """sha256 of canon_json([{name, description, input_schema}, ...]) in listing order."""
    return sha256_text(canon_json(
        [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]))


def _read_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_toolsets(path: Path) -> dict[str, Toolset]:
    raw = _read_yaml(Path(path))
    out: dict[str, Toolset] = {}
    for name, ts in raw["toolsets"].items():
        tools = tuple(ToolSpec(t["name"], t["description"], t["input_schema"], t["reply"]) for t in ts["tools"])
        out[name] = Toolset(name, ts["id_field"], tools, tools_sha(tools))
    return out


def load_caps(path: Path) -> Caps:
    c = _read_yaml(Path(path))["caps"]
    return Caps(int(c["post_chars"]), int(c["subject_chars"]), int(c["message_chars"]))


# ---- JSON side files --------------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_expected_init(path: Path, settings: "Settings | None" = None) -> dict:
    """expected_init.json; with `settings` the server name and tool prefix follow settings.mcp_server_name
    (and cwd follows paths.cwd, which differs in fake runs)."""
    exp = _read_json(Path(path))
    if settings is not None:
        exp["mcp_server_name"] = settings.mcp_server_name
        exp["tool_prefix"] = tool_prefix(settings)
        exp["cwd"] = str(settings.paths.cwd)
    return exp


def load_prereg_hashes(path: Path) -> dict[str, str]:
    """{path under prereg/: sha256}. Keys are the paths as written in PREREG section 23 ('prompts/tools.md',
    'private/...', 'duty_word_check.md', 'references.bib') plus 'PREREG.md' (the frozen file's own hash, recorded
    by the prereg-v1 tag, not by section 23)."""
    return dict(_read_json(Path(path))["files"])


def load_prereg_pins(path: Path) -> dict:
    """The whole prereg_hashes.json: {'binary_sha256': ..., 'files': {...}}."""
    return _read_json(Path(path))


def env_allowlist(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(base, audit). base = fixed child env values; audit = extra vars on audit turns
    (OTEL_LOG_RAW_API_BODIES holds the template 'file:{bodies_dir}'). HOME, TMPDIR, CLAUDE_CONFIG_DIR and
    ANTHROPIC_API_KEY are dynamic; see env_dynamic_names()."""
    d = _read_json(Path(path))
    return dict(d["base"]), dict(d["audit"])


def env_dynamic_names(path: Path) -> tuple[str, ...]:
    return tuple(_read_json(Path(path))["dynamic"])


def load_log_allowlists(path: Path) -> dict:
    return _read_json(Path(path))
