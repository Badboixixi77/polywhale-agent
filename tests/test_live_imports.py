"""Live-execution import guard.

Paper mode never touches the on-chain imports, so a broken solana/solders
API can hide for weeks and only explode on the first real trade (2026-08-13:
TxOpts moved from solana.rpc.types to solana.rpc.models and every live swap
failed at import). These tests keep the live path importable.
"""


def test_live_swap_imports_resolve():
    from solders.keypair import Keypair  # noqa: F401
    from solders.message import to_bytes_versioned  # noqa: F401
    from solders.transaction import VersionedTransaction  # noqa: F401
    from solana.rpc.async_api import AsyncClient  # noqa: F401
    from solana.rpc.commitment import Confirmed  # noqa: F401
    try:
        from solana.rpc.types import TxOpts
    except ImportError:
        from solana.rpc.models import TxOpts
    # the exact options _send_swap uses must be constructible
    opts = TxOpts(skip_preflight=False, max_retries=3)
    assert opts.max_retries == 3 and opts.skip_preflight is False
    assert Confirmed is not None


def test_live_signing_roundtrip():
    """Sign a dummy message the same way _send_swap does (no network)."""
    from solders.keypair import Keypair
    from solders.hash import Hash
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.message import to_bytes_versioned
    from solders.transaction import VersionedTransaction

    kp = Keypair()
    dest = Pubkey.new_unique()
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=dest, lamports=1))
    msg = Message.new_with_blockhash([ix], kp.pubkey(), Hash.default())
    sig = kp.sign_message(to_bytes_versioned(msg))
    tx = VersionedTransaction.populate(msg, [sig])
    assert len(bytes(tx)) > 0
