# smart_contracts/escrow_sale/tests/test_expiry_refund_flow.py
# Scenario: Use an existing ASA (default 744398469). Deposit 500 tokens to the app.
# Rate=50 TOK/ALGO, Goal=10 ALGO. Investor contributes 5 ALGO, sale expires, buyer refunds,
# then creator reclaims the 500 tokens.

import os
import base64
import time
from pathlib import Path
import importlib.util

from dotenv import load_dotenv
from algosdk import mnemonic, encoding, account, transaction as tx
from algosdk.v2client import algod
from algosdk.logic import get_application_address
from algosdk.atomic_transaction_composer import (
    AtomicTransactionComposer,
    TransactionWithSigner,
    AccountTransactionSigner,
)
from algosdk.abi import Method

# -------------------- Load contract module --------------------
THIS_FILE = Path(__file__).resolve()
PKG_DIR = THIS_FILE.parents[1]                 # .../escrow_sale
CONTRACT_FILE = PKG_DIR / "escrow_sale.py"

if not CONTRACT_FILE.exists():
    raise FileNotFoundError(f"Contract file not found at: {CONTRACT_FILE}")

spec = importlib.util.spec_from_file_location("escrow_contract", CONTRACT_FILE)
escrow_contract = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(escrow_contract)
build_approval = escrow_contract.approval  # returns (approval_teal, clear_teal)

# -------------------- Env / clients --------------------
load_dotenv(dotenv_path=PKG_DIR / ".env")

ALGOD_SERVER = os.getenv("ALGOD_SERVER", "https://testnet-api.algonode.cloud")
ALGOD_TOKEN = os.getenv("ALGOD_TOKEN", "")
CREATOR_MNEMONIC = os.getenv("CREATOR_MNEMONIC")
BUYER_MNEMONIC = os.getenv("BUYER_MNEMONIC")
ASA_ID = int(os.getenv("ASA_ID", "744398469"))  # override in .env if needed

assert CREATOR_MNEMONIC and BUYER_MNEMONIC, "Set CREATOR_MNEMONIC and BUYER_MNEMONIC in escrow_sale/.env"

algod_client = algod.AlgodClient(ALGOD_TOKEN, ALGOD_SERVER)

# -------------------- helpers --------------------
def addr_sk_from_mn(mn: str):
    sk = mnemonic.to_private_key(mn)
    addr = account.address_from_private_key(sk)
    return addr, sk

CREATOR, CREATOR_SK = addr_sk_from_mn(CREATOR_MNEMONIC)
BUYER, BUYER_SK = addr_sk_from_mn(BUYER_MNEMONIC)

def wait(txid: str):
    return tx.wait_for_confirmation(algod_client, txid, 10)

def compile_teal(src: str) -> bytes:
    resp = algod_client.compile(src)
    return base64.b64decode(resp["result"])

def fund(addr: str, amt: int):
    sp = algod_client.suggested_params()
    p = tx.PaymentTxn(CREATOR, sp, addr, amt)
    stx = p.sign(CREATOR_SK)
    algod_client.send_transaction(stx)
    wait(stx.get_txid())

def app_boxes_for_user(app_id: int, addr: str):
    name = b"u:" + encoding.decode_address(addr)
    return [(app_id, name)]

def user_algo_balance(addr: str) -> int:
    return algod_client.account_info(addr).get("amount", 0)

def user_asset_balance(addr: str, asa_id: int) -> int:
    info = algod_client.account_info(addr)
    for a in info.get("assets", []):
        if a["asset-id"] == asa_id:
            return a.get("amount", 0)
    return 0

def ensure_asset_optin(addr: str, sk: str, asa_id: int):
    """Opt-in 'addr' to asset if not already opted-in."""
    if user_asset_balance(addr, asa_id) is None:  # not reliable; check holdings
        pass
    info = algod_client.account_info(addr)
    opted = any(a["asset-id"] == asa_id for a in info.get("assets", []))
    if not opted:
        sp = algod_client.suggested_params()
        optin = tx.AssetTransferTxn(addr, sp, addr, 0, asa_id)
        algod_client.send_transaction(optin.sign(sk))
        wait(optin.get_txid())

def call_method(app_id: int, sender: str, sk: str, method: Method,
                args=None, boxes=None, extra_fee=0, foreign_assets=None):
    sp = algod_client.suggested_params()
    sp.flat_fee = True
    sp.fee = max(sp.min_fee, 1000) + int(extra_fee)
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=method,
        sender=sender,
        sp=sp,
        signer=AccountTransactionSigner(sk),
        method_args=args or [],
        boxes=boxes or [],
        foreign_assets=foreign_assets or [],
    )
    return atc.execute(algod_client, 4)

# -------------------- the test --------------------
def test_expiry_buyer_refund_and_creator_reclaim_existing_asa():
    # Scenario parameters (independent of ASA decimals since we don't claim tokens here)
    DEPOSIT_TO_APP = 500              # base units of ASA
    RATE = 50                         # tokens / ALGO (base units per ALGO)
    GOAL = 10_000_000                 # microAlgos (10 ALGO)
    INVEST = 5_000_000                # microAlgos (5 ALGO)

    # 1) Ensure the creator is opted-in and holds at least 500 units of the existing ASA
    ensure_asset_optin(CREATOR, CREATOR_SK, ASA_ID)
    creator_hold = user_asset_balance(CREATOR, ASA_ID)
    assert creator_hold >= DEPOSIT_TO_APP, (
        f"Creator must hold at least {DEPOSIT_TO_APP} units of ASA {ASA_ID}, "
        f"current balance: {creator_hold}"
    )

    # 2) Create app with RATE and GOAL
    approval_teal, clear_teal = build_approval()
    approval_prog = compile_teal(approval_teal)
    clear_prog = compile_teal(clear_teal)

    sp = algod_client.suggested_params()
    global_schema = tx.StateSchema(num_uints=7, num_byte_slices=1)
    local_schema = tx.StateSchema(0, 0)
    m_create = Method.from_signature("create_app(uint64,uint64,uint64)uint64")

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=0,
        method=m_create,
        sender=CREATOR,
        sp=sp,
        signer=AccountTransactionSigner(CREATOR_SK),
        method_args=[ASA_ID, RATE, GOAL],
        on_complete=tx.OnComplete.NoOpOC,
        approval_program=approval_prog,
        clear_program=clear_prog,
        global_schema=global_schema,
        local_schema=local_schema,
    )
    resp = atc.execute(algod_client, 4)
    create_txid = resp.tx_ids[0]
    conf = wait(create_txid)
    app_id = conf["application-index"]
    app_addr = get_application_address(app_id)

    # 3) Fund app address for min-balance & inner txn fees
    fund(app_addr, 400_000)

    # 4) App opts into the existing ASA
    m_optin = Method.from_signature("opt_in_asset()void")
    call_method(app_id, CREATOR, CREATOR_SK, m_optin, extra_fee=1000, foreign_assets=[ASA_ID])

    # 5) Deposit 500 tokens into the app
    sp = algod_client.suggested_params()
    xfer = tx.AssetTransferTxn(CREATOR, sp, app_addr, DEPOSIT_TO_APP, ASA_ID)
    algod_client.send_transaction(xfer.sign(CREATOR_SK))
    wait(xfer.get_txid())

    # 6) Buyer contributes 5 ALGO (escrowed)
    sp_pay = algod_client.suggested_params()
    pay = tx.PaymentTxn(BUYER, sp_pay, app_addr, INVEST)
    atc = AtomicTransactionComposer()
    atc.add_transaction(TransactionWithSigner(pay, AccountTransactionSigner(BUYER_SK)))
    m_contrib = Method.from_signature("contribute()void")
    sp_app = algod_client.suggested_params()
    sp_app.flat_fee = True
    sp_app.fee = 3000  # for box write
    atc.add_method_call(
        app_id=app_id,
        method=m_contrib,
        sender=BUYER,
        sp=sp_app,
        signer=AccountTransactionSigner(BUYER_SK),
        boxes=app_boxes_for_user(app_id, BUYER),
    )
    atc.execute(algod_client, 4)

    # 7) Wait for deadline to pass (contract DURATION_SECONDS = 60)
    time.sleep(65)

    # 8) Finalize -> should mark EXPIRED (since total < goal)
    m_finalize = Method.from_signature("finalize()void")
    call_method(app_id, CREATOR, CREATOR_SK, m_finalize)

    # 9) Buyer calls refund() to get pledged ALGO back
    m_refund = Method.from_signature("refund()uint64")
    app_algo_before = user_algo_balance(app_addr)
    buyer_algo_before = user_algo_balance(BUYER)

    res = call_method(
        app_id,
        BUYER,
        BUYER_SK,
        m_refund,
        boxes=app_boxes_for_user(app_id, BUYER),
        extra_fee=2000,  # inner payment
    )
    refunded = res.abi_results[0].return_value
    assert refunded == INVEST, f"refunded {refunded}, expected {INVEST}"

    time.sleep(1.0)
    app_algo_after = user_algo_balance(app_addr)
    buyer_algo_after = user_algo_balance(BUYER)

    # App should have sent back exactly the investor's 5 ALGO
    assert app_algo_before - app_algo_after == INVEST, "App ALGO balance did not decrease by refund amount"
    # Buyer should have received ~5 ALGO more, minus the outer fee they paid
    assert buyer_algo_after >= buyer_algo_before + INVEST - 3_000, "Buyer ALGO balance did not increase as expected"

    # 10) Creator reclaims their deposited tokens from the app
    app_tok_before = user_asset_balance(app_addr, ASA_ID)
    creator_tok_before = user_asset_balance(CREATOR, ASA_ID)
    assert app_tok_before >= DEPOSIT_TO_APP, "App does not hold the expected deposited tokens"

    m_reclaim = Method.from_signature("reclaim_asset(uint64,address)void")
    call_method(
        app_id,
        CREATOR,
        CREATOR_SK,
        m_reclaim,
        args=[DEPOSIT_TO_APP, CREATOR],
        extra_fee=2000,            # inner ASA transfer
        foreign_assets=[ASA_ID],   # some nodes require ASA listed
    )

    time.sleep(1.0)
    app_tok_after = user_asset_balance(app_addr, ASA_ID)
    creator_tok_after = user_asset_balance(CREATOR, ASA_ID)

    # App ASA balance decreases by the reclaimed amount; creator balance increases by same amount
    assert app_tok_before - app_tok_after == DEPOSIT_TO_APP, "App ASA balance did not decrease by reclaimed amount"
    assert creator_tok_after - creator_tok_before == DEPOSIT_TO_APP, "Creator ASA balance did not increase by reclaimed amount"
