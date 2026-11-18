---
name: fe-product-ai-architect
description: >-
  Use this skill when designing, mocking up, scaffolding, or building features for the FE/
  JavaScript/TypeScript frontend and its FastAPI + LangGraph AI backend bridge. Guides a
  mockup-first Product Designer workflow followed by modular Full-Stack AI engineering.
---

# Product Designer & Full-Stack AI Architect Workflow

Follow this 4-stage workflow whenever creating or extending features in `FE/` and its BFF bridge:

## Stage 1: Product Discovery & UX Mockup (Designer First)
1. Map the operator's job-to-be-done (e.g., investigating an `IOT_TEMP_VAL_C > 4.0°C` excursion or auditing `FDE_VIEWS.AgentAuditLog`).
2. Produce an interactive visual mockup (using the `generative_ui` skill or a self-contained HTML prototype in `FE/design-mockups/`) showing layout grid, interactive streaming states, and design tokens.
3. Validate the mockup with the user before writing production module code.

## Stage 2: Full-Stack AI Contract Design (BFF & Streaming)
1. Align SSE events, session management, audit logging, and temporal tooling (`Past / Present / Future`) between `FE/src/shared/api/` and `BE/api/server.py`.
2. Read [architecture-contracts.md](./references/architecture-contracts.md) for endpoint contracts, FastAPI lifespan patterns, temporal tool schemas, and service startup commands.

## Stage 3: Modular Frontend Implementation
1. Build pure, isolated UI components in `FE/src/shared/ui/` and domain modules in `FE/src/features/<domain>/`.
2. Keep business logic inside custom hooks/controllers (`useDispatchStream`, `useAuditLogs`, `useDispatchSession`) decoupled from view templates so they can be unit-tested independently.

## Stage 4: Verification & Reliability Checks
1. Run lint, typecheck, and unit tests (`npm test` / `vitest` and `pytest` / `python -m unittest`).
2. Verify offline/degraded behavior when the FastAPI backend or MSSQL/Pinecone services return sentinel warnings.
