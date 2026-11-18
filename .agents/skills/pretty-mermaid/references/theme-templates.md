# Pretty Mermaid Theme Templates

## 1. Sequence Diagram Header (`sequenceDiagram`)

```mermaid
%%{init: {
  'theme': 'base',
  'themeVariables': {
    'primaryColor': '#0f172a',
    'primaryTextColor': '#f8fafc',
    'primaryBorderColor': '#38bdf8',
    'lineColor': '#64748b',
    'secondaryColor': '#1e293b',
    'tertiaryColor': '#020617',
    'noteBkgColor': '#fef3c7',
    'noteTextColor': '#92400e',
    'noteBorderColor': '#f59e0b',
    'actorBkg': '#0f172a',
    'actorTextColor': '#38bdf8',
    'actorBorder': '#38bdf8',
    'signalColor': '#334155',
    'signalTextColor': '#0f172a'
  }
}}%%
sequenceDiagram
    autonumber
```

## 2. Flowchart Class Definitions (`flowchart LR` / `flowchart TD`)

```mermaid
flowchart LR
    classDef fe fill:#0f172a,stroke:#38bdf8,stroke-width:2px,color:#f8fafc
    classDef bff fill:#1e293b,stroke:#818cf8,stroke-width:2px,color:#f8fafc
    classDef agent fill:#064e3b,stroke:#34d399,stroke-width:2px,color:#f8fafc
    classDef data fill:#312e81,stroke:#a78bfa,stroke-width:2px,color:#f8fafc
    classDef warn fill:#7f1d1d,stroke:#f87171,stroke-width:2px,color:#fef2f2
```
