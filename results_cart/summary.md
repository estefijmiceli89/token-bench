| Mode | Green runs | Agent turns | Tool calls | Tokens per run | Tokens per green run | Cost per green run | Seconds per run |
|---|---|---|---|---|---|---|---|
| baseline | 1 of 1 | 1 | 0 | 22,624 | 22,624 | $0.017 | 3.2 |
| mcp | 10 of 10 | 4 to 6 | 3 to 5 | 101,248 to 156,748 | 129,479 | $0.057 | 19.0 to 35.8 |
| cli | 9 of 10 | 15 to 19 | 14 to 18 | 416,426 to 616,926 | 514,859 | $0.126 | 50.4 to 80.0 |
| codegen | 9 of 10 | 3 to 5 | 2 to 4 | 49,405 to 76,066 | 77,875 | $0.057 | 20.1 to 39.2 |

Ranges cover successful runs only. Agent turns = rounds with the model (one turn can hold several tool calls). Tokens per green run count failed runs too.

| Mode | Median input | Median cache write | Median cache read | Median output |
|---|---|---|---|---|
| baseline | 2 | 3,644 | 18,962 | 16 |
| mcp | 12 | 8,444 | 119,898 | 1,083 |
| cli | 36 | 11,868 | 482,166 | 1,936 |
| codegen | 6 | 6,544 | 66,596 | 1,706 |

Runs blocked by captcha/consent: 0
NOTE, runs where a tool was denied (the agent had to work around it): cli run 4, codegen run 3
