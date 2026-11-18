# token-bench

Small benchmark: how much does an AI agent cost to drive a browser with **Playwright MCP**, **Playwright CLI**, or by **writing a Playwright test script**?

Used for the article "Playwright CLI was supposed to be the cheap one. In my test it cost 2.2x more."

## What it does

Runs the same job on https://automationexercise.com (a QA practice shop) through Claude Code in headless mode, and records tokens, cost, tool calls, time and failures per run.

- `cart` task (about 20 actions): search "dress", add 3; search "top", add 1; search "jeans", add 2; remove the top; read the cart and report each line and the total.
- A script checks the answer (quantities, price x quantity, total). The model does not grade itself.
- Modes: `baseline` (no browser), `mcp`, `cli`, `codegen`.
- Metric: cost per green run = total spend of all runs (failed ones included) / number of successful runs.

## Run it

```
# once, for codegen mode
npm i playwright && npx playwright install chromium

python3 bench.py run --task cart --modes baseline,mcp,cli,codegen --runs 10
python3 bench.py run --task cart --modes codegen --runs 5 --start 11   # add more runs later
python3 bench.py report --task cart
python3 bench.py why --task cart      # why did failed runs fail?
```

Needs the `claude` CLI (Claude Code) logged in. Raw output goes to `results_cart/`.

## Known limits

- One site, one flow, one model (Sonnet). Small samples.
- In `cli` mode the agent gets a short command cheat sheet, not the official skill.
- Runs where the test setup denied a command to the agent are counted as failures (the report lists them as DENIED).
- Nothing is optimized (no prompt tuning, no snapshot reduction).
