# EPSILON — Autonomous AI Trading Agent

> **An AI trading agent built around deterministic risk controls, broker-side reconciliation, and fail-closed execution.**

EPSILON is an autonomous **paper-trading AI agent** that combines LLM-based market reasoning with deterministic Python risk controls and Alpaca's trading infrastructure.

The key idea is simple:

> **Let AI make a decision. Never let AI control execution risk.**

---

## 🚀 What We Built

EPSILON is designed as a production-oriented trading-agent architecture rather than a simple LLM trading bot.

It provides:

* AI-generated **BUY / SELL / HOLD** decisions
* Real Alpaca Paper Trading market/account data
* Featherless AI inference
* Deterministic position sizing
* Multi-layer risk validation
* Final execution gate
* Alpaca MCP integration
* Automatic order idempotency
* Ambiguous-order reconciliation
* PostgreSQL worker heartbeat and leader election
* JWT authentication and RBAC
* Audit and observability
* Fail-closed behavior

> **Trading mode is strictly limited to Alpaca Paper Trading. No live Alpaca trading path is implemented.**

---

# 🎯 The Problem

Most AI trading agents focus primarily on the intelligence layer:

```text
Market Data → LLM → BUY/SELL → Broker
```

The dangerous part is what happens **after the LLM makes the decision**.

An LLM can produce a reasonable trade decision, but it should not be trusted to:

* calculate unrestricted order size
* modify risk limits
* directly call the broker
* decide whether an ambiguous order was executed
* blindly retry failed orders

EPSILON separates **decision intelligence** from **execution authority**.

---

# 💡 Key Innovation: Order Reconciliation Guard

While auditing the **Alpaca MCP server source code**, we identified a reliability problem around ambiguous order-submission failures.

A network timeout can leave the system unable to immediately determine whether the broker received the order.

This creates the classic distributed-systems problem:

```text
AI requests order
       ↓
Alpaca MCP
       ↓
Network timeout
       ↓
Did Alpaca receive the order?
       ↓
      UNKNOWN
```

A naive AI agent could retry and potentially create a duplicate order.

## Our Solution

EPSILON introduces an **Order Reconciliation Guard** inside the Risk Guard Proxy.

```text
AI Decision
     ↓
Final Order Gate
     ↓
Order Reconciliation Guard
     ├── Generate deterministic client_order_id
     ├── Submit order
     ├── Detect ambiguous failure
     ├── Query Alpaca order state
     └── Reconcile before retry
     ↓
Alpaca MCP Server
     ↓
Alpaca Paper Trading
```

### 1. Deterministic Idempotency

EPSILON automatically generates a deterministic `client_order_id` for order attempts.

The AI does not need to remember to provide one.

This allows subsequent reconciliation to identify the original order attempt.

### 2. Active Reconciliation

When an order submission fails ambiguously, EPSILON does **not** simply return the uncertainty to the LLM.

The proxy queries Alpaca to determine the broker-side state before allowing a retry.

The execution state is resolved as:

* `FILLED`
* `NOT_PLACED`
* `PENDING`

This prevents the dangerous:

```text
UNKNOWN → RETRY → POSSIBLE DUPLICATE
```

flow.

Instead:

```text
AMBIGUOUS FAILURE
       ↓
RECONCILE WITH ALPACA
       ↓
VERIFY STATE
       ↓
CONTINUE OR STOP
```

### 3. Order-State Ledger

EPSILON records the lifecycle of order attempts so execution can be audited instead of inferred from an LLM response.

```text
ORDER_ATTEMPT
      ↓
SUBMITTED
      ↓
AMBIGUOUS_FAILURE
      ↓
RECONCILIATION
      ↓
FILLED / NOT_PLACED / PENDING
```

> **This is the core infrastructure contribution of EPSILON.**

We did not simply build an LLM that sends trades to Alpaca.

We inspected the underlying MCP execution path and built a protective reconciliation layer around it.

---

# 🧠 AI Decision Architecture

The LLM is responsible for **reasoning**, not execution.

```text
Real Market Data
      ↓
Technical Indicators
      ↓
AI Context Builder
      ↓
Featherless LLM
      ↓
Structured BUY / SELL / HOLD
      ↓
Schema Validation
      ↓
Deterministic Position Sizing
      ↓
Risk Rules
      ↓
Final Order Gate
      ↓
Order Reconciliation Guard
      ↓
Alpaca Paper Trading
```

The LLM can propose:

* symbol
* action
* confidence
* thesis
* entry reasoning

The LLM **cannot determine**:

* executable quantity
* maximum order size
* portfolio risk limits
* buying-power limits
* risk overrides
* direct broker execution

Those decisions remain deterministic Python controls.

---

# 🛡️ Multi-Layer Risk System

Every potential order passes through multiple independent controls.

## Layer 1 — Market Data Validation

EPSILON validates:

* symbol
* price
* OHLC relationships
* timestamps
* freshness
* data availability

Invalid or stale data causes the decision to fail closed.

## Layer 2 — AI Output Validation

The LLM response must conform to the expected structured schema.

Invalid, duplicated, unknown, or malformed decisions are rejected.

## Layer 3 — Deterministic Position Sizing

Order quantity is calculated by Python using configured limits and current market/account state.

The LLM cannot choose unrestricted quantities.

## Layer 4 — Risk Rules

The risk engine checks:

* approved watchlist
* order side
* crypto/options restrictions
* short-selling restrictions
* maximum orders per run
* daily loss limit
* maximum open positions
* maximum order notional
* maximum position notional
* buying power

## Layer 5 — Final Order Gate

Before submission, EPSILON verifies:

* paper trading mode
* kill switch
* symbol
* side
* quantity
* market/order type
* risk limits
* idempotency

## Layer 6 — Fresh Broker State

The Risk Guard Proxy obtains fresh account, position, and market-price information before forwarding the order.

## Layer 7 — Alpaca Reconciliation

If execution becomes ambiguous, EPSILON reconciles the order with Alpaca before allowing a retry.

---

# 🔒 Fail-Closed Design

EPSILON follows a simple safety principle:

> **If the system cannot prove that an action is safe, it does not trade.**

| Failure Condition           | EPSILON Behavior          |
| --------------------------- | ------------------------- |
| Invalid market data         | 🛑 Stop                   |
| Stale price                 | 🛑 Reject                 |
| Invalid LLM output          | 🛑 Stop                   |
| LLM unavailable             | 🛑 Stop                   |
| Alpaca unavailable          | 🛑 Stop                   |
| Database unavailable        | 🛑 Worker blocked         |
| Worker lease lost           | 🛑 Stop trading           |
| Risk violation              | 🛑 Reject                 |
| Kill switch enabled         | 🛑 Reject                 |
| Ambiguous order             | 🔄 Reconcile before retry |
| Paper configuration invalid | 🛑 Stop                   |
| Authentication failure      | 🛑 Reject                 |

---

# 🏗️ System Architecture

```text
                    ┌──────────────────────┐
                    │     EPSILON UI       │
                    │   React + Vercel     │
                    └──────────┬───────────┘
                               │ REST
                               ▼
                    ┌──────────────────────┐
                    │      FastAPI         │
                    │   API + Auth/RBAC    │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
       ┌────────────┐   ┌────────────┐   ┌──────────────┐
       │ PostgreSQL │   │  Worker    │   │ Observability│
       │   State    │   │ Scheduler  │   │   / Audit    │
       └────────────┘   └─────┬──────┘   └──────────────┘
                              │
                              ▼
                       ┌───────────────┐
                       │ Orchestrator  │
                       └───────┬───────┘
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
             ┌──────────────┐     ┌──────────────┐
             │ Featherless  │     │ Risk Guard   │
             │     AI       │     │    Proxy     │
             └──────────────┘     └──────┬───────┘
                                         │
                                         ▼
                                ┌────────────────┐
                                │ Alpaca MCP     │
                                │    Server      │
                                └───────┬────────┘
                                        │
                                        ▼
                                ┌────────────────┐
                                │ Alpaca Paper   │
                                │   Trading API  │
                                └────────────────┘
```

---

# ⚙️ Technology Stack

## AI / Agent

* Python
* Featherless AI
* OpenAI-compatible LLM interface
* Structured LLM output
* Deterministic decision validation

## Trading

* Alpaca Paper Trading API
* Alpaca MCP Server
* Model Context Protocol (MCP)
* Market data validation
* Order reconciliation
* Deterministic idempotency

## Backend

* FastAPI
* Python
* PostgreSQL
* SQLAlchemy
* Persistent worker
* Repository pattern

## Security

* JWT authentication
* RBAC
* Paper-only enforcement
* Kill switch
* Rate limiting
* Secret redaction
* Audit logging
* Fail-closed execution

## Frontend

* React
* TypeScript
* Vite
* REST API

## Testing / Quality

* Pytest
* Ruff
* Security-focused tests
* Failure-mode testing
* Integration-path testing

---

# 🔐 Security Architecture

EPSILON deliberately prevents the LLM from becoming a broker controller.

The LLM has **no direct Alpaca execution capability**.

```text
LLM
 │
 │ Decision only
 ▼
DecisionLoop
 │
 ▼
Risk Rules
 │
 ▼
Final Order Gate
 │
 ▼
Risk Guard Proxy
 │
 ▼
Alpaca
```

Broker credentials are kept outside the model context.

Paper trading is enforced through multiple independent layers.

Production authentication uses JWT/RBAC, while development-only authentication mechanisms cannot bypass production authentication.

---

# 🔬 Testing & Verification

The system was audited at source-code and runtime level.

## Automated Verification

```text
428 tests passed
Ruff: PASS
Frontend build: PASS
```

## Runtime Verification

We exercised:

* FastAPI `/health`
* FastAPI `/ready`
* PostgreSQL failure handling
* Worker lease failure
* JWT authentication
* RBAC
* Alpaca MCP execution path
* Alpaca Paper API boundary
* Featherless API boundary
* LLM failure handling
* market-data failure handling
* fail-closed execution

Invalid external credentials produced real `401 Unauthorized` responses, and the trading system correctly stopped without submitting an order.

---

# 🚧 Current Verification Status

EPSILON's architecture and safety controls are implemented and tested.

The final live E2E paper-trading verification requires valid:

* Alpaca Paper Trading credentials
* Featherless API credentials
* PostgreSQL credentials

The current invalid-credential tests demonstrated the intended behavior:

```text
External failure
      ↓
Dependency unavailable
      ↓
Risk system blocks execution
      ↓
NO ORDER
```

> **We do not claim a successful live paper order until the external credentials are regenerated and the controlled E2E test is completed.**

---

# 🧩 Engineering Issues Identified During Audit

Our source-code audit uncovered several important implementation gaps beyond the original trading-agent design.

### 1. PostgreSQL Persistence

PostgreSQL repositories existed but were not fully connected to the production decision/execution persistence path.

### 2. Database-Backed Idempotency

Database-backed idempotency existed but was not fully wired into the production order path.

### 3. Worker Lease Renewal

Worker lease renewal required stricter fail-closed handling.

### 4. Worker Startup

Worker startup required stricter production database enforcement.

### 5. Distributed State

Local JSONL state created problems for horizontally separated API/worker deployments.

### 6. Database Bootstrap

PostgreSQL schema initialization/migration needed a proper production bootstrap path.

### 7. SQL Integration Testing

Repository SQL required real database integration testing.

### 8. Broker Clock Validation

Alpaca market-session validation should use the authoritative broker clock at execution time.

> These findings drove the hardening work instead of being hidden behind the **"428 tests passed"** result.

---

# 🏆 Why EPSILON Is Different

Most trading-agent projects demonstrate:

```text
LLM → Trading API
```

EPSILON focuses on the harder problem:

> **How do you safely operate an autonomous AI agent when execution can become ambiguous?**

Our architecture separates four responsibilities:

## 🧠 Intelligence

AI decides **what it believes should happen**.

## 🛡️ Governance

Deterministic rules decide **whether it is allowed to happen**.

## ⚡ Execution

The broker layer determines **what actually happened**.

## 🔄 Reconciliation

EPSILON verifies broker state before an ambiguous action can be retried.

---

# 📊 Decision → Governance → Execution

```text
┌─────────────────────┐
│    AI INTELLIGENCE  │
│                     │
│  Market reasoning   │
│  BUY / SELL / HOLD  │
│  Confidence         │
│  Trading thesis     │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   DETERMINISTIC     │
│     GOVERNANCE      │
│                     │
│ Schema validation   │
│ Position sizing     │
│ Risk limits         │
│ Kill switch         │
│ Paper-only checks   │
│ Final order gate    │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│      EXECUTION      │
│                     │
│   Risk Guard Proxy  │
│   Alpaca MCP        │
│   Alpaca Paper API  │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│    RECONCILIATION   │
│                     │
│ Broker state        │
│ Order ledger        │
│ Idempotency         │
│ Retry protection    │
└─────────────────────┘
```

---

# 📌 Project Status

| Component       | Status                                     |
| --------------- | ------------------------------------------ |
| Trading Mode    | 🟢 Alpaca Paper Trading only               |
| AI              | 🟢 Featherless-compatible LLM provider     |
| Execution       | 🟢 Alpaca MCP + Risk Guard Proxy           |
| Risk            | 🟢 Multi-layer deterministic controls      |
| Authentication  | 🟢 JWT + RBAC                              |
| Persistence     | 🟡 PostgreSQL architecture + runtime state |
| Automated Tests | 🟢 428 tests passing                       |
| Linting         | 🟢 Ruff passing                            |
| Frontend Build  | 🟢 Passing                                 |
| Live Paper E2E  | 🟡 Pending valid external credentials      |

---

# 🧭 Core Principle

> **AI proposes. Deterministic controls decide. The broker executes. EPSILON reconciles reality.**

That is the foundation of a safer autonomous trading agent.

---

## ⚠️ Disclaimer

EPSILON is an experimental **paper-trading AI agent** and is not financial advice.

The system is designed for research, engineering, and hackathon purposes. It does not provide guarantees about trading performance, profitability, or future market behavior.

**No live trading path is included.**
