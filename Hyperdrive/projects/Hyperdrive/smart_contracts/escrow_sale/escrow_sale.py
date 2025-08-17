# smart_contracts/escrow_sale/escrow_sale.py
# Goal-based, time-boxed ASA sale with full escrow (ALGO + ASA).

from pyteal import *
from pyteal import abi

# ---------------- Constants ----------------
RATE_SCALE = Int(1_000_000)                 # 1 ALGO = 1_000_000 microAlgos
DURATION_SECONDS = Int(60 * 24 * 60 * 60)   # 60 days

# ---------------- Global state keys ----------------
CREATOR   = Bytes("creator")     # bytes: address
ASA_ID    = Bytes("asa_id")      # uint
RATE      = Bytes("rate")        # uint: tokens per 1 ALGO (ASA base units)
GOAL      = Bytes("goal")        # uint: microAlgos target
TOTAL     = Bytes("total")       # uint: microAlgos pledged
START_TS  = Bytes("start_ts")    # uint: creation timestamp
DEADLINE  = Bytes("deadline")    # uint: start + 60d
STATUS    = Bytes("status")      # uint: 0=open, 1=success, 2=expired

# ---------------- Per-user box layout ----------------
# key: b"u:" + <32-byte address>
# value (16 bytes): pledged_microalgo(8) || tokens_owed(8)
BOX_PREFIX = Bytes("u:")

def box_key(addr: Expr) -> Expr:
    return Concat(BOX_PREFIX, addr)

def itob8(x: Expr) -> Expr:
    return Itob(x)  # Itob already returns 8 bytes (u64)

def btoi8(bs: Expr, start: int) -> Expr:
    return Btoi(Extract(bs, Int(start), Int(8)))


def approval():
    router = Router("EscrowSale")

    # ---------- Compat shim: emulate @router.create if missing ----------
    def _create_decorator(router_obj):
        if hasattr(router_obj, "create"):
            return router_obj.create
        def _wrap(fn=None, **_kwargs):
            return router_obj.method(no_op=CallConfig.CREATE)(fn)
        return _wrap

    create = _create_decorator(router)

    # ===================== CREATE =====================
    @create
    def create_app(
        asa_id: abi.Uint64,
        rate_tokens_per_algo: abi.Uint64,
        goal_microalgo: abi.Uint64,
        *,
        output: abi.Uint64,
    ):
        now = Global.latest_timestamp()
        return Seq(
            App.globalPut(CREATOR, Txn.sender()),
            App.globalPut(ASA_ID, asa_id.get()),
            App.globalPut(RATE, rate_tokens_per_algo.get()),
            App.globalPut(GOAL, goal_microalgo.get()),
            App.globalPut(TOTAL, Int(0)),
            App.globalPut(START_TS, now),
            App.globalPut(DEADLINE, now + DURATION_SECONDS),
            App.globalPut(STATUS, Int(0)),  # open
            output.set(Int(1)),
        )

    # ===================== ADMIN METHODS =====================
    @router.method
    def opt_in_asset():
        asa = App.globalGet(ASA_ID)
        return Seq(
            Assert(Txn.sender() == App.globalGet(CREATOR)),
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.AssetTransfer,
                TxnField.xfer_asset: asa,
                TxnField.asset_amount: Int(0),
                TxnField.asset_receiver: Global.current_application_address(),
            }),
            InnerTxnBuilder.Submit(),
            Approve(),
        )

    @router.method
    def set_rate(new_rate_tokens_per_algo: abi.Uint64):
        return Seq(
            Assert(Txn.sender() == App.globalGet(CREATOR)),
            Assert(App.globalGet(STATUS) == Int(0)),
            App.globalPut(RATE, new_rate_tokens_per_algo.get()),
            Approve(),
        )

    @router.method
    def withdraw_algo(amount: abi.Uint64):
        return Seq(
            Assert(Txn.sender() == App.globalGet(CREATOR)),
            Assert(App.globalGet(STATUS) == Int(1)),
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.Payment,
                TxnField.receiver: Txn.sender(),
                TxnField.amount: amount.get(),
            }),
            InnerTxnBuilder.Submit(),
            Approve(),
        )

    @router.method
    def reclaim_asset(amount: abi.Uint64, to: abi.Address):
        asa = App.globalGet(ASA_ID)
        return Seq(
            Assert(Txn.sender() == App.globalGet(CREATOR)),
            Assert(Or(App.globalGet(STATUS) == Int(2), App.globalGet(STATUS) == Int(1))),
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.AssetTransfer,
                TxnField.xfer_asset: asa,
                TxnField.asset_amount: amount.get(),
                TxnField.asset_receiver: to.get(),
            }),
            InnerTxnBuilder.Submit(),
            Approve(),
        )

    # ===================== CONTRIBUTION / FINALIZE =====================
    @router.method
    def contribute():
        """
        Expected group: [0] Payment -> app, [1] this AppCall.
        """
        app_addr = Global.current_application_address()
        rate = App.globalGet(RATE)
        user_key = box_key(Gtxn[0].sender())

        pledged = ScratchVar(TealType.uint64)
        owed    = ScratchVar(TealType.uint64)
        tokens  = ScratchVar(TealType.uint64)
        present = BoxGet(user_key)

        return Seq(
            Assert(App.globalGet(STATUS) == Int(0)),
            Assert(Global.latest_timestamp() < App.globalGet(DEADLINE)),

            Assert(Global.group_size() == Int(2)),
            Assert(Txn.group_index() == Int(1)),
            Assert(Gtxn[0].type_enum() == TxnType.Payment),
            Assert(Gtxn[0].receiver() == app_addr),
            Assert(Gtxn[0].amount() > Int(0)),
            Assert(Gtxn[0].rekey_to() == Global.zero_address()),
            Assert(Gtxn[0].close_remainder_to() == Global.zero_address()),

            pledged.store(Int(0)),
            owed.store(Int(0)),
            present,
            If(present.hasValue()).Then(Seq(
                pledged.store(btoi8(present.value(), 0)),
                owed.store(btoi8(present.value(), 8)),
            )),

            tokens.store(WideRatio([Gtxn[0].amount(), rate], [RATE_SCALE])),
            Assert(tokens.load() > Int(0)),

            pledged.store(pledged.load() + Gtxn[0].amount()),
            owed.store(owed.load() + tokens.load()),
            BoxPut(user_key, Concat(itob8(pledged.load()), itob8(owed.load()))),

            App.globalPut(TOTAL, App.globalGet(TOTAL) + Gtxn[0].amount()),
            Approve(),
        )

    @router.method
    def finalize():
        total    = App.globalGet(TOTAL)
        goal     = App.globalGet(GOAL)
        now      = Global.latest_timestamp()
        deadline = App.globalGet(DEADLINE)

        return Seq(
            Assert(App.globalGet(STATUS) == Int(0)),
            If(total >= goal)
            .Then(App.globalPut(STATUS, Int(1)))
            .Else(Seq(
                Assert(now >= deadline),
                App.globalPut(STATUS, Int(2)),
            )),
            Approve(),
        )

    # ===================== CLAIM / REFUND =====================
    @router.method
    def claim(*, output: abi.Uint64):
        """
        After SUCCESS, contributor claims owed ASA.
        Returns: uint64 = tokens sent.
        """
        asa = App.globalGet(ASA_ID)
        user_key = box_key(Txn.sender())
        present = BoxGet(user_key)

        pledged = ScratchVar(TealType.uint64)
        owed    = ScratchVar(TealType.uint64)

        return Seq(
            Assert(App.globalGet(STATUS) == Int(1)),
            present,
            Assert(present.hasValue()),
            pledged.store(btoi8(present.value(), 0)),
            owed.store(btoi8(present.value(), 8)),
            Assert(owed.load() > Int(0)),

            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.AssetTransfer,
                TxnField.xfer_asset: asa,
                TxnField.asset_amount: owed.load(),
                TxnField.asset_receiver: Txn.sender(),
            }),
            InnerTxnBuilder.Submit(),

            BoxPut(user_key, Concat(itob8(Int(0)), itob8(Int(0)))),

            output.set(owed.load()),
        )

    @router.method
    def refund(*, output: abi.Uint64):
        """
        After EXPIRED, contributor refunds pledged ALGO.
        Returns: uint64 = microAlgos refunded.
        """
        user_key = box_key(Txn.sender())
        present = BoxGet(user_key)

        pledged = ScratchVar(TealType.uint64)
        owed    = ScratchVar(TealType.uint64)

        return Seq(
            Assert(App.globalGet(STATUS) == Int(2)),
            present,
            Assert(present.hasValue()),
            pledged.store(btoi8(present.value(), 0)),
            owed.store(btoi8(present.value(), 8)),
            Assert(pledged.load() > Int(0)),

            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.Payment,
                TxnField.receiver: Txn.sender(),
                TxnField.amount: pledged.load(),
            }),
            InnerTxnBuilder.Submit(),

            BoxPut(user_key, Concat(itob8(Int(0)), itob8(Int(0)))),

            output.set(pledged.load()),
        )

    @router.method(no_op=CallConfig.CALL)
    def noop():
        return Approve()

    # --------- Return just the two TEAL programs (not the contract tuple) ----------
    compiled = router.compile_program(version=8)
    if isinstance(compiled, (tuple, list)):
        if len(compiled) >= 2:
            approval_teal, clear_teal = compiled[0], compiled[1]
        else:
            approval_teal, clear_teal = compiled[0], compileTeal(Approve(), mode=Mode.Application, version=8)
    else:
        approval_teal = compiled
        clear_teal = compileTeal(Approve(), mode=Mode.Application, version=8)

    return approval_teal, clear_teal


if __name__ == "__main__":
    approval_teal, clear_teal = approval()
    print("# --- APPROVAL ---")
    print(approval_teal)
    print("# --- CLEAR ---")
    print(clear_teal)
