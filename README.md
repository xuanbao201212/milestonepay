# MilestonePay — Staged Escrow Reviewed On-chain

> Escrow the whole budget once. Release it **stage by stage** as an LLM jury reads each
> deliverable on-chain against that milestone's own acceptance criteria.

**Category:** Marketplace Escrow / Performance Contracts · Agentic Commerce

---

## What changed after review (v2)

A reviewer flagged a real lifecycle hole: if the escrowed budget was **larger** than the sum of
the declared milestones, releasing every declared milestone used to mark the project COMPLETED —
after which `terminate_project()` no longer applied, **stranding the leftover GEN in the
contract**. v2 makes every funded unit recoverable:

| Fix | Detail |
|---|---|
| **No auto-complete on release** | `release_milestone()` no longer flips the project to a terminal state, so leftover budget stays reachable. |
| **`withdraw_unallocated(project_id)`** | The client pulls back budget that was never allocated to a milestone. The project stays ACTIVE and further milestones can still be defined against the remaining budget. |
| **`complete_project(project_id)`** | The client closes the project once all *declared* milestones are released; the leftover (never-allocated, or declared-but-unreleased) is **paid back to the client** in the same transaction. A completed project can never strand value. |
| **Regression test** | `test_leftover_recoverable_when_budget_exceeds_milestones` funds 1000, declares only 600, releases all 600, and asserts the remaining 400 is returned without the project becoming terminal. `test_withdraw_unallocated_keeps_project_active` covers the early-withdrawal path. |

---

## Why GenLayer

Staged payment is how real work gets funded — but someone still has to decide whether a stage was
actually delivered. Traditionally that's a human reviewer (slow, and a trusted party) or an oracle
(which cannot read a spec and judge it). MilestonePay puts the stage review in the contract:
the client writes each milestone's acceptance criteria in plain language, and at review time the
contract fetches the contractor's deliverable and has an LLM jury rule approved / rejected through
validator consensus.

The money story is complete in both directions: the budget is escrowed up-front, each approved
stage releases its own amount, and anything unreleased returns to the client on termination.

---

## Money flow

```
client --budget(payable)--> [ MilestonePay ]  (full budget under custody)
                                   |
              contractor submits deliverable for milestone N
                                   |
                review_milestone() reads it on-chain, jury rules
                                   |
        approved --> release_milestone() pays that stage's GEN to the contractor
        rejected --> nothing moves; the stage can be resubmitted
                                   |
        terminate_project() --> everything unreleased returns to the client
```

| Actor | Action | Money |
|---|---|---|
| client | `create_project` (**payable**) | budget enters the contract |
| client | `add_milestone(title, criteria, amount)` | defines staged payouts (sum ≤ budget) |
| contractor | `submit_milestone(index, url)` | — |
| anyone (client pays gas) | `review_milestone(index)` | jury reads the URL on-chain |
| client | `release_milestone(index)` | that stage's GEN goes to the contractor |
| client | `terminate_project()` | remaining budget returns to the client |

All outflows use `emit_transfer`; `get_contract_balance()` exposes live custody.

---

## Lifecycle

```
ACTIVE --(all declared milestones released)--> COMPLETED
   └----terminate_project----> TERMINATED (remaining refunded to the client)

per milestone: PENDING -> SUBMITTED -> APPROVED (releasable) | REJECTED (resubmittable)
```

---

## Contract: `contracts/milestone_pay.py`

| Method | Type | Description |
|---|---|---|
| `create_project(contractor, title)` | **payable** write | Escrows the budget |
| `add_milestone(project_id, title, criteria, amount)` | write | Client defines a stage (+ its payout) |
| `submit_milestone(project_id, index, url)` | write | Contractor delivers |
| `review_milestone(project_id, index)` | write | LLM reads the deliverable and rules |
| `release_milestone(project_id, index)` | write | Client releases an approved stage |
| `terminate_project(project_id)` | write | Client stops and refunds the remainder |
| `withdraw_unallocated(project_id)` | write | Client pulls back never-allocated budget (project stays ACTIVE) |
| `complete_project(project_id)` | write | Client closes it; leftover budget is returned in the same call |
| `get_project` / `get_milestone` / `get_project_count` / `get_contract_balance` | view | State |

### Review prompt (the important part)

```
MILESTONE: <title>
ACCEPTANCE CRITERIA (authoritative): <criteria>
PAYOUT IF APPROVED: <amount> wei
DELIVERABLE URL: <url>
CONTENT FETCHED FROM THE DELIVERABLE RIGHT NOW: <page>
Decide whether the deliverable satisfies the criteria, judging ONLY on the fetched content.
Respond: {"approved": bool, "reason": "...", "confidence": "..."}
```

Validators re-run the same reasoning and must agree on the boolean before the milestone state
changes. A rejected stage stays resubmittable, so a contractor is never permanently blocked.

### Payout path

```python
@gl.evm.contract_interface
class _Payee:
    class View: pass
    class Write: pass

def _pay(self, to, amount):
    _Payee(to).emit_transfer(value=amount)   # external message → real GEN out
```

---

## Frontend: `index.html`

Single-file, build-free (GenLayerJS from esm.sh), GitHub Pages ready. Fifth house style in the
series (deep green/teal with gold accents): a **budget-custody readout**, project cards with a
release progress bar, and a per-milestone list showing state, payout, deliverable link and the
jury's cited verdict.

---

## Local tests (13 passing)

```bash
pip install genlayer-test pytest
pytest tests/direct/ -v
```

Covers: budget required to create a project, budget recorded as custody, client≠contractor,
client-only milestone definition, milestone amounts cannot exceed the budget, contractor-only
submission, review-requires-submission, the approved→release payout path, rejection +
resubmission, explicit completion, termination refunding the remainder, **and the reviewer's
case: a budget larger than the declared milestones stays fully recoverable (both via
`complete_project` and via early `withdraw_unallocated`)**.

---

## Deploy

```bash
npm install -g genlayer
genlayer network set testnet-bradbury
genlayer deploy --contract contracts/milestone_pay.py
genlayer call <CONTRACT> get_contract_balance
```

> **Header rule:** line 1 must be the runner directive
> `# { "Depends": "py-genlayer:<hash>" }` — `test`/`latest` are rejected outside debug mode.

---

## Verified end-to-end on Bradbury

Client escrows 0.5 GEN across two stages (0.2 + 0.3).

| Step | Result |
|---|---|
| `create_project` payable (0.5 GEN) | contract balance 0 → 0.5 GEN (budget under custody) |
| `add_milestone` ×2 | declared 0.5 GEN, split 0.2 / 0.3 |
| `submit_milestone` + `review_milestone` | ACCEPTED · jury read the deliverable on-chain |
| `release_milestone` | ACCEPTED · 0.2 GEN emitted to the contractor |
| `terminate_project` | ACCEPTED · remaining 0.3 GEN refunded to the client |
| final state | TERMINATED, released 0.2, remaining 0 |

```
milestonepay/
├── contracts/milestone_pay.py           the Intelligent Contract
├── tests/direct/test_milestone_pay.py   10 direct-mode tests
├── index.html                           build-free frontend (GitHub Pages ready)
├── genlayer.config.yaml
└── README.md
```

Built on [GenLayer](https://docs.genlayer.com) Intelligent Contracts.
