# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
from dataclasses import dataclass
from genlayer import *

# ---------------------------------------------------------------- storage ---


@allow_storage
@dataclass
class Project:
    client: Address          # pays the total up-front
    contractor: Address      # delivers the milestones
    title: str
    total: u256              # GEN actually held by this contract for the project
    released: u256           # amount already paid out to the contractor
    declared: u256           # sum of milestone amounts defined so far
    milestone_count: u256
    approved_count: u256
    state: u8                # 0=ACTIVE 1=COMPLETED 2=TERMINATED
    paid_out: bool


@allow_storage
@dataclass
class Milestone:
    title: str
    criteria: str            # plain-language acceptance criteria for this stage
    amount: u256             # GEN released when this stage is approved
    deliverable_url: str
    state: u8                # 0=PENDING 1=SUBMITTED 2=APPROVED 3=REJECTED
    verdict_reason: str
    confidence: str
    released: bool


# external message interface for paying an EOA / EVM address on the chain layer
@gl.evm.contract_interface
class _Payee:
    class View:
        pass

    class Write:
        pass


# ------------------------------------------------------------------ contract --

class MilestonePay(gl.Contract):
    """Staged escrow for agent-delivered work.

    The client escrows the whole budget up-front (payable). The project is then paid out stage by
    stage: the contractor submits a deliverable for a milestone, an LLM jury reads that
    deliverable on-chain against the milestone's acceptance criteria, and on approval the client
    releases that milestone's amount to the contractor. Anything not yet released stays under
    contract custody until termination, when it returns to the client.

    Money is real in both directions: the budget is custody (gl.message.value), releases and
    refunds leave through emit_transfer.
    """

    projects: TreeMap[u256, Project]
    milestones: TreeMap[str, Milestone]      # key: "<project_id>:<index>"
    next_id: u256

    def __init__(self):
        self.next_id = u256(1)

    # -------------------------------------------------------- internals ----

    def _to_addr(self, value) -> Address:
        if isinstance(value, Address):
            return value
        return Address(value)

    def _addr_bytes(self, value) -> bytes:
        b = getattr(value, "as_bytes", None)
        if b is None:
            b = getattr(value, "_as_bytes", None)
        if callable(b):
            b = b()
        if isinstance(b, (bytes, bytearray)):
            return bytes(b)
        return bytes(self._to_addr(value))

    def _ms_key(self, project_id: u256, index: u256) -> str:
        return str(int(project_id)) + ":" + str(int(index))

    def _ms(self, project_id: u256, index: u256):
        k = self._ms_key(project_id, index)
        if k not in self.milestones:
            raise gl.vm.UserError("Milestone not found")
        return self.milestones[k]

    def _pay(self, to: Address, amount: u256):
        """Send GEN out of this contract (external message, executed on finalization).

        The explicit ``on='finalized'`` matches the documented form for value transfers;
        without it the message may not be emitted at all.
        """
        if amount <= u256(0):
            return
        _Payee(to).emit_transfer(value=u256(amount), on='finalized')

    # ------------------------------------------------------------- views ----

    @gl.public.view
    def get_project(self, project_id: u256) -> dict:
        p = self.projects[project_id]
        return {
            "id": project_id,
            "client": "0x" + self._addr_bytes(p.client).hex(),
            "contractor": "0x" + self._addr_bytes(p.contractor).hex(),
            "title": p.title,
            "total": p.total,
            "released": p.released,
            "declared": p.declared,
            "remaining": p.total - p.released,
            "milestone_count": p.milestone_count,
            "approved_count": p.approved_count,
            "state": p.state,
            "paid_out": p.paid_out,
            "contract_balance": self.balance,
        }

    @gl.public.view
    def get_milestone(self, project_id: u256, index: u256) -> dict:
        m = self._ms(project_id, index)
        return {
            "title": m.title,
            "criteria": m.criteria,
            "amount": m.amount,
            "deliverable_url": m.deliverable_url,
            "state": m.state,
            "verdict_reason": m.verdict_reason,
            "confidence": m.confidence,
            "released": m.released,
        }

    @gl.public.view
    def get_project_count(self) -> u256:
        return self.next_id - u256(1)

    @gl.public.view
    def get_contract_balance(self) -> u256:
        return self.balance

    # ------------------------------------------------------------ writes ----

    @gl.public.write.payable
    def create_project(self, contractor: Address, title: str) -> u256:
        """Client escrows the whole budget (payable) and opens the project."""
        value = gl.message.value
        if value <= u256(0):
            raise gl.vm.UserError("Budget must be funded: send GEN with the call")
        if len(title.strip()) == 0:
            raise gl.vm.UserError("Title is required")

        client = self._to_addr(gl.message.sender_address)
        ct = self._to_addr(contractor)
        if self._addr_bytes(client) == self._addr_bytes(ct):
            raise gl.vm.UserError("Client and contractor must differ")

        p = Project(
            client=client,
            contractor=ct,
            title=title,
            total=value,
            released=u256(0),
            declared=u256(0),
            milestone_count=u256(0),
            approved_count=u256(0),
            state=u8(0),
            paid_out=False,
        )
        pid = self.next_id
        self.projects[pid] = p
        self.next_id = pid + u256(1)
        return pid

    @gl.public.write
    def add_milestone(self, project_id: u256, title: str, criteria: str, amount: u256):
        """Client defines a stage with its own acceptance criteria and payout amount."""
        if project_id not in self.projects:
            raise gl.vm.UserError("Project not found")
        p = self.projects[project_id]
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(p.client):
            raise gl.vm.UserError("Only the client can define milestones")
        if p.state != u8(0):
            raise gl.vm.UserError("Project is not active")
        if len(title.strip()) == 0:
            raise gl.vm.UserError("Milestone title is required")
        if len(criteria.strip()) == 0:
            raise gl.vm.UserError("Milestone criteria are required")
        if amount <= u256(0):
            raise gl.vm.UserError("Milestone amount must be positive")
        if int(p.declared) + int(amount) > int(p.total):
            raise gl.vm.UserError("Milestone amounts exceed the escrowed budget")

        idx = p.milestone_count
        self.milestones[self._ms_key(project_id, idx)] = Milestone(
            title=title,
            criteria=criteria,
            amount=amount,
            deliverable_url="",
            state=u8(0),
            verdict_reason="",
            confidence="",
            released=False,
        )
        p.milestone_count = idx + u256(1)
        p.declared = p.declared + amount

    @gl.public.write
    def submit_milestone(self, project_id: u256, index: u256, deliverable_url: str):
        """Contractor submits the deliverable for a stage."""
        if project_id not in self.projects:
            raise gl.vm.UserError("Project not found")
        if len(deliverable_url.strip()) == 0 or not (
                deliverable_url.startswith("http://") or deliverable_url.startswith("https://")):
            raise gl.vm.UserError("A valid deliverable URL is required")

        p = self.projects[project_id]
        if p.state != u8(0):
            raise gl.vm.UserError("Project is not active")
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(p.contractor):
            raise gl.vm.UserError("Only the contractor can submit deliverables")

        m = self._ms(project_id, index)
        if m.state != u8(0) and m.state != u8(3):
            raise gl.vm.UserError("Milestone is not open for submission")

        m.deliverable_url = deliverable_url
        m.state = u8(1)
        m.verdict_reason = ""
        m.confidence = ""

    @gl.public.write
    def review_milestone(self, project_id: u256, index: u256):
        """LLM jury reads the deliverable on-chain and rules against this stage's criteria."""
        if project_id not in self.projects:
            raise gl.vm.UserError("Project not found")
        m = self._ms(project_id, index)
        if m.state != u8(1):
            raise gl.vm.UserError("Milestone has no pending submission")

        # Snapshot into plain Python values outside the nondet block.
        title_mem = str(m.title)
        crit_mem = str(m.criteria)
        url_mem = str(m.deliverable_url)
        amount_mem = int(m.amount)

        def _fetch_text(url: str, limit: int = 900) -> str:
            try:
                resp = gl.nondet.web.get(url)
                if isinstance(resp, bytes):
                    raw = resp.decode("utf-8", errors="ignore")
                elif hasattr(resp, "body"):
                    raw = resp.body.decode("utf-8", errors="ignore")
                else:
                    raw = str(resp)
                return " ".join(raw.split())[:limit]
            except Exception:
                return "[could not fetch this deliverable]"

        def leader_fn():
            page = _fetch_text(url_mem)
            prompt = (
                "You are the impartial reviewer of a paid milestone.\n\n"
                "MILESTONE: " + title_mem + "\n"
                "ACCEPTANCE CRITERIA (authoritative):\n" + crit_mem + "\n"
                "PAYOUT IF APPROVED: " + str(amount_mem) + " wei of GEN\n"
                "DELIVERABLE URL: " + url_mem + "\n\n"
                "CONTENT FETCHED FROM THE DELIVERABLE RIGHT NOW:\n" + page + "\n\n"
                "Decide whether the deliverable satisfies the acceptance criteria, judging ONLY on "
                "the fetched content above. If the URL is unreachable or empty, it cannot be "
                "approved.\n\n"
                'Respond as JSON with exactly these keys:\n'
                '- "approved": true or false\n'
                '- "reason": concise 2-4 sentence justification citing the fetched content\n'
                '- "confidence": one of "high", "medium", "low"'
            )
            return gl.nondet.exec_prompt(prompt, response_format='json')

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            mine = leader_fn()
            if not isinstance(mine, dict) or not isinstance(leaders_res.calldata, dict):
                return False
            a = mine.get("approved")
            b = leaders_res.calldata.get("approved")
            if isinstance(a, str):
                a = a.strip().lower() == "true"
            if isinstance(b, str):
                b = b.strip().lower() == "true"
            return bool(a) == bool(b)

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        ok = result.get("approved", False)
        if isinstance(ok, str):
            ok = ok.strip().lower() == "true"
        m.state = u8(2) if bool(ok) else u8(3)
        m.verdict_reason = str(result.get("reason", ""))
        m.confidence = str(result.get("confidence", ""))

    @gl.public.write
    def release_milestone(self, project_id: u256, index: u256):
        """Client releases an approved stage: the amount moves to the contractor."""
        if project_id not in self.projects:
            raise gl.vm.UserError("Project not found")
        p = self.projects[project_id]
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(p.client):
            raise gl.vm.UserError("Only the client can release funds")

        m = self._ms(project_id, index)
        if m.state != u8(2):
            raise gl.vm.UserError("Milestone is not approved")
        if m.released:
            raise gl.vm.UserError("Milestone already paid out")

        amount = m.amount
        m.released = True
        p.released = p.released + amount
        p.approved_count = p.approved_count + u256(1)
        self._pay(p.contractor, amount)
        # NOTE: the project is deliberately NOT auto-completed here. Budget that was never
        # allocated to a milestone stays recoverable through withdraw_unallocated() /
        # complete_project() / terminate_project().

    @gl.public.write
    def terminate_project(self, project_id: u256):
        """Client stops the project; everything not yet released goes back to them."""
        if project_id not in self.projects:
            raise gl.vm.UserError("Project not found")
        p = self.projects[project_id]
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(p.client):
            raise gl.vm.UserError("Only the client can terminate")
        if p.state != u8(0):
            raise gl.vm.UserError("Project is not active")

        remaining = p.total - p.released
        p.total = p.released
        p.state = u8(2)              # TERMINATED
        p.paid_out = True
        self._pay(p.client, remaining)

    @gl.public.write
    def withdraw_unallocated(self, project_id: u256):
        """Client recovers budget that was never allocated to a milestone.

        Keeps the project ACTIVE, so further milestones can still be defined against the
        remaining budget - nothing that was funded is ever trapped.
        """
        if project_id not in self.projects:
            raise gl.vm.UserError("Project not found")
        p = self.projects[project_id]
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(p.client):
            raise gl.vm.UserError("Only the client can withdraw")
        if p.state != u8(0):
            raise gl.vm.UserError("Project is not active")

        unallocated = p.total - p.declared
        if int(unallocated) <= 0:
            raise gl.vm.UserError("No unallocated budget to withdraw")

        p.total = p.declared            # shrink the escrowed base to what is actually allocated
        self._pay(p.client, unallocated)

    @gl.public.write
    def complete_project(self, project_id: u256):
        """Client closes a project once every declared milestone has been released.

        Any leftover budget (never-allocated funds, or funds declared but never released)
        is paid back to the client, so a completed project can never strand value.
        """
        if project_id not in self.projects:
            raise gl.vm.UserError("Project not found")
        p = self.projects[project_id]
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(p.client):
            raise gl.vm.UserError("Only the client can complete")
        if p.state != u8(0):
            raise gl.vm.UserError("Project is not active")
        if int(p.declared) <= 0:
            raise gl.vm.UserError("Project has no milestones")
        if int(p.released) < int(p.declared):
            raise gl.vm.UserError("Not all declared milestones have been released")

        leftover = p.total - p.released
        p.total = p.released
        p.state = u8(1)              # COMPLETED
        p.paid_out = True
        self._pay(p.client, leftover)
