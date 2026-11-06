"""Write generated/design/design.tex (\\wtthDesign...) from the configs at frozen-v1 (fallback: main, PROVISIONAL)."""
import re
import subprocess

import yaml

from _common import PAPER, REPO, git, hashes, resolve_ref, show, tex_escape, write_generated

WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
         11: "eleven", 12: "twelve"}
DIGIT = {"0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four", "5": "Five", "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine"}


def tok(s: str) -> str:
    return "".join(DIGIT.get(c, c) for c in "".join(p[:1].upper() + p[1:] for p in re.split(r"[^A-Za-z0-9]+", s) if p))


CELL_TOKENS = {"NONE": "None", "CONCERN": "Concern", "CLAIM": "Claim", "POLICY": "Policy", "NONE-R": "NoneR", "RECEIPT": "Receipt",
               "NO-RECEIPT": "NoReceipt", "FORGED": "Forged", "NEG": "Neg", "POS": "Pos", "B-NONE": "BNone"}


def fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", "{,}")


def main() -> None:
    ref, prov = resolve_ref()
    exp = yaml.safe_load(show(ref, "configs/experiment.yaml"))
    tools = yaml.safe_load(show(ref, "configs/tools.yaml"))
    h = hashes(ref)
    const = yaml.safe_load((PAPER / "data/prereg_constants.yaml").read_text())
    facts = yaml.safe_load((PAPER / "data/process_facts.yaml").read_text())
    out: dict[str, str] = {}

    def put(k: str, v) -> None:
        assert k not in out, k
        out[k] = str(v)

    core = exp["phases"]["core"]["arms"][0]["cells"]
    sonnet = exp["phases"]["sonnet"]["arms"][0]["cells"]
    put("Cells", len(exp["core_cells"]))
    put("CellsWord", WORDS[len(exp["core_cells"])])
    for c in core:
        put("Seeds" + CELL_TOKENS[c["cell"]], c["count"])
    put("CoreEpisodes", fmt_int(sum(c["count"] for c in core)))
    put("SonnetEpisodes", fmt_int(sum(c["count"] for c in sonnet)))
    put("SonnetCells", len(sonnet))
    put("SonnetCellsWord", WORDS[len(sonnet)])
    for k, key in (("Rounds", "rounds"), ("LiveWitnesses", "live_witnesses"), ("Roster", "roster"),
                   ("TicketsPerRound", "tickets_per_round")):
        n = int(const[key]["value"])
        put(k, n)
        put(k + "Word", WORDS[n])
    for k, key in (("Alpha", "alpha"), ("GOneBandLo", "g1_band_lo"), ("GOneBandHi", "g1_band_hi"), ("GOneCeiling", "g1_ceiling"),
                   ("GTwoGap", "g2_gap"), ("GTwoClearFail", "g2_clear_fail"), ("GZeroRefusal", "g0_refusal")):
        put(k, const[key]["value"])
    b = exp["budget"]
    put("CapUsd", r"\$" + f"{b['cap_usd']:.2f}")
    put("JudgeReserveUsd", r"\$" + f"{b['judge_reserve_usd']:.2f}")
    mb = exp["models"]["max_budget_usd"]
    put("MaxBudgetHaiku", r"\$" + f"{mb[exp['models']['haiku']]:.2f}")
    put("MaxBudgetSonnet", r"\$" + f"{mb[exp['models']['sonnet']]:.2f}")
    put("TurnTimeout", exp["timeouts"]["turn_s"])
    put("MaxAttempts", exp["max_attempts"])
    put("MaxAttemptsWord", WORDS[exp["max_attempts"]])
    caps = tools["caps"]
    put("LenPost", fmt_int(caps["post_chars"]))
    put("LenMessage", fmt_int(caps["message_chars"]))
    put("LenSubject", fmt_int(caps["subject_chars"]))
    s = exp["scheduler"]
    put("KInit", s["k_init"]); put("KTarget", s["k_target"]); put("KMax", s["k_max"])
    put("CliVersion", json_get(ref, "configs/expected_init.json", "claude_code_version"))
    put("BinarySha", h["binary_sha256"])
    put("BinaryShaShort", h["binary_sha256"][:12])
    names = facts["model_names"]
    for k, mid in (("Haiku", exp["models"]["haiku"]), ("Sonnet", exp["models"]["sonnet"]), ("Judge", exp["models"]["judge"]),
                   ("Annotator", facts["annotator_model"]["value"])):
        put("Model" + k, r"\texttt{" + tex_escape(mid) + "}")
        put("Model" + k + "Name", names[mid])
    put("Effort", exp["models"]["effort"])
    put("McpServerName", r"\texttt{" + tex_escape(exp["mcp_server_name"]) + "}")
    put("Keywords", ", ".join(r"\textit{" + tex_escape(k) + "}" for k in exp["scenarios"]["main"]["keywords"]))
    pre = h["files"]["PREREG.md"]
    put("PreregSha", pre); put("PreregShaShort", pre[:12])
    put("PreregFrozenDate", facts["prereg_frozen_date"]["value"])
    tagdate = git("for-each-ref", "--format=%(taggerdate:format:%-d %B %Y)", "refs/tags/prereg-v1").strip()
    put("PreregTagDate", tagdate)
    put("PreregTagCommit", git("rev-parse", "--short=7", "prereg-v1^{commit}").strip())
    try:
        put("FrozenTagCommit", git("rev-parse", "--short=7", "frozen-v1^{commit}").strip())
    except subprocess.CalledProcessError:
        put("FrozenTagCommit", "not yet created")
    dev = show(ref, "DEVIATIONS.md").decode()
    ids = re.findall(r"^## (D-\d{3})", dev, flags=re.M)
    put("DeviationsLast", ids[-1])
    # the last entry at HEAD (Appendix C lists every deviation, including those recorded after the freeze)
    put("DeviationsLastHead", re.findall(r"^## (D-\d{3})", show("HEAD", "DEVIATIONS.md").decode(), flags=re.M)[-1])
    priv = h["files"]["private/claim_paraphrases_and_forged_line.md"]
    put("PrivateSha", priv); put("PrivateShaShort", priv[:12])
    privb = h["files"]["private/backup_scenario_private.md"]
    put("PrivateBackupSha", privb); put("PrivateBackupShaShort", privb[:12])
    put("TZero", facts["t_zero"]["value"])
    put("PriorArtThrough", facts["prior_art_through"]["value"])
    put("PreregGistDate", facts["prereg_gist_date"]["value"])
    put("OssAuditDate", facts["oss_audit_date"]["value"])
    put("OssAuditRecheckDate", facts["oss_audit_recheck_date"]["value"])
    put("ReviewAuthorChanged", facts["review_author_changed"]["value"])
    put("AuditSampleMod", exp["audit"]["production_sample_mod"])
    # P2 is defined only where C did not really report and there is a violation (PREREG section 10): derived from the cells
    p2 = [c for c in exp["core_cells"] if not exp["cells"][c]["c_note"] and exp["cells"][c]["v_lines"] == "F"]
    put("PTwoCells", len(p2)); put("PTwoCellsWord", WORDS[len(p2)])
    for k, key in (("PowerPct", "power_pct"), ("PredStandDownLo", "pred_standdown_lo"), ("PredStandDownHi", "pred_standdown_hi"),
                   ("PredReceiptsWithin", "pred_receipts_within")):
        put(k, const[key]["value"])
    ana = yaml.safe_load(show(ref, "configs/analysis.yaml"))
    put("KappaThreshold", f"{ana['kappa_threshold']:.2f}")
    put("BootstrapResamples", fmt_int(ana["bootstrap_B"]))
    for k, v in exp1_sentences().items():
        put(k, v)
    write_env_table(ref)
    write_preexisting_extension(facts["preexisting_extension"])
    put("SourceRef", ref)
    put("Provisional", "yes" if prov else "no")

    lines = ["% Generated by tools/design_macros.py. Do not edit.",
             f"% source ref: {ref}" + (" (PROVISIONAL: frozen-v1 does not exist yet, fell back to main)" if prov else ""),
             f"% note: configs at {ref} still say mcp_server_name={exp['mcp_server_name']}; D-012 (rename) not recorded yet" if "D-012" not in ids else ""]
    lines += [rf"\newcommand{{\wtthDesign{k}}}{{{v}}}" for k, v in out.items()]
    write_generated(PAPER / "generated/design/design.tex", "\n".join(l for l in lines if l) + "\n")
    print(f"design_macros: {len(out)} macros from {ref}{' (PROVISIONAL)' if prov else ''}")


EXP1_TAG = "exp1-v1"
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")


def fenced_block(md: str, heading_prefix: str) -> str:
    """The first ```text block under the first heading that starts with `heading_prefix`: the parse of
    wtth.prereg.fenced_blocks / block_at (lines joined with \\n, no trailing newline)."""
    heading, cur = "", None
    for line in md.split("\n"):
        if cur is None:
            m = _HEADING.match(line)
            if m:
                heading = m.group(1)
            elif line == "```text":
                cur = []
        elif line == "```":
            if heading.startswith(heading_prefix):
                return "\n".join(cur)
            cur = None
        else:
            cur.append(line)
    raise SystemExit(f"design_macros: no ```text block under a heading starting {heading_prefix!r}")


def exp1_sentences() -> dict[str, str]:
    """The two sentences the D-022 follow-up added to FORGED's runbook, read at the tag exp1-v1 that froze them, so the
    paper never types them. ExpOnePolicySentence: RB-POLICY from prereg/prompts/runbook.md (SHA-256 checked against
    configs/prereg_hashes.json); ExpOneProvSentence: XP-PROV from exp/prompts/runbook_exp1.md (SHA-256 checked against
    configs/exp1.yaml). Where runs/stimuli holds the system prompt the witnesses were sent, its last paragraph must be
    the same text, byte for byte."""
    import hashlib

    try:
        git("rev-parse", "--verify", "--quiet", f"refs/tags/{EXP1_TAG}")
    except subprocess.CalledProcessError:
        raise SystemExit(f"design_macros: tag {EXP1_TAG} is missing; the follow-up's text is frozen there")
    cfg = yaml.safe_load(show(EXP1_TAG, "configs/exp1.yaml"))
    ent = next(x for x in cfg["extra_texts"] if x["path"] == "exp/prompts/runbook_exp1.md")
    raw = show(EXP1_TAG, ent["path"])
    if hashlib.sha256(raw).hexdigest() != ent["sha256"]:
        raise SystemExit("design_macros: SHA-256 of exp/prompts/runbook_exp1.md differs from configs/exp1.yaml")
    rb = show(EXP1_TAG, "prereg/prompts/runbook.md")
    if hashlib.sha256(rb).hexdigest() != hashes(EXP1_TAG)["files"]["prompts/runbook.md"]:
        raise SystemExit("design_macros: SHA-256 of prompts/runbook.md differs from configs/prereg_hashes.json")
    texts = {"ExpOnePolicySentence": ("V-R1-POLICY", fenced_block(rb.decode("utf-8"), "RB-POLICY")),
             "ExpOneProvSentence": ("V-R1-PROV", fenced_block(raw.decode("utf-8"), "XP-PROV"))}
    for variant, t in texts.values():
        sent = REPO / "runs" / "stimuli" / f"system_main_SB-1_{variant}.txt"
        if sent.is_file() and sent.read_text(encoding="utf-8").split("\n\n")[-1] != t:
            raise SystemExit(f"design_macros: the last paragraph of runs/stimuli/{sent.name} differs from the tagged text")
    return {k: tex_escape(t) for k, (_, t) in texts.items()}


def json_get(ref: str, path: str, key: str):
    import json
    return json.loads(show(ref, path))[key]


def write_env_table(ref: str) -> None:
    """generated/design/env_allowlist.tex: the turn environment from configs/env_allowlist.json (names and fixed values)."""
    import json
    env = json.loads(show(ref, "configs/env_allowlist.json"))
    tt = lambda s: r"\texttt{" + tex_escape(str(s)) + "}"
    rows = [(tt(k), tt(v), "every turn") for k, v in env["base"].items()]
    rows += [(tt(k), tt(v), "audited turns only") for k, v in env["audit"].items()]
    rows += [(tt(k), "set per slot or per call", "every turn") for k in env["dynamic"]]
    lines = [f"% Generated by tools/design_macros.py from configs/env_allowlist.json at {ref}. Do not edit.",
             r"\begin{tabularx}{\linewidth}{@{}>{\raggedright\arraybackslash}p{0.5\linewidth}>{\raggedright\arraybackslash}Xl@{}}",
             r"\toprule", r"Variable & Value & Scope \\", r"\midrule"]
    lines += [f"{a} & {b} & {c} \\\\" for a, b, c in rows]
    lines += [r"\bottomrule", r"\end{tabularx}"]
    write_generated(PAPER / "generated/design/env_allowlist.tex", "\n".join(lines) + "\n")


def write_preexisting_extension(rows: list) -> None:
    """generated/design/preexisting_extension.tex: research notes after PREREG section 20's list (research/01-34) that were
    last modified before T0 (data/process_facts.yaml; research/51 item 7). Appendix C.2 and the README list them."""
    lines = ["% Generated by tools/design_macros.py from data/process_facts.yaml (preexisting_extension). Do not edit.",
             r"\begin{tabular}{@{}lll@{}}", r"\toprule", r"Research note & Content & Last modified \\", r"\midrule"]
    lines += [rf"\texttt{{{tex_escape(r['note'])}}} & {tex_escape(r['what'])} & {tex_escape(r['modified'])} \\" for r in rows]
    lines += [r"\bottomrule", r"\end{tabular}"]
    write_generated(PAPER / "generated/design/preexisting_extension.tex", "\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
