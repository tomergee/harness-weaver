# Examples

Task packs and committed example outputs land here as Tier 1 implementation
proceeds.

Planned structure:

```
examples/
├── tasks/                     # individual task JSON files
│   ├── discovery-mood-tense.json
│   ├── continue-watching-stranger-things.json
│   └── analytical-runtime-rating.json
├── packs/                     # collections of tasks
│   ├── discovery.json
│   ├── continue-watching.json
│   └── analytical.json
└── output/                    # committed sample runs
    ├── single-agent-basic-vs-with-sandbox.md
    └── trajectories/
        └── *.json
```

Committed outputs let readers see what the harness produces without setting
up an API key.
