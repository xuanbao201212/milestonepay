# -*- coding: utf-8 -*-
"""MilestonePay direct-mode tests — staged escrow with real custody and staged payouts.

Run: pytest tests/direct/ -v
"""


def _deploy(direct_deploy):
    import pathlib
    import tempfile

    src = pathlib.Path(__file__).resolve().parents[2] / "contracts" / "milestone_pay.py"
    code = src.read_text(encoding="utf-8")
    tmp = pathlib.Path(tempfile.gettempdir()) / "milestone_pay_local.py"
    tmp.write_text(code, encoding="utf-8")
    return direct_deploy(str(tmp))


def _addr(v):
    from genlayer import Address
    return Address(v)


def _hex(v) -> str:
    if isinstance(v, str):
        return v.lower().replace("0x", "")
    for attr in ("as_bytes", "_as_bytes"):
        if hasattr(v, attr):
            b = getattr(v, attr)
            if callable(b):
                b = b()
            return b.hex()
    return bytes(v).hex()


def _project(direct_vm, contract, client, contractor, budget=1000, title="Build an oracle"):
    direct_vm.sender = client
    direct_vm.value = budget
    pid = contract.create_project(_addr(contractor), title)
    direct_vm.value = 0
    return pid


def _add_ms(direct_vm, contract, client, pid, title="Stage 1", crit="Must include real code", amount=400):
    direct_vm.sender = client
    contract.add_milestone(pid, title, crit, amount)


def _submit(direct_vm, contract, contractor, pid, index=0, url="https://example.com/work"):
    direct_vm.sender = contractor
    contract.submit_milestone(pid, index, url)


# ------------------------------------------------- 1. budget custody ----

def test_create_requires_budget(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with direct_vm.expect_revert("Budget must be funded"):
        contract.create_project(_addr(direct_bob), "unfunded")
    direct_vm.value = 0


def test_budget_is_custody_and_roles_differ(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _project(direct_vm, contract, direct_alice, direct_bob, budget=1500)
    p = contract.get_project(pid)
    assert int(p["total"]) == 1500          # recorded from gl.message.value
    assert int(p["released"]) == 0
    assert int(p["remaining"]) == 1500
    assert int(p["state"]) == 0
    assert _hex(p["client"]) == direct_alice.hex()
    assert _hex(p["contractor"]) == direct_bob.hex()

    direct_vm.sender = direct_alice
    direct_vm.value = 500
    with direct_vm.expect_revert("Client and contractor must differ"):
        contract.create_project(_addr(direct_alice), "self project")
    direct_vm.value = 0


# ------------------------------------------------- 2. milestone setup ----

def test_only_client_defines_milestones(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _project(direct_vm, contract, direct_alice, direct_bob, budget=1000)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the client can define milestones"):
        contract.add_milestone(pid, "Stage", "criteria", 100)

    _add_ms(direct_vm, contract, direct_alice, pid, amount=400)
    p = contract.get_project(pid)
    assert int(p["milestone_count"]) == 1
    assert int(p["declared"]) == 400

    m = contract.get_milestone(pid, 0)
    assert int(m["amount"]) == 400
    assert int(m["state"]) == 0


def test_milestone_amounts_cannot_exceed_budget(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _project(direct_vm, contract, direct_alice, direct_bob, budget=1000)
    _add_ms(direct_vm, contract, direct_alice, pid, amount=700)

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Milestone amounts exceed the escrowed budget"):
        contract.add_milestone(pid, "Stage 2", "criteria", 400)

    with direct_vm.expect_revert("Milestone amount must be positive"):
        contract.add_milestone(pid, "Stage 2", "criteria", 0)


# ------------------------------------------------- 3. submission gate ----

def test_only_contractor_submits(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _project(direct_vm, contract, direct_alice, direct_bob)
    _add_ms(direct_vm, contract, direct_alice, pid)

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Only the contractor can submit deliverables"):
        contract.submit_milestone(pid, 0, "https://example.com/x")

    _submit(direct_vm, contract, direct_bob, pid)
    assert int(contract.get_milestone(pid, 0)["state"]) == 1


def test_review_requires_submission(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _project(direct_vm, contract, direct_alice, direct_bob)
    _add_ms(direct_vm, contract, direct_alice, pid)
    with direct_vm.expect_revert("Milestone has no pending submission"):
        contract.review_milestone(pid, 0)


# ------------------------------------------------- 4. judging + payouts ----

def test_approved_milestone_releases_funds(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _project(direct_vm, contract, direct_alice, direct_bob, budget=1000)
    _add_ms(direct_vm, contract, direct_alice, pid, amount=400)
    _submit(direct_vm, contract, direct_bob, pid)

    direct_vm.mock_web(r".*example\.com/work.*",
                       {"status": 200, "body": "<html>import genlayer - real contract code here</html>"})
    direct_vm.mock_llm(r".*", {"approved": True,
                               "reason": "The fetched deliverable contains real GenLayer contract code as required.",
                               "confidence": "high"})
    contract.review_milestone(pid, 0)

    m = contract.get_milestone(pid, 0)
    assert int(m["state"]) == 2             # APPROVED
    assert "code" in m["verdict_reason"].lower()

    # only the client can release
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the client can release funds"):
        contract.release_milestone(pid, 0)

    direct_vm.sender = direct_alice
    contract.release_milestone(pid, 0)

    m = contract.get_milestone(pid, 0)
    p = contract.get_project(pid)
    assert m["released"] is True
    assert int(p["released"]) == 400
    assert int(p["remaining"]) == 600
    assert int(p["approved_count"]) == 1

    with direct_vm.expect_revert("Milestone already paid out"):
        contract.release_milestone(pid, 0)


def test_rejected_milestone_can_be_resubmitted(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _project(direct_vm, contract, direct_alice, direct_bob, budget=800)
    _add_ms(direct_vm, contract, direct_alice, pid, amount=300)
    _submit(direct_vm, contract, direct_bob, pid)

    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>nothing here</html>"})
    direct_vm.mock_llm(r".*", {"approved": False,
                               "reason": "The deliverable is an empty page and does not meet the criteria.",
                               "confidence": "high"})
    contract.review_milestone(pid, 0)
    assert int(contract.get_milestone(pid, 0)["state"]) == 3      # REJECTED

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Milestone is not approved"):
        contract.release_milestone(pid, 0)

    # contractor can re-submit after a rejection (goes back to SUBMITTED)
    _submit(direct_vm, contract, direct_bob, pid, url="https://example.com/real-work")
    assert int(contract.get_milestone(pid, 0)["state"]) == 1
    assert int(contract.get_project(pid)["released"]) == 0   # nothing released while pending


def test_full_project_completes(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _project(direct_vm, contract, direct_alice, direct_bob, budget=1000)
    _add_ms(direct_vm, contract, direct_alice, pid, title="Stage 1", amount=400)
    _add_ms(direct_vm, contract, direct_alice, pid, title="Stage 2", amount=600)

    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>delivered</html>"})
    direct_vm.mock_llm(r".*", {"approved": True, "reason": "Deliverable satisfies the criteria.",
                               "confidence": "high"})

    _submit(direct_vm, contract, direct_bob, pid, index=0)
    contract.review_milestone(pid, 0)
    direct_vm.sender = direct_alice
    contract.release_milestone(pid, 0)

    _submit(direct_vm, contract, direct_bob, pid, index=1)
    contract.review_milestone(pid, 1)
    direct_vm.sender = direct_alice
    contract.release_milestone(pid, 1)

    # releasing does not auto-complete the project (leftover budget must stay recoverable)
    p = contract.get_project(pid)
    assert int(p["released"]) == 1000
    assert int(p["remaining"]) == 0
    assert int(p["state"]) == 0

    # the client explicitly completes it; nothing is left behind
    contract.complete_project(pid)
    p = contract.get_project(pid)
    assert int(p["state"]) == 1             # COMPLETED
    assert p["paid_out"] is True
    assert int(p["remaining"]) == 0


# ------------------------------- 6. leftover budget is never trapped ----

def test_leftover_recoverable_when_budget_exceeds_milestones(direct_vm, direct_deploy, direct_alice, direct_bob):
    """The reviewer's case: fund 1000, declare only 600, release it all, and the rest must come back."""
    contract = _deploy(direct_deploy)
    pid = _project(direct_vm, contract, direct_alice, direct_bob, budget=1000)
    _add_ms(direct_vm, contract, direct_alice, pid, title="Stage 1", amount=400)
    _add_ms(direct_vm, contract, direct_alice, pid, title="Stage 2", amount=200)
    p = contract.get_project(pid)
    assert int(p["total"]) == 1000
    assert int(p["declared"]) == 600          # 400 GEN never allocated

    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>delivered</html>"})
    direct_vm.mock_llm(r".*", {"approved": True, "reason": "Meets the criteria.", "confidence": "high"})

    _submit(direct_vm, contract, direct_bob, pid, index=0)
    contract.review_milestone(pid, 0)
    direct_vm.sender = direct_alice
    contract.release_milestone(pid, 0)

    _submit(direct_vm, contract, direct_bob, pid, index=1)
    contract.review_milestone(pid, 1)
    direct_vm.sender = direct_alice
    contract.release_milestone(pid, 1)

    # every declared milestone is released, but 400 GEN of budget is still under custody
    p = contract.get_project(pid)
    assert int(p["released"]) == 600
    assert int(p["remaining"]) == 400
    assert int(p["state"]) == 0                # NOT trapped in a terminal state

    # the client closes the project and the leftover budget is returned
    contract.complete_project(pid)
    p = contract.get_project(pid)
    assert int(p["state"]) == 1                # COMPLETED
    assert p["paid_out"] is True
    assert int(p["total"]) == 600              # escrow base shrunk to what was paid out
    assert int(p["remaining"]) == 0            # nothing stranded


def test_withdraw_unallocated_keeps_project_active(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Unallocated budget can be pulled out early without ending the project."""
    contract = _deploy(direct_deploy)
    pid = _project(direct_vm, contract, direct_alice, direct_bob, budget=1000)
    _add_ms(direct_vm, contract, direct_alice, pid, amount=300)

    # a non-client cannot withdraw
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the client can withdraw"):
        contract.withdraw_unallocated(pid)

    direct_vm.sender = direct_alice
    contract.withdraw_unallocated(pid)
    p = contract.get_project(pid)
    assert int(p["total"]) == 300              # escrow base now matches what is allocated
    assert int(p["declared"]) == 300
    assert int(p["remaining"]) == 300
    assert int(p["state"]) == 0                # still ACTIVE

    # the freed capacity is gone: no unallocated budget left to withdraw
    with direct_vm.expect_revert("No unallocated budget to withdraw"):
        contract.withdraw_unallocated(pid)

    # and the milestone can still be delivered and paid as normal
    _submit(direct_vm, contract, direct_bob, pid, index=0)
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>work</html>"})
    direct_vm.mock_llm(r".*", {"approved": True, "reason": "Meets criteria.", "confidence": "high"})
    contract.review_milestone(pid, 0)
    direct_vm.sender = direct_alice
    contract.release_milestone(pid, 0)
    p = contract.get_project(pid)
    assert int(p["released"]) == 300
    assert int(p["remaining"]) == 0


def test_complete_requires_all_declared_released(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _project(direct_vm, contract, direct_alice, direct_bob, budget=500)
    _add_ms(direct_vm, contract, direct_alice, pid, amount=200)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the client can complete"):
        contract.complete_project(pid)

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Not all declared milestones have been released"):
        contract.complete_project(pid)


# ------------------------------------------------- 5. termination ----

def test_terminate_refunds_remaining_to_client(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    pid = _project(direct_vm, contract, direct_alice, direct_bob, budget=1000)
    _add_ms(direct_vm, contract, direct_alice, pid, amount=400)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the client can terminate"):
        contract.terminate_project(pid)

    direct_vm.sender = direct_alice
    contract.terminate_project(pid)
    p = contract.get_project(pid)
    assert int(p["state"]) == 2             # TERMINATED
    assert p["paid_out"] is True
    assert int(p["remaining"]) == 0         # nothing left under custody

    with direct_vm.expect_revert("Project is not active"):
        contract.terminate_project(pid)
