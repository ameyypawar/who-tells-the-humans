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
    priv = h["files"]["private/claim_paraphrases_and_forged_line.md"]
    put("PrivateSha", priv); put("PrivateShaShort", priv[:12])
    put("TZero", facts["t_zero"]["value"])
    put("PriorArtThrough", facts["prior_art_through"]["value"])
    put("AuditSampleMod", exp["audit"]["production_sample_mod"])
    put("SourceRef", ref)
    put("Provisional", "yes" if prov else "no")

    lines = ["% Generated by tools/design_macros.py. Do not edit.",
             f"% source ref: {ref}" + (" (PROVISIONAL: frozen-v1 does not exist yet, fell back to main)" if prov else ""),
             f"% note: configs at {ref} still say mcp_server_name={exp['mcp_server_name']}; D-012 (rename) not recorded yet" if "D-012" not in ids else ""]
    lines += [rf"\newcommand{{\wtthDesign{k}}}{{{v}}}" for k, v in out.items()]
    write_generated(PAPER / "generated/design/design.tex", "\n".join(l for l in lines if l) + "\n")
    print(f"design_macros: {len(out)} macros from {ref}{' (PROVISIONAL)' if prov else ''}")


def json_get(ref: str, path: str, key: str):
    import json
    return json.loads(show(ref, path))[key]


if __name__ == "__main__":
    main()
