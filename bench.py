#!/usr/bin/env python3
"""token-bench: how much does an AI agent cost, and how stable is it, when it uses a browser?

Compares 4 modes with the SAME model, SAME task, SAME flags (only Claude Code is the agent):
  baseline : no browser at all. Measures the fixed overhead of the agent itself.
  mcp      : agent uses Playwright MCP tools.
  cli      : agent uses `playwright-cli` through Bash.
  codegen  : agent writes ONE Playwright script and fixes it until it works.

Two tasks, both on https://automationexercise.com (a QA practice shop):
  search : search a product and return the first result (short, ~6 steps).
  cart   : a longer flow (~20 actions): add 3 products, change quantities, remove one,
           read the cart. The script checks the arithmetic, not the model.

Usage:
  python3 bench.py run --task cart --runs 1
  python3 bench.py report --task cart
  python3 bench.py why --task cart        # why did the failed runs fail?
Stdlib only. Needs: `claude` (Claude Code), node/npx, `npm i -g @playwright/cli`,
and for codegen `npm i playwright` in this folder.
"""
import argparse, csv, json, os, re, shutil, statistics, subprocess, tempfile, time
from pathlib import Path

ROOT = Path(__file__).parent
RESULTS = ROOT / "results"  # set per task in main()
_c = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE = str(Path(_c).resolve()) if "/" in _c else (shutil.which(_c) or _c)  # absolute: runs happen in a temp dir

SITES = {"google": "https://www.google.com", "duckduckgo": "https://duckduckgo.com"}
DEFAULT_SITE = {"search": "https://automationexercise.com/products", "cart": "https://automationexercise.com"}
DEFAULT_TURNS = {"search": 40, "cart": 90}
DEFAULT_TIMEOUT = {"search": 600, "cart": 1200}

BLOCK_RULE = ("If the page shows a captcha, consent wall or 'unusual traffic' block that you "
              "cannot pass in a few steps, stop and answer exactly: BLOCKED.")

TASKS = {
    "search": (
        "Go to {site}, search for \"{query}\", and find the FIRST result (skip ads). "
        + BLOCK_RULE + " Otherwise, as your final answer, write one single line in this format:\n"
        "RESULT: <name of the first result> | <its full URL>\nDo not write anything else."
    ),
    "cart": (
        "Go to {site} and do these steps in order:\n"
        "1. Search for \"dress\". Open the FIRST result. Set the quantity to 3 and add it to the cart. Continue shopping.\n"
        "2. Search for \"top\". Add the FIRST result to the cart with quantity 1. Continue shopping.\n"
        "3. Search for \"jeans\". Open the FIRST result. Set the quantity to 2 and add it to the cart. Continue shopping.\n"
        "4. Open the cart. Remove the item you added in step 2 (the \"top\").\n"
        "5. Read the cart as it is now.\n"
        + BLOCK_RULE + " Otherwise, as your final answer write one line per cart item and then the total, "
        "exactly in this format, with plain numbers (no currency, no thousands separator):\n"
        "ITEM: <name> | <unit price> | <quantity> | <line total>\n"
        "TOTAL: <cart total>\nDo not write anything else."
    ),
}

CLI_HELP = (
    "Use the Bash tool with the `playwright-cli` command. Main commands: "
    "`playwright-cli open <url>`, `playwright-cli snapshot`, `playwright-cli fill <ref> <text>`, "
    "`playwright-cli click <ref>`, `playwright-cli press Enter`, `playwright-cli goto <url>`, "
    "`playwright-cli find <text>`, `playwright-cli close`. Refs (like e12) come from the snapshot. "
    "Do not use any other browser tool. Close the browser when you finish."
)

CODEGEN_HELP = (
    "The `playwright` library is already installed: do not check or install anything. "
    "Do NOT drive the browser interactively. Write ONE Node.js script at ./run.js "
    "using the `playwright` library (chromium, headless) that does the whole task and prints "
    "exactly the final answer in the requested format. Run it with `node run.js`. "
    "If it fails, fix it and run again. When it works, answer with the same final lines."
)

RUN_FIELDS = [
    "mode", "run", "ok", "blocked", "turns", "input", "cache_write", "cache_read",
    "output", "total_tokens", "cost_usd", "seconds", "denials", "tool_calls", "model", "answer", "error",
]
NUM_FIELDS = ["denials", "tool_calls", "turns", "input", "cache_write", "cache_read", "output", "total_tokens", "cost_usd"]


def playwright_paths():
    """Folders where node can find the `playwright` library (this folder, or the global one)."""
    cands = [ROOT / "node_modules"]
    try:
        g = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
        if g:
            cands.append(Path(g))
    except Exception:
        pass
    return [str(c) for c in cands if (c / "playwright").is_dir()]


def mcp_config(path: Path):
    path.write_text(json.dumps({"mcpServers": {"playwright": {
        "command": "npx", "args": ["@playwright/mcp@latest", "--headless"]}}}))


def build_cmd(mode, prompt, model, max_turns, tmp):
    cmd = [CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", model,
           "--max-turns", str(max_turns), "--no-session-persistence",
           "--disable-slash-commands", "--strict-mcp-config"]
    if mode == "mcp":
        cfg = tmp / "mcp.json"
        mcp_config(cfg)
        cmd += ["--mcp-config", str(cfg), "--allowed-tools", "mcp__playwright", "--permission-mode", "dontAsk"]
    elif mode == "cli":
        cmd += ["--allowed-tools", "Bash(playwright-cli:*)", "--permission-mode", "dontAsk"]
    elif mode == "codegen":
        cmd += ["--allowed-tools", "Bash(node:*) Bash(ls:*) Bash(cat:*) Bash(head:*) Bash(pwd:*) Bash(npm ls:*) Bash(sed:*) Bash(grep:*) Write Read Edit", "--permission-mode", "dontAsk"]
    else:  # baseline
        cmd += ["--allowed-tools", "", "--permission-mode", "dontAsk"]
    return cmd


def parse(stdout):
    """Read `claude -p --output-format stream-json --verbose`: tokens, turns, cost and tool calls."""
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    data = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if data is None:
        raise ValueError("no result event in claude output")
    tool_ids = set()  # every tool call the agent made (browser actions, ToolSearch, Bash, ...)
    for e in events:
        if e.get("type") == "assistant":
            for c in (e.get("message", {}).get("content") or []):
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tool_ids.add(c.get("id"))
    u = data.get("usage", {})
    i, cw = u.get("input_tokens", 0), u.get("cache_creation_input_tokens", 0)
    cr, o = u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0)
    return {
        "turns": data.get("num_turns", 0), "tool_calls": len(tool_ids),
        "input": i, "cache_write": cw, "cache_read": cr,
        "output": o, "total_tokens": i + cw + cr + o,
        "cost_usd": round(data.get("total_cost_usd", 0) or 0, 4),
        "answer": (data.get("result") or "").strip().replace("\n", " ")[:700],
        "is_error": bool(data.get("is_error")),
        "denials": len(data.get("permission_denials") or []),
    }


def _n(s):
    return int(s.replace(",", ""))


def check_search(answer):
    if "BLOCKED" in answer and "RESULT:" not in answer:
        return False, True
    m = re.search(r"RESULT:\s*(.+?)\s*\|\s*(https?://\S+)", answer)
    return bool(m and len(m.group(1)) > 2), False


def check_cart(answer):
    """Independent check: 2 items with quantities 3 and 2, price x qty = line, lines add up to TOTAL."""
    if "BLOCKED" in answer and "ITEM:" not in answer:
        return False, True
    money = r"(?:Rs\.?\s*)?(\d[\d,]*)"
    items = re.findall(rf"ITEM:\s*(.+?)\s*\|\s*{money}\s*\|\s*(\d+)\s*\|\s*{money}", answer)
    total = re.search(rf"TOTAL:\s*{money}", answer)
    if len(items) != 2 or not total:
        return False, False
    if sorted(int(q) for _, _, q, _ in items) != [2, 3]:
        return False, False
    if any(_n(p) * int(q) != _n(line) for _, p, q, line in items):
        return False, False
    return sum(_n(line) for *_, line in items) == _n(total.group(1)), False


CHECKS = {"search": check_search, "cart": check_cart}


def run_one(mode, run, args):
    site = SITES.get(args.site, args.site) if args.site else DEFAULT_SITE[args.task]
    task = TASKS[args.task].format(site=site, query=args.query)
    if mode == "baseline":
        prompt = "Reply with exactly: RESULT: baseline | https://example.com"
    elif mode == "cli":
        prompt = f"{task}\n\n{CLI_HELP}"
    elif mode == "codegen":
        prompt = f"{task}\n\n{CODEGEN_HELP}"
    else:
        prompt = f"{task}\n\nUse the Playwright browser tools."
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        cmd = build_cmd(mode, prompt, args.model, args.max_turns, tmp)
        t0 = time.time()
        row = {"mode": mode, "run": run, "model": args.model, "error": ""}
        try:
            env = {**os.environ, "NODE_PATH": os.pathsep.join(playwright_paths())}  # so run.js finds `playwright`
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp, timeout=args.timeout, env=env)
            RESULTS.mkdir(exist_ok=True)
            (RESULTS / f"raw_{mode}_{run}.json").write_text(p.stdout + "\n---STDERR---\n" + p.stderr)
            row.update(parse(p.stdout))
            if p.returncode != 0 and not row["answer"]:
                row["error"] = p.stderr.strip()[:200]
        except Exception as e:  # timeout, bad json, missing claude
            row.update({k: 0 for k in NUM_FIELDS})
            row.update({"answer": "", "is_error": True, "error": f"{type(e).__name__}: {e}"[:200]})
        row["seconds"] = round(time.time() - t0, 1)
        if mode == "baseline":
            ok, blocked = check_search(row["answer"])
        else:
            ok, blocked = CHECKS[args.task](row["answer"])
        failed = row.pop("is_error", False)
        if failed and not row["error"]:
            row["error"] = row["answer"][:200]  # e.g. authentication error
        row["ok"] = int(ok and not failed)
        row["blocked"] = int(blocked)
        if mode == "codegen" and row["ok"]:  # keep the generated script
            s = tmp / "run.js"
            if s.exists():
                shutil.copy(s, RESULTS / f"run_{run}.js")
    return row


def cmd_run(args):
    if "codegen" in args.modes.split(",") and not playwright_paths():
        raise SystemExit("codegen needs the `playwright` library, and I cannot find it.\n"
                         f"Run this once, then try again:\n  cd {ROOT} && npm i playwright && npx playwright install chromium")
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "runs.csv"
    new = not out.exists()
    with out.open("a", newline="") as f:
        w = csv.DictWriter(f, RUN_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for mode in args.modes.split(","):
            n = 1 if mode == "baseline" else args.runs  # baseline is almost identical every time: once is enough
            for r in range(args.start, args.start + n):
                row = run_one(mode, r, args)
                w.writerow(row); f.flush()
                print(f"{mode:9} run {r}: ok={row['ok']} turns={row['turns']} "
                      f"tools={row['tool_calls']} tokens={row['total_tokens']:,} cost=${row['cost_usd']} {row['seconds']}s "
                      + (f"DENIED={row['denials']} " if row['denials'] else "") + row['error'])
    print(f"\nSaved {out}. Next: python3 bench.py report --task {args.task}")


def med(xs):
    return round(statistics.median(xs)) if xs else 0


def span(xs, fmt="{:,}"):
    return "-" if not xs else (fmt.format(xs[0]) if min(xs) == max(xs) else f"{fmt.format(min(xs))} to {fmt.format(max(xs))}")


def cmd_report(args):
    rows = list(csv.DictReader((RESULTS / "runs.csv").open()))
    lines = ["| Mode | Green runs | Agent turns | Tool calls | Tokens per run | Tokens per green run | Cost per green run | Seconds per run |",
             "|---|---|---|---|---|---|---|---|"]
    detail = ["| Mode | Median input | Median cache write | Median cache read | Median output |", "|---|---|---|---|---|"]
    for mode in ["baseline", "mcp", "cli", "codegen"]:
        rs = [r for r in rows if r["mode"] == mode]
        if not rs:
            continue
        green = [r for r in rs if r["ok"] == "1"]
        tot_tokens = sum(int(r["total_tokens"]) for r in rs)  # failed runs count too
        tot_cost = sum(float(r["cost_usd"]) for r in rs)
        if green:
            lines.append(
                f"| {mode} | {len(green)} of {len(rs)} | {span([int(r['turns']) for r in green])} | "
                f"{span([int(r.get('tool_calls') or 0) for r in green])} | "
                f"{span([int(r['total_tokens']) for r in green])} | {round(tot_tokens / len(green)):,} | "
                f"${tot_cost / len(green):.3f} | {span([float(r['seconds']) for r in green], '{:.1f}')} |")
        else:
            lines.append(f"| {mode} | 0 of {len(rs)} | - | - | - | n/a | n/a | - |")
        detail.append(f"| {mode} | " + " | ".join(f"{med([int(r[k]) for r in rs]):,}" for k in
                      ["input", "cache_write", "cache_read", "output"]) + " |")
    blocked = sum(int(r["blocked"]) for r in rows)
    denied = [f"{r['mode']} run {r['run']}" for r in rows if int(r.get("denials") or 0) > 0]
    text = ("\n".join(lines) + "\n\nRanges cover successful runs only. Agent turns = rounds with the model (one turn can hold several tool calls). Tokens per green run count failed runs too.\n\n"
            + "\n".join(detail) + f"\n\nRuns blocked by captcha/consent: {blocked}\n"
            + (f"NOTE, runs where a tool was denied (the agent had to work around it): {', '.join(denied)}\n" if denied else ""))
    (RESULTS / "summary.md").write_text(text)
    print(text)


def cmd_why(args):
    for r in csv.DictReader((RESULTS / "runs.csv").open()):
        if r["ok"] == "0":
            print(f"{r['mode']} run {r['run']} | turns={r['turns']} | tool calls={r.get('tool_calls', '?')} | tokens={int(r['total_tokens']):,} | denied tools={r.get('denials', '?')}")
            print("   ", (r["error"] or r["answer"])[:400], "\n")


def main():
    global RESULTS
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("run", cmd_run), ("report", cmd_report), ("why", cmd_why)]:
        p = sub.add_parser(name)
        p.add_argument("--task", default="search", choices=TASKS)
        if name == "run":
            p.add_argument("--runs", type=int, default=5)
            p.add_argument("--start", type=int, default=1, help="first run number (use 6 to add runs after 5 existing ones)")
            p.add_argument("--modes", default="baseline,mcp,cli,codegen")
            p.add_argument("--model", default="sonnet")
            p.add_argument("--site", default=None, help="start URL (default: automationexercise.com)")
            p.add_argument("--query", default="dress", help="only for --task search")
            p.add_argument("--max-turns", type=int, default=None)
            p.add_argument("--timeout", type=int, default=None)
        p.set_defaults(fn=fn)
    a = ap.parse_args()
    if a.cmd == "run":
        a.max_turns = a.max_turns or DEFAULT_TURNS[a.task]
        a.timeout = a.timeout or DEFAULT_TIMEOUT[a.task]
    RESULTS = ROOT / ("results" if a.task == "search" else f"results_{a.task}")
    a.fn(a)


if __name__ == "__main__":
    main()
