# concept-demos

Minimal, runnable demos that explain how AI agent systems actually work.

Each folder is one concept, one script, one README. Read the code top-to-bottom. Run it. Inspect the output. That's the whole point.

## Demos

| Folder | What it demonstrates | Runtime |
|---|---|---|
| [nano_team](nano_team/) | How AI agent teams communicate via filesystem mailboxes — and why all routing control is prompt-based, not enforced | ~3-5 min, requires Claude API |

## Philosophy

- **Runnable over readable**: every demo produces real output you can inspect
- **Minimal**: one script per concept, no frameworks, no abstractions
- **Educational comments**: code comments explain WHY, not WHAT
- **Faithful**: demos mirror how real systems work, not simplified toy versions

## Adding a demo

```
concept-demos/
├── your_concept/
│   ├── README.md       # what it demonstrates, how to run, what to look for
│   ├── your_script.py  # the demo
│   └── pyproject.toml  # dependencies (if needed)
└── ...
```
