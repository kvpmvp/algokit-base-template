# smart_contracts/escrow_sale/tests/test_escrow_e2e.py
# End-to-end test: deploy escrow sale -> contribute (escrow) -> finalize (success) -> claim -> small withdraw
# Works on TestNet (Algonode) or LocalNet. Requires two funded accounts in escrow_sale/.env.

import os
import sys
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

# ---------------------------------------------------------------------
# Load the contract module directly from its file path to avoid name collisions
# Folder: .../smart_contracts/escrow_sale/
# File:   escrow_sale.py  (adjust CONTRACT_FILE if you renamed it)
# ---------------------------------------------------------------------
THIS_FILE = Path(__file__).resolve()
PKG_DIR = THIS_FILE.parents[1]              # .../escrow_sale
CONTRACT_FILE = PKG_DIR / "escrow_sale.py"  # <-- change if your file has a different name

if not CONTRACT_FILE.exists():
    raise FileNotFoundError(f"Contract file not found at: {CONTRACT_FILE}")

spec = importlib.util.spec_from_file_location("escrow_contract", CONTRACT_FILE)
escrow_contract = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(escrow_contract)
build_approval = escrow_contract.approval  # function that returns (approval_teal, clear_teal)

# Load .env that sits next to escrow_sale.py
load_dotenv(dotenv_path=PKG_DIR / ".env")

ALGOD_SERVER = os.getenv("ALGOD_SERVER", "https://testnet-api.algonode.cloud")
ALGOD_TOKEN = os.getenv("ALGOD_TOKEN", "")
CREATOR_MNEMONIC = os.getenv("CREATOR_MNEMONIC")
BUYER_MNEMONIC = os.getenv("BUYER_MNEMONIC")

assert CREATOR_MNEMONIC and BUYER_MNEMONIC, "Set CREATOR_MNEMONIC and BUYER_MNEMONIC in escrow_sale/.env"

algod_client = algod.AlgodClient(ALGOD_TOKEN, ALGOD_SERVER)

# ---------------------- helpers ----------------------
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

def create_asa(creator_addr: str, creator_sk: str, total=1_000_000, decimals=0) -> int:
    sp = algod_client.suggested_params()
    create = tx.AssetConfigTxn(
        creator_addr, sp,
        total=total,
        default_frozen=False,
        unit_name="TOK",
        asset_name="ProjectToken",
        manager=creator_addr,
        reserve=creator_addr,
        freeze=creator_addr,
        clawback=creator_addr,
        decimals=decimals,
    )
    stx = create.sign(creator_sk)
    txid = algod_client.send_transaction(stx)
    res = wait(txid)
    return res["asset-index"]

def create_app(asa_id: int, rate_tokens_per_algo: int, goal_microalgo: int) -> int:
    # Compile programs
    approval_teal, clear_teal = build_approval()
    approval_prog = compile_teal(approval_teal)
    clear_prog = compile_teal(clear_teal)

    sp = algod_client.suggested_params()

    # Schemas must match the contract's declared global/local usage
    global_schema = tx.StateSchema(num_uints=7, num_byte_slices=1)
    local_schema = tx.StateSchema(0, 0)

    # ABI method definition for create (signature form is SDK-version-proof)
    m_create = Method.from_signature("create_app(uint64,uint64,uint64)uint64")

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=0,  # CREATE
        method=m_create,
        sender=CREATOR,
        sp=sp,
        signer=AccountTransactionSigner(CREATOR_SK),
        method_args=[asa_id, rate_tokens_per_algo, goal_microalgo],
        # creation-only fields:
        on_complete=tx.OnComplete.NoOpOC,
        approval_program=approval_prog,
        clear_program=clear_prog,
        global_schema=global_schema,
        local_schema=local_schema,
        extra_pages=0,
    )
    resp = atc.execute(algod_client, 4)
    create_txid = resp.tx_ids[0]
    confirmed = wait(create_txid)
    return confirmed["application-index"]

def fund(addr: str, amt: int):
    sp = algod_client.suggested_params()
    p = tx.PaymentTxn(CREATOR, sp, addr, amt)
    stx = p.sign(CREATOR_SK)
    algod_client.send_transaction(stx)
    wait(stx.get_txid())

def call_method(app_id: int, sender: str, sk: str, method: Method, args=None, boxes=None, extra_fee=0, foreign_assets=None, foreign_apps=None, accounts=None):
    sp = algod_client.suggested_params()
    sp.flat_fee = True
    sp.fee = max(sp.min_fee, 1000) + int(extra_fee)
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=method,
        sender=sender,
        sp=sp,  # ATC expects 'sp' kwarg
        signer=AccountTransactionSigner(sk),
        method_args=args or [],
        boxes=boxes or [],
        foreign_assets=foreign_assets or [],
        foreign_apps=foreign_apps or [],
        accounts=accounts or [],
    )
    return atc.execute(algod_client, 4)

def app_boxes_for_user(app_id: int, addr: str):
    # box key = b"u:" + 32 raw bytes of the address
    name = b"u:" + encoding.decode_address(addr)
    return [(app_id, name)]

def user_asset_balance(addr: str, asa_id: int) -> int:
    info = algod_client.account_info(addr)
    for a in info.get("assets", []):
        if a["asset-id"] == asa_id:
            return a.get("amount", 0)
    return 0

# ---------------------- test ----------------------
def test_success_flow():
    # 1) Create an ASA (0 decimals for easy math)
    asa_id = create_asa(CREATOR, CREATOR_SK, total=1_000_000, decimals=0)

    # 2) Create app with rate=100 tokens/ALGO and goal=5 ALGO (5_000_000 µAlgo)
    app_id = create_app(asa_id, rate_tokens_per_algo=100, goal_microalgo=5_000_000)
    app_addr = get_application_address(app_id)

    # 3) Fund the app address for min balance + inner tx fees (~0.4 ALGO is safe)
    fund(app_addr, 400_000)

    # 4) Creator calls opt_in_asset()  (pass ASA as foreign asset for environments that require it)
    m_optin = Method.from_signature("opt_in_asset()void")
    call_method(app_id, CREATOR, CREATOR_SK, m_optin, extra_fee=1000, foreign_assets=[asa_id])

    # 5) Deposit the sale supply to the app (e.g., 100k TOK)
    sp = algod_client.suggested_params()
    xfer = tx.AssetTransferTxn(CREATOR, sp, app_addr, 100_000, asa_id)
    algod_client.send_transaction(xfer.sign(CREATOR_SK))
    wait(xfer.get_txid())

    # 6) BUYER contributes 3 ALGO (escrow; no token yet)
    sp_pay = algod_client.suggested_params()
    pay = tx.PaymentTxn(BUYER, sp_pay, app_addr, 3_000_000)  # 3 ALGO
    atc = AtomicTransactionComposer()
    atc.add_transaction(TransactionWithSigner(pay, AccountTransactionSigner(BUYER_SK)))
    m_contrib = Method.from_signature("contribute()void")
    sp_app = algod_client.suggested_params()
    sp_app.flat_fee = True
    sp_app.fee = 3000  # bump for box write
    atc.add_method_call(
        app_id=app_id,
        method=m_contrib,
        sender=BUYER,
        sp=sp_app,
        signer=AccountTransactionSigner(BUYER_SK),
        method_args=[],
        boxes=app_boxes_for_user(app_id, BUYER),
    )
    atc.execute(algod_client, 4)

    # 7) CREATOR contributes 2 ALGO (to hit the 5 ALGO goal)
    sp_pay2 = algod_client.suggested_params()
    pay2 = tx.PaymentTxn(CREATOR, sp_pay2, app_addr, 2_000_000)
    atc2 = AtomicTransactionComposer()
    atc2.add_transaction(TransactionWithSigner(pay2, AccountTransactionSigner(CREATOR_SK)))
    atc2.add_method_call(
        app_id=app_id,
        method=m_contrib,
        sender=CREATOR,
        sp=sp_app,
        signer=AccountTransactionSigner(CREATOR_SK),
        method_args=[],
        boxes=app_boxes_for_user(app_id, CREATOR),
    )
    atc2.execute(algod_client, 4)

    # 8) finalize() should set SUCCESS (TOTAL >= GOAL)
    m_finalize = Method.from_signature("finalize()void")
    call_method(app_id, BUYER, BUYER_SK, m_finalize)

    # 9) Before claiming, BUYER must opt-in to ASA to receive tokens
    sp = algod_client.suggested_params()
    optin_buyer = tx.AssetTransferTxn(BUYER, sp, BUYER, 0, asa_id)
    algod_client.send_transaction(optin_buyer.sign(BUYER_SK))
    wait(optin_buyer.get_txid())

    # 10) BUYER claim() should receive 3 * 100 = 300 TOK
    before = user_asset_balance(BUYER, asa_id)
    m_claim = Method.from_signature("claim()uint64")
    res = call_method(
        app_id,
        BUYER,
        BUYER_SK,
        m_claim,
        boxes=app_boxes_for_user(app_id, BUYER),
        extra_fee=2000,              # inner ASA transfer
        foreign_assets=[asa_id],     # include ASA for environments that require it
    )
    tokens_sent = res.abi_results[0].return_value
    assert tokens_sent == 300, f"expected 300 TOK, got {tokens_sent}"

    time.sleep(1.0)
    after = user_asset_balance(BUYER, asa_id)
    assert after - before == 300, f"buyer balance delta mismatch: {after - before}"

    # 11) (Optional) creator withdraws a bit of ALGO to verify path
    m_withdraw = Method.from_signature("withdraw_algo(uint64)void")
    call_method(app_id, CREATOR, CREATOR_SK, m_withdraw, args=[1_000_000], extra_fee=1000)
