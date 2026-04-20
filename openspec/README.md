# OpenSpec — cube-harness

Machine-friendly specifications for coding agents. Each spec describes a capability:
its contract, invariants, and constraints. Read these before modifying code in the
corresponding layer.

## Structure

```
openspec/
├── specs/             # Living contracts, one dir per capability
│   ├── core/          # AgentOutput, Trajectory, TrajectoryStep, ActionSpace
│   ├── agent/         # Agent, AgentConfig
│   ├── tool/          # ToolWithTelemetry, AsyncToolWithTelemetry (telemetry wrapper over cube.tool)
│   ├── episode/       # Episode, EpisodeConfig, MAX_STEPS
│   ├── experiment/    # Experiment, ExpResult, resume/retry semantics
│   ├── storage/       # Storage Protocol, FileStorage V1/V2 layouts
│   ├── llm/           # LLM, LLMConfig, Prompt, LLMCall, Usage
│   ├── metrics/       # OpenTelemetry tracer, ADP export
│   ├── analyze/       # XRay viewer (Gradio)
│   └── mcp/           # MCP server integration
└── changes/           # Active proposals as delta specs
    └── archive/       # Completed changes, prefixed with date
```

Specs are terse. Cross-repo: this harness consumes **cube-standard**'s contracts
(Task, Benchmark, Tool, Resource). Always consult the cube-standard spec first when
your change touches the base protocol.

## Writing style

Each spec covers:
- **Purpose** — one sentence
- **Public API** — types, methods, signatures
- **Invariants** — what must always hold
- **Contracts** — what implementers must guarantee
- **Gotchas** — non-obvious constraints
